"""动画表情导出：全部使用合成图片、SQLite 和假下载函数，不联网。"""

import asyncio
import hashlib
import io
import json
from pathlib import Path
import sqlite3

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
import pytest

from wechat_extract import emoji as module
from wechat_extract import mcp_server
from wechat_extract.export_media import export_media
from wechat_extract.messages import MessageReader

AESKEY = "0123456789abcdef0123456789abcdef"
CDN = "http://vweixinf.tc.qq.com/110/20401/stodownload?m={md5}&amp;filekey=plain"
ENC = "http://vweixinf.tc.qq.com/110/20402/stodownload?m={md5}&amp;filekey=cipher"


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGBA", (6, 5), color).save(stream, "PNG")
    return stream.getvalue()


def gif(count=8):
    frames = [Image.new("RGB", (10, 8), (i * 30, 255 - i * 30, 40)) for i in range(count)]
    stream = io.BytesIO()
    frames[0].save(stream, "GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    return stream.getvalue()


def encrypt(data, key=AESKEY):
    raw = bytes.fromhex(key)
    pad = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(raw), modes.CBC(raw)).encryptor()
    return encryptor.update(data + bytes([pad]) * pad) + encryptor.finalize()


def emoji_xml(data, cdn=CDN, enc=ENC, prefix="wxid_sender:\n"):
    md5 = hashlib.md5(data).hexdigest()
    return (f'{prefix}<msg><emoji fromusername="wxid_sender" md5="{md5}" len="{len(data)}" '
            f'cdnurl="{cdn.format(md5=md5)}" encrypturl="{enc.format(md5=md5)}" aeskey="{AESKEY}" '
            f'externurl="http://vweixinf.tc.qq.com/extern" width="6" height="5"/></msg>')


def plain_url(data):
    return CDN.format(md5=hashlib.md5(data).hexdigest()).replace("&amp;", "&")


def cipher_url(data):
    return ENC.format(md5=hashlib.md5(data).hexdigest()).replace("&amp;", "&")


class Fetcher:
    """按 URL 返回预置字节；值为异常时抛出。记录每次调用。"""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        value = self.responses.get(url, OSError("not found"))
        if isinstance(value, Exception):
            raise value
        return value


def add(account, text, local_id=1, local_type=47, chat="room"):
    account.mkdir(exist_ok=True)
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    with sqlite3.connect(account / "message_0.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS Name2Id (user_name TEXT)")
        if not conn.execute("SELECT 1 FROM Name2Id").fetchone():
            conn.execute("INSERT INTO Name2Id VALUES (?)", (chat,))
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,sort_seq INTEGER,message_content TEXT)')
        conn.execute(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?)', (local_id, local_id + 100, local_type, 10, local_id, text))


def row_of(account, local_id=1):
    return MessageReader(account).get_message("room", local_id, shard="message_0.db")


def export(tmp_path, fetch, local_id=1, **kwargs):
    return module.export_emoji(row_of(tmp_path / "plain", local_id), tmp_path / "out", fetch=fetch, **kwargs)


def test_parse_group_prefix_and_entities():
    data = png()
    info = module.parse_emoji(emoji_xml(data))
    assert info["md5"] == hashlib.md5(data).hexdigest() and info["len"] == len(data)
    assert info["cdnurl"] == plain_url(data) and "&filekey=plain" in info["cdnurl"]
    assert info["encrypturl"] == cipher_url(data) and info["aeskey"] == AESKEY
    assert (info["width"], info["height"]) == (6, 5)
    assert module.parse_emoji(emoji_xml(data, prefix=""))["md5"] == info["md5"]
    for bad in ("not xml", "<msg><emoji/></msg>", '<msg><emoji md5="../x"/></msg>', "<msg><img md5=\"a\"/></msg>"):
        with pytest.raises(ValueError):
            module.parse_emoji(bad)


def test_cdn_success_static_png_preview_is_original(tmp_path):
    data = png()
    add(tmp_path / "plain", emoji_xml(data))
    fetch = Fetcher({plain_url(data): data})
    result = export(tmp_path, fetch)
    assert result["status"] == "ok" and result["source"] == "cdn" and result["kind"] == "emoji"
    assert Path(result["output_path"]).name == "emoji.png"
    assert Path(result["output_path"]).read_bytes() == data
    assert result["preview_path"] == result["output_path"]
    assert result["animated"] is False and result["frames"] == 1 and "duration_ms" not in result
    assert (result["format"], result["width"], result["height"]) == ("png", 6, 5)
    assert fetch.calls == [plain_url(data)]
    # 输出必须落在 media_output_relative_path 规定的消息目录下。
    relative = Path(result["output_path"]).relative_to((tmp_path / "out").resolve())
    assert relative.parts[1:] == ("message_0.db", row_of(tmp_path / "plain")["table"], "1", "emoji.png")
    json.dumps(result)


def test_cdn_mismatch_falls_back_to_encrypted(tmp_path):
    data = png()
    add(tmp_path / "plain", emoji_xml(data))
    fetch = Fetcher({plain_url(data): png("blue"), cipher_url(data): encrypt(data)})
    result = export(tmp_path, fetch)
    assert result["status"] == "ok" and result["source"] == "encrypted_cdn"
    assert Path(result["output_path"]).read_bytes() == data
    assert fetch.calls == [plain_url(data), cipher_url(data)]


