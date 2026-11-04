"""按单条消息导出已缓存图片；仅在稳定读取、解密和完整解码通过后原子落盘。"""

import hashlib
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile

from PIL import Image, UnidentifiedImageError

from .media import AmbiguousMediaError, resolve_image
from .messages import ShardReadError
from .wxdat import ImageKeyProvider, MAGIC_V1, MAGIC_V2, dat_decrypt, wxgf_to_jpg


class IncompleteImageError(ValueError):
    """图片损坏、截断或读取时仍在变化。"""


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_stable(path):
    before = _signature(path.stat())
    data = path.read_bytes()
    between = _signature(path.stat())
    second = path.read_bytes()
    after = _signature(path.stat())
    if before != between or before != after or data != second or len(data) != before[2]:
        raise IncompleteImageError("source_changed")
    if not data:
        raise IncompleteImageError("empty_file")
    return data, after


def _known_image(data):
    return (
        data.startswith((b"\xff\xd8", b"\x89PNG", b"GIF87a", b"GIF89a", b"BM"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def _validate_image(data):
    """结束标记和 Pillow verify/load 双重检查；小于 100 KB 的小图同样有效。"""
    if data.startswith(b"\xff\xd8") and not data.endswith(b"\xff\xd9"):
        raise IncompleteImageError("jpeg_missing_end")
    if data.startswith(b"\x89PNG") and not data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
        raise IncompleteImageError("png_missing_end")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            extension = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp"}.get(image.format)
            if extension is None:
                raise ValueError("unsupported_image_format")
            # 动图的后续帧也需要实际解码。
            for frame in range(1, getattr(image, "n_frames", 1)):
                image.seek(frame)
                image.load()
            return extension, image.width, image.height
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise IncompleteImageError("image_decode_failed") from exc


def _trim_dat_footer(data):
    # 参考副本 media.py:71–89 的 32B 上限；只处理 DAT 解密结果，之后仍须完整解码。
    for start, end in ((b"\xff\xd8", b"\xff\xd9"),
                       (b"\x89PNG\r\n\x1a\n", b"\x00\x00\x00\x00IEND\xaeB`\x82")):
        if data.startswith(start) and not data.endswith(end):
            index = data.rfind(end)
            count = len(data) - index - len(end)
            if index > 0 and 0 < count <= 32:
                return data[:index + len(end)], count
    return data, 0


def export_image(account_dir, source_account_dir, conversation, local_id, output_dir, *,
                 shard, tier="best", xor_key=None, aes_key=None, key_provider=None,
                 aes_size_mode="ciphertext", ffmpeg="ffmpeg"):
    """不下载、不控制 UI；两种显式密钥必须同时提供，失败结果不包含密钥或正文。"""
    if (xor_key is None) != (aes_key is None):
        raise ValueError("xor_key 与 aes_key 必须同时提供")
    if not isinstance(shard, str) or not shard:
        raise ValueError("导出必须提供非空 shard")
    if aes_size_mode not in ("ciphertext", "plaintext"):
        raise ValueError("aes_size_mode 必须为 ciphertext 或 plaintext")
    identity = {
        "account": str(Path(account_dir).resolve()), "conversation": conversation,
        "table": "Msg_" + hashlib.md5(conversation.encode()).hexdigest(),
        "shard": shard, "local_id": local_id,
    }
    result = {"status": "missing", "reason": "", "identity": identity, "output_path": None, "tier": tier}

    def fail(status, reason):
        return dict(result, status=status, reason=reason)

    try:
        located = resolve_image(account_dir, source_account_dir, conversation, local_id, shard=shard, tier=tier)
    except AmbiguousMediaError:
        return fail("ambiguous", "multiple_media_candidates")
    except (FileNotFoundError, NotADirectoryError):
        return fail("missing", "account_or_shard_missing")
    except ShardReadError:
        return fail("read_failed", "message_shard_unreadable")
    result.update(identity=located["identity"], tier=located["tier"] or tier)
    if located["status"] != "ok":
        return fail(located["status"], located["reason"])
    source = Path(located["path"])
    try:
        data, signature = _read_stable(source)
    except FileNotFoundError:
        return fail("missing", "source_disappeared")
    except IncompleteImageError as exc:
        return fail("incomplete", str(exc))
    except OSError:
        return fail("read_failed", "source_unreadable")

    encrypted = data[:6] in (MAGIC_V1, MAGIC_V2)
    if encrypted:
        if len(data) < 15:
            return fail("incomplete", "dat_header_truncated")
        aes_size, xor_size = struct.unpack_from("<LL", data, 6)
        cipher_size = aes_size if aes_size_mode == "ciphertext" else (aes_size // 16 + 1) * 16
        if len(data) < 15 + cipher_size + xor_size:
            return fail("incomplete", "dat_payload_truncated")
        try:
            keys = [(xor_key, aes_key)] if aes_key is not None else (
                (key_provider or ImageKeyProvider()).keys_for(Path(source_account_dir).name)
            )
        except (OSError, ValueError):
            return fail("decrypt_failed", "key_provider_failed")
        if not keys:
            return fail("decrypt_failed", "image_key_unavailable")
        candidates = []
        for xkey, akey in keys:
            try:
                candidate, _ = dat_decrypt(data, xkey, akey, aes_size_mode=aes_size_mode)
                if candidate.startswith(b"wxgf") or _known_image(candidate):
                    candidates.append(candidate)
            except (ValueError, TypeError, OverflowError):
                continue
        if not candidates:
            return fail("decrypt_failed", "wrong_key_or_invalid_payload")
    elif _known_image(data) or data.startswith(b"wxgf"):
        candidates = [data]
    else:
        return fail("unsupported", "unknown_image_content")

    output = Path(output_dir).resolve()
    destination = output / located["output_relative_path"]
    if not destination.resolve().is_relative_to(output):
        raise ValueError("输出路径越出指定目录")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".export-", dir=destination) as temporary:
            temp = Path(temporary)
            conversion_failed = False
            for candidate in candidates:
                candidate, footer_size = _trim_dat_footer(candidate) if encrypted else (candidate, 0)
                if candidate.startswith(b"wxgf"):
                    converted = temp / "converted.jpg"
                    try:
                        if not wxgf_to_jpg(candidate, converted, ffmpeg=ffmpeg):
                            conversion_failed = True
                            continue
                        candidate = converted.read_bytes()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        conversion_failed = True
                        continue
                try:
                    extension, width, height = _validate_image(candidate)
                except (IncompleteImageError, ValueError):
                    continue
                if _signature(source.stat()) != signature:
                    return fail("incomplete", "source_changed")
                final = destination / f"{result['tier']}.{extension}"
                staged = temp / f"verified.{extension}"
                with staged.open("wb") as stream:
                    stream.write(candidate)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staged, final)
                return dict(result, status="ok", reason="", output_path=str(final),
                            width=width, height=height, format=extension,
                            trimmed_footer_bytes=footer_size)
            if conversion_failed:
                return fail("conversion_failed", "wxgf_conversion_failed")
            return fail("decrypt_failed" if encrypted else "incomplete",
                        "decrypted_image_invalid" if encrypted else "image_integrity_failed")
    except OSError:
        return fail("write_failed", "output_or_source_io_failed")


def export_media(account_dir, source_account_dir, conversation, local_id, output_dir, *,
                 shard, kind="auto", tier="best", aes_key=None, xor_key=None,
                 key_provider=None, aes_size_mode="ciphertext", ffmpeg="ffmpeg",
                 data_index=None, filename=None, emoji_hosts=None, fetch=None):
    """统一入口；图片和表情完整解码校验，其他附件只保证稳定读取和原子复制，不伪称已解码。

    emoji 是唯一会联网的类型：按消息 XML 从白名单 CDN（emoji_hosts）取回原件，fetch 可注入。
    """
    from . import emoji, media
    from .messages import MessageReader

    if kind not in {"auto", "image", "voice", "video", "file", "record-file", "emoji"}:
        raise ValueError("kind 必须是 auto/image/voice/video/file/record-file/emoji")
    if not isinstance(shard, str) or not shard:
        raise ValueError("导出必须提供非空 shard")
    identity = {
        "account": str(Path(account_dir).resolve()), "conversation": conversation,
        "table": "Msg_" + hashlib.md5(conversation.encode()).hexdigest(),
        "shard": shard, "local_id": local_id,
    }
    result = {"status": "missing", "reason": "", "identity": identity,
              "output_path": None, "tier": None}

    def fail(status, reason):
        return dict(result, status=status, reason=reason, kind=kind)

    if kind in ("auto", "emoji"):
        try:
            row = MessageReader(account_dir).get_message(conversation, local_id, shard=shard)
        except (FileNotFoundError, NotADirectoryError):
            return fail("missing", "account_or_shard_missing")
        except ShardReadError:
            return fail("read_failed", "message_shard_unreadable")
        if row is None:
            return fail("missing", "message_not_found")
    if kind == "auto":
        message_type = row["message_type"]
        kind = {3: "image", 34: "voice", 43: "video", 47: "emoji", 62: "video"}.get(message_type.base_type)
        if kind is None and message_type.category == "file":
            kind = "file"
        if kind is None:
            return fail("unsupported", "message_has_no_supported_media")
    if kind == "emoji":
        return emoji.export_emoji(row, output_dir, hosts=emoji_hosts, fetch=fetch)
    if kind == "image":
        return dict(export_image(
            account_dir, source_account_dir, conversation, local_id, output_dir,
            shard=shard, tier=tier, xor_key=xor_key, aes_key=aes_key,
            key_provider=key_provider, aes_size_mode=aes_size_mode, ffmpeg=ffmpeg,
        ), kind="image")
    try:
        if kind == "voice":
            located = media.read_voice(account_dir, conversation, local_id, shard=shard)
        elif kind == "record-file":
            if data_index is None or filename is None:
                raise ValueError("record-file 必须提供 data_index 和 filename")
            located = media.resolve_record_file(
                account_dir, source_account_dir, conversation, local_id,
                shard=shard, data_index=data_index, filename=filename,
            )
        else:
            resolver = media.resolve_video if kind == "video" else media.resolve_file
            located = resolver(account_dir, source_account_dir, conversation, local_id, shard=shard)
    except AmbiguousMediaError:
        return fail("ambiguous", "multiple_media_candidates")
    except (FileNotFoundError, NotADirectoryError):
        return fail("missing", "account_or_shard_missing")
    except ShardReadError:
        return fail("read_failed", "message_shard_unreadable")
    result["identity"] = located["identity"]
    if located["status"] != "ok":
        return fail(located["status"], located["reason"])
    source = None
    if kind == "voice":
        data = located.get("data")
        if not isinstance(data, bytes) or not data:
            return fail("incomplete", "empty_voice_data")
        if not data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
            return fail("unsupported", "voice_is_not_silk")
        name = "voice.silk"
    else:
        source = Path(located["path"])
        name = source.name
        try:
            data, signature = _read_stable(source)
        except FileNotFoundError:
            return fail("missing", "source_disappeared")
        except IncompleteImageError as exc:
            return fail("incomplete", str(exc))
        except OSError:
            return fail("read_failed", "source_unreadable")
    output = Path(output_dir).resolve()
    destination = output / located["output_relative_path"]
    if kind == "record-file":
        # 一条合并转发中可包含多个同名附件，index 也是输出身份的一部分。
        destination = destination / f"record-{data_index}"
    if not destination.resolve().is_relative_to(output):
        raise ValueError("输出路径越出指定目录")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".export-", dir=destination) as temporary:
            staged = Path(temporary) / "payload"
            with staged.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if source is not None and _signature(source.stat()) != signature:
                return fail("incomplete", "source_changed")
            final = destination / name
            os.replace(staged, final)
        return dict(result, status="ok", reason="", kind=kind, output_path=str(final),
                    validation="silk_header_only" if kind == "voice" else "stable_copy_only")
    except OSError:
        return fail("write_failed", "output_or_source_io_failed")
