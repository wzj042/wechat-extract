"""动画表情（local_type=47）按消息 XML 从微信 CDN 取回原件，校验后落盘并为动图生成抽帧拼图。

本地 cache/<月>/Emoticon 下的副本用未知密钥加密，不使用。实测 cdnurl 直接返回原件，
encrypturl 返回 AES-128-CBC 密文（key = iv = aeskey），两者解出的内容 md5 都等于 XML 的 md5。
externurl 不是原件，不用。失败结果不包含 aeskey、URL 或正文。
"""

import hashlib
import io
import math
import os
from pathlib import Path
import re
import tempfile
import urllib.request
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from .events import strip_group_prefix
from .export_media import IncompleteImageError, _validate_image
from .media import media_output_relative_path

DEFAULT_HOSTS = (".qq.com", ".qpic.cn")
TIMEOUT = 30
MAX_BYTES = 20 * 1024 * 1024
FORMATS = ("gif", "png", "jpg", "webp")
CACHE_DIR = ".emoji-cache"
PREVIEW_FRAMES = 6
PREVIEW_EDGE = 256
PREVIEW_GAP = 4
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")


def parse_emoji(content_text):
    """解析 <msg><emoji .../></msg>；群消息的“wxid:\\n”前缀先去掉，&amp; 由 XML 解析器还原。"""
    text = strip_group_prefix(content_text or "")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("emoji_xml_invalid")
    start = text.find("<")
    try:
        root = ET.fromstring(text[start:]) if start >= 0 else None
    except ET.ParseError:
        root = None
    node = None if root is None else root if root.tag == "emoji" else root.find("emoji")
    if node is None:
        raise ValueError("emoji_xml_invalid")
    md5 = (node.get("md5") or "").strip()
    if not _HEX32.fullmatch(md5):
        # md5 同时用作缓存文件名，必须严格是 32 位十六进制。
        raise ValueError("emoji_md5_missing")

    def number(name):
        value = (node.get(name) or "").strip()
        return int(value) if value.isdigit() else None

    return {
        "md5": md5.lower(), "len": number("len"),
        "cdnurl": node.get("cdnurl") or "", "encrypturl": node.get("encrypturl") or "",
        "aeskey": node.get("aeskey") or "",
        "width": number("width"), "height": number("height"),
    }


def _suffixes(hosts):
    # "qq.com" 与 ".qq.com" 等价：都表示该域名本身及其子域。配置误写成单个字符串也按一项处理。
    hosts = [hosts] if isinstance(hosts, str) else hosts
    return tuple("." + str(host).strip().lower().lstrip(".") for host in hosts if str(host).strip(". "))


def allowed(url, hosts=None):
    """只放行 http/https 且主机名落在白名单后缀内的地址。"""
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    return any(("." + host).endswith(suffix) for suffix in _suffixes(hosts or DEFAULT_HOSTS))


class _Redirect(urllib.request.HTTPRedirectHandler):
    """重定向目标同样必须在白名单内，避免被 CDN 之外的地址带走。"""

    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl, self.hosts):
            raise ValueError("emoji_host_not_allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, *, hosts=None, timeout=TIMEOUT, max_bytes=MAX_BYTES):
    """GET 白名单 CDN 地址；超过 max_bytes 直接拒绝。测试中 monkeypatch 本函数代替联网。"""
    if not allowed(url, hosts):
        raise ValueError("emoji_host_not_allowed")
    opener = urllib.request.build_opener(_Redirect(hosts))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(request, timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError("emoji_too_large")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("emoji_too_large")
    return data


def _decrypt(data, aeskey):
    """AES-128-CBC，key = iv = bytes.fromhex(aeskey)，去 PKCS7。"""
    key = bytes.fromhex(aeskey)
    if len(key) != 16 or not data or len(data) % 16:
        raise ValueError("emoji_decrypt_failed")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise ValueError("emoji_decrypt_failed")
    return plain[:-pad]


def _verify(data, info):
    """md5（及 len）必须与 XML 一致，再完整解码；返回 (扩展名, 宽, 高)。"""
    if info["len"] is not None and len(data) != info["len"]:
        raise IncompleteImageError("emoji_length_mismatch")
    if hashlib.md5(data).hexdigest() != info["md5"]:
        raise IncompleteImageError("emoji_md5_mismatch")
    try:
        extension, width, height = _validate_image(data)
    except IncompleteImageError:
        raise
    except ValueError as exc:
        raise ValueError("unsupported_emoji_format") from exc
    if extension not in FORMATS:
        raise ValueError("unsupported_emoji_format")
    return extension, width, height


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".export-", dir=path.parent) as temporary:
        staged = Path(temporary) / "payload"
        with staged.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)


def _same(path, md5):
    try:
        return path.is_file() and hashlib.md5(path.read_bytes()).hexdigest() == md5
    except OSError:
        return False


def _animation(data):
    """返回 (帧数, 总时长毫秒)；静图时长为 None。"""
    with Image.open(io.BytesIO(data)) as image:
        frames = getattr(image, "n_frames", 1)
        if frames <= 1:
            return 1, None
        total = 0
        for index in range(frames):
            image.seek(index)
            total += int(image.info.get("duration") or 0)
        return frames, total