def test_both_sources_fail_without_leaking(tmp_path):
    data = png()
    add(tmp_path / "plain", emoji_xml(data))
    fetch = Fetcher({plain_url(data): OSError(plain_url(data)), cipher_url(data): b"x" * 32})
    result = export(tmp_path, fetch)
    assert result["status"] == "decrypt_failed" and result["output_path"] is None
    text = json.dumps(result)
    assert AESKEY not in text and "stodownload" not in text and "wxid_sender" not in text
    fetch = Fetcher({plain_url(data): OSError("boom")})
    assert export(tmp_path, fetch)["status"] == "download_failed"
    assert not (tmp_path / "out" / module.CACHE_DIR).exists()


def test_host_not_in_allowlist_is_rejected(tmp_path):
    data = png()
    evil = "http://evil.example.com/{md5}"
    add(tmp_path / "plain", emoji_xml(data, cdn=evil, enc=evil))
    fetch = Fetcher({})
    result = export(tmp_path, fetch)
    assert result["status"] == "download_failed" and result["reason"] == "emoji_host_not_allowed"
    assert fetch.calls == []
    # 配置可覆盖白名单。
    url = evil.format(md5=hashlib.md5(data).hexdigest())
    assert export(tmp_path, Fetcher({url: data}), hosts=["example.com"])["status"] == "ok"
    assert module.allowed("https://wxapp.tc.qq.com/x") and module.allowed("http://a.qpic.cn/x")
    for bad in ("ftp://a.qq.com/x", "http://qq.com.evil.cn/x", "http://evilqq.com/x",
                "http://a.qq.com@evil.cn/x", "file:///etc/passwd"):
        assert not module.allowed(bad)
    with pytest.raises(ValueError):
        module.download("http://evil.example.com/x")
    with pytest.raises(ValueError):
        module._Redirect(None).redirect_request(None, None, 302, "", {}, "http://evil.example.com/x")


def test_cache_hit_skips_download(tmp_path):
    data = png()
    add(tmp_path / "plain", emoji_xml(data))
    add(tmp_path / "plain", emoji_xml(data), local_id=2)
    assert export(tmp_path, Fetcher({plain_url(data): data}))["source"] == "cdn"
    cached = tmp_path / "out" / module.CACHE_DIR / f"{hashlib.md5(data).hexdigest()}.png"
    assert cached.read_bytes() == data
    fetch = Fetcher({})
    result = export(tmp_path, fetch, local_id=2)
    assert result["status"] == "ok" and result["source"] == "cache" and fetch.calls == []
    assert Path(result["output_path"]).parent.name == "2"
    # 缓存被篡改时不信任，重新下载。
    cached.write_bytes(png("blue"))
    assert export(tmp_path, Fetcher({plain_url(data): data}))["source"] == "cdn"


def test_animated_gif_preview(tmp_path):
    data = gif(8)
    add(tmp_path / "plain", emoji_xml(data))
    result = export(tmp_path, Fetcher({plain_url(data): data}))
    assert result["status"] == "ok" and result["format"] == "gif"
    assert result["animated"] is True and result["frames"] == 8 and result["preview_frames"] == 6
    assert result["duration_ms"] == 400
    preview = Path(result["preview_path"])
    assert preview.name == "preview.png" and preview.parent == Path(result["output_path"]).parent
    with Image.open(preview) as image:
        assert image.format == "PNG"
        gap = module.PREVIEW_GAP
        assert image.size == (3 * 10 + 4 * gap, 2 * 8 + 3 * gap)
        # 帧按时间顺序排列，帧间是灰色间隔。
        assert image.convert("RGB").getpixel((0, 0)) == (200, 200, 200)
        assert image.convert("RGB").getpixel((gap + 5, gap + 4))[0] < image.convert("RGB").getpixel((gap * 2 + 15, gap + 4))[0]


def test_auto_maps_47_to_emoji(tmp_path, monkeypatch):
    data = png()
    add(tmp_path / "plain", emoji_xml(data))
    add(tmp_path / "plain", "<msg/>", local_id=2, local_type=1)
    monkeypatch.setattr(module, "download", Fetcher({plain_url(data): data}))
    result = export_media(tmp_path / "plain", tmp_path / "source", "room", 1, tmp_path / "out",
                          shard="message_0.db")
    assert result["status"] == "ok" and result["kind"] == "emoji"
    explicit = export_media(tmp_path / "plain", tmp_path / "source", "room", 2, tmp_path / "out",
                            shard="message_0.db", kind="emoji")
    assert explicit["status"] == "unsupported" and explicit["reason"] == "not_emoji_message"


class Queries:
    def resolve_chat(self, name):
        return "room"


class Runtime:
    def __init__(self, root):
        self.account_dir = root / "plain"
        self.source_account_dir = root / "source"
        self.output_dir = root / "out"
        self.config = {"agent_path_map": {str(self.output_dir): "/agent/media"}}
        self.queries = Queries()

    def refresh(self, scope="messages"):
        return {"status": "ok"}


def call(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    return json.loads(result[0].text)


def test_mcp_decode_emoji_uses_preview(tmp_path, monkeypatch):
    data = gif(4)
    add(tmp_path / "plain", emoji_xml(data))
    monkeypatch.setattr(module, "download", Fetcher({plain_url(data): data}))
    server = mcp_server.create_server(Runtime(tmp_path))
    decoded = call(server, "decode_image", chat_name="r", local_id=1)
    assert decoded["status"] == "ok" and decoded["kind"] == "emoji"
    assert decoded["animated"] is True and decoded["frames"] == 4
    assert Path(decoded["preview_path"]).name == "preview.png"
    assert decoded["agent_path"].startswith("/agent/media/") and decoded["agent_path"].endswith("/emoji.gif")
    assert decoded["preview_agent_path"].endswith("/preview.png")
    assert AESKEY not in json.dumps(decoded)