def _preview(data, max_frames=PREVIEW_FRAMES, edge=PREVIEW_EDGE, gap=PREVIEW_GAP):
    """均匀抽帧、等比缩小、合成到白底，按时间顺序排成 ≤3 列网格；返回 (PNG 字节, 抽取帧数)。"""
    with Image.open(io.BytesIO(data)) as image:
        total = getattr(image, "n_frames", 1)
        count = min(total, max_frames)
        tiles = []
        for index in (i * total // count for i in range(count)):
            image.seek(index)
            frame = image.convert("RGBA")
            frame.thumbnail((edge, edge))
            tile = Image.new("RGB", frame.size, "white")
            tile.paste(frame, mask=frame.getchannel("A"))
            tiles.append(tile)
    columns = min(3, count)
    rows = math.ceil(count / columns)
    cell_w, cell_h = max(t.width for t in tiles), max(t.height for t in tiles)
    canvas = Image.new("RGB", (columns * cell_w + (columns + 1) * gap, rows * cell_h + (rows + 1) * gap),
                       (200, 200, 200))
    for position, tile in enumerate(tiles):
        row, column = divmod(position, columns)
        left, top = gap + column * (cell_w + gap), gap + row * (cell_h + gap)
        canvas.paste(Image.new("RGB", (cell_w, cell_h), "white"), (left, top))
        canvas.paste(tile, (left + (cell_w - tile.width) // 2, top + (cell_h - tile.height) // 2))
    stream = io.BytesIO()
    canvas.save(stream, "PNG")
    return stream.getvalue(), count


def _from_cache(cache, info):
    for extension in FORMATS:
        path = cache / f"{info['md5']}.{extension}"
        try:
            data = path.read_bytes()
            verified = _verify(data, info)
        except (OSError, ValueError):
            continue
        if verified[0] == extension:
            return data, verified
    return None, None


def export_emoji(row, output_dir, *, hosts=None, fetch=None):
    """导出一条动画表情：缓存 → cdnurl → encrypturl；原件写成 emoji.<ext>，动图另生成 preview.png。

    fetch(url) -> bytes 可注入；默认调用模块级 download（白名单、30s 超时、20MB 上限）。
    同一表情按 md5 内容寻址缓存在 <output_dir>/.emoji-cache，命中就不再下载。
    """
    identity = {name: row[name] for name in ("account", "conversation", "shard", "table", "local_id")}
    result = {"status": "missing", "reason": "", "kind": "emoji", "identity": identity,
              "output_path": None, "preview_path": None, "md5": None}

    def fail(status, reason):
        return dict(result, status=status, reason=reason)

    if int(row["local_type"]) & 0xFFFFFFFF != 47:
        return fail("unsupported", "not_emoji_message")
    try:
        info = parse_emoji(row.get("content_text"))
    except ValueError as exc:
        return fail("unsupported", str(exc))
    result["md5"] = info["md5"]
    fetch = fetch or (lambda url: download(url, hosts=hosts))

    output = Path(output_dir).resolve()
    cache = output / CACHE_DIR
    destination = output / media_output_relative_path(row)
    if not destination.resolve().is_relative_to(output):
        raise ValueError("输出路径越出指定目录")

    (data, verified), source = _from_cache(cache, info), "cache"
    if data is None:
        failure = ("missing", "emoji_url_missing")
        for source, url in (("cdn", info["cdnurl"]), ("encrypted_cdn", info["encrypturl"])):
            if not url:
                continue
            if source == "encrypted_cdn" and not _HEX32.fullmatch(info["aeskey"]):
                failure = ("decrypt_failed", "emoji_aeskey_missing")
                continue
            if not allowed(url, hosts):
                failure = ("download_failed", "emoji_host_not_allowed")
                continue
            try:
                payload = fetch(url)
            except Exception:
                # 第三方异常可能带 URL，只保留固定原因。
                failure = ("download_failed", "emoji_download_failed")
                continue
            if source == "encrypted_cdn":
                try:
                    payload = _decrypt(payload, info["aeskey"])
                except ValueError:
                    failure = ("decrypt_failed", "emoji_decrypt_failed")
                    continue
            try:
                verified = _verify(payload, info)
            except IncompleteImageError as exc:
                failure = ("incomplete", str(exc))
                continue
            except ValueError as exc:
                failure = ("unsupported", str(exc))
                continue
            data = payload
            break
        if data is None:
            # 两条都失败时报告最后一次尝试的原因。
            return fail(*failure)

    extension, width, height = verified
    try:
        frames, duration = _animation(data)
    except (OSError, ValueError):
        return fail("incomplete", "image_decode_failed")
    try:
        if source != "cache":
            _write_atomic(cache / f"{info['md5']}.{extension}", data)
        final = destination / f"emoji.{extension}"
        if not _same(final, info["md5"]):
            _write_atomic(final, data)
        preview, sampled = final, 1
        if frames > 1:
            content, sampled = _preview(data)
            preview = destination / "preview.png"
            _write_atomic(preview, content)
    except OSError:
        return fail("write_failed", "output_io_failed")
    extra = {"duration_ms": duration} if frames > 1 else {}
    return dict(result, status="ok", reason="", output_path=str(final), preview_path=str(preview),
                format=extension, width=width, height=height, animated=frames > 1,
                frames=frames, preview_frames=sampled, source=source, **extra)
