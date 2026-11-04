"""全部使用合成图片、SQLite 和附件目录。"""

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
import pytest

from wechat_extract import export_media as module
from wechat_extract.wxdat import MAGIC_V2


def picture(color="red", fmt="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (4, 3), color).save(stream, fmt)
    return stream.getvalue()


def encrypted(data, key=b"0123456789abcdef"):
    padded = data + bytes([16 - len(data) % 16]) * (16 - len(data) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()
    return MAGIC_V2 + struct.pack("<LLB", len(cipher), 0, 1) + cipher


def fixture(tmp_path, shard="message_0.db", resource="a" * 32, suffix="_h", data=None):
    account, source = tmp_path / "plain", tmp_path / "wxid_example_hash"
    account.mkdir(exist_ok=True)
    table = "Msg_" + hashlib.md5(b"room").hexdigest()
    with sqlite3.connect(account / shard) as conn:
        conn.execute(f'CREATE TABLE "{table}" (local_id INTEGER,local_type INTEGER,sort_seq INTEGER,create_time INTEGER,message_content TEXT)')
        conn.execute(f'INSERT INTO "{table}" VALUES(1,3,1,0,?)', (f'<msg><img md5="{resource}"/></msg>',))
    path = source / "msg" / "attach" / hashlib.md5(b"room").hexdigest() / "2026-10" / "Img" / (resource + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(picture() if data is None else data)
    return account, source, path


def export(tmp_path, account, source, **kwargs):
    return module.export_image(account, source, "room", 1, tmp_path / "out",
                               shard=kwargs.pop("shard", "message_0.db"), **kwargs)


def test_two_shards_same_local_id_different_small_plain_images(tmp_path):
    account, source, _ = fixture(tmp_path)
    fixture(tmp_path, "message_1.db", "b" * 32, data=picture("blue"))
    left = export(tmp_path, account, source)
    right = export(tmp_path, account, source, shard="message_1.db")
    assert left["status"] == right["status"] == "ok"
    assert left["output_path"] != right["output_path"]
    assert Path(left["output_path"]).read_bytes() != Path(right["output_path"]).read_bytes()
    assert left["width"] == 4 and left["height"] == 3
    assert left["tier"] == "original"
    json.dumps(left)


def test_thumbnail_explicit_and_no_original_downgrade(tmp_path):
    account, source, _ = fixture(tmp_path, suffix="_t.dat")
    assert export(tmp_path, account, source, tier="original")["status"] == "missing"
    result = export(tmp_path, account, source, tier="thumb")
    assert result["status"] == "ok" and result["tier"] == "thumb"


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_truncation_never_replaces_valid_output(tmp_path, fmt):
    account, source, path = fixture(tmp_path, data=picture(fmt=fmt))
    result = export(tmp_path, account, source)
    output = Path(result["output_path"])
    before = output.read_bytes()
    path.write_bytes(path.read_bytes()[:-5])
    failed = export(tmp_path, account, source)
    assert failed["status"] == "incomplete"
    assert output.read_bytes() == before


def test_changed_during_read_is_incomplete(tmp_path, monkeypatch):
    account, source, path = fixture(tmp_path)
    original = Path.read_bytes
    calls = 0

    def changing(self):
        nonlocal calls
        data = original(self)
        if self == path:
            calls += 1
            if calls == 1:
                self.write_bytes(picture("blue"))
        return data

    monkeypatch.setattr(Path, "read_bytes", changing)
    assert export(tmp_path, account, source)["reason"] == "source_changed"


def test_decrypt_wrong_key_unknown_and_incomplete_dat(tmp_path):
    account, source, path = fixture(tmp_path, data=encrypted(picture()))
    assert export(tmp_path, account, source, xor_key=0, aes_key=b"0123456789abcdef")["status"] == "ok"
    result = export(tmp_path, account, source, xor_key=0, aes_key=b"wrong-key-1234567")
    assert result["status"] == "decrypt_failed"
    assert "wrong-key" not in json.dumps(result)
    path.write_bytes(path.read_bytes()[:-8])
    assert export(tmp_path, account, source, xor_key=0, aes_key=b"0123456789abcdef")["status"] == "incomplete"
    path.write_bytes(b"unknown payload")
    assert export(tmp_path, account, source)["status"] == "unsupported"
    with pytest.raises(ValueError):
        export(tmp_path, account, source, xor_key=0)


def test_provider_and_wxgf_conversion_must_validate(tmp_path, monkeypatch):
    account, source, path = fixture(tmp_path, data=encrypted(picture()))

    class Provider:
        def keys_for(self, wxid):
            assert wxid == source.name
            return [(0, b"badbadbadbadbadb"), (0, b"0123456789abcdef")]

    assert export(tmp_path, account, source, key_provider=Provider())["status"] == "ok"
    path.write_bytes(b"wxgf" + b"synthetic")
    monkeypatch.setattr(module, "wxgf_to_jpg", lambda *a, **k: False)
    assert export(tmp_path, account, source)["status"] == "conversion_failed"

    def invalid(_data, target, **kwargs):
        Path(target).write_bytes(b"\xff\xd8\xffjunk\xff\xd9")
        return True

    monkeypatch.setattr(module, "wxgf_to_jpg", invalid)
    assert export(tmp_path, account, source)["status"] == "incomplete"

    def valid(_data, target, **kwargs):
        Path(target).write_bytes(picture(fmt="JPEG"))
        return True

    monkeypatch.setattr(module, "wxgf_to_jpg", valid)
    result = export(tmp_path, account, source)
    assert result["status"] == "ok" and result["format"] == "jpg"


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_dat_footer_is_bounded_and_requires_full_decode(tmp_path, fmt):
    image = picture(fmt=fmt)
    account, source, path = fixture(tmp_path, data=encrypted(image + b"F" * 24))
    result = export(tmp_path, account, source, xor_key=0, aes_key=b"0123456789abcdef")
    assert result["status"] == "ok" and result["trimmed_footer_bytes"] == 24
    assert Path(result["output_path"]).read_bytes() == image
    path.write_bytes(encrypted(image + b"F" * 33))
    assert export(tmp_path, account, source, xor_key=0, aes_key=b"0123456789abcdef")["status"] == "decrypt_failed"
    path.write_bytes(encrypted(b"\xff\xd8\xffjunk\xff\xd9" + b"F" * 24))
    assert export(tmp_path, account, source, xor_key=0, aes_key=b"0123456789abcdef")["status"] == "decrypt_failed"


def test_auto_image_dispatch(tmp_path):
    account, source, _ = fixture(tmp_path)
    result = module.export_media(account, source, "room", 1, tmp_path / "out", shard="message_0.db")
    assert result["status"] == "ok" and result["kind"] == "image"


@pytest.mark.parametrize("kind", ["voice", "video", "file", "record-file"])
def test_generic_export_atomic_copy_and_json(tmp_path, monkeypatch, kind):
    from wechat_extract import media

    account, source, path = fixture(tmp_path)
    identity = {"account": str(account), "conversation": "room", "table": "Msg_synthetic",
                "shard": "message_0.db", "local_id": 1}
    payload = b"\x02#!SILK_V3synthetic" if kind == "voice" else b"synthetic attachment"
    path.write_bytes(payload)
    located = {"status": "ok", "reason": "", "identity": identity, "path": path,
               "data": payload, "output_relative_path": Path("account/message_0.db/table/1")}
    target = {"voice": "read_voice", "video": "resolve_video", "file": "resolve_file",
              "record-file": "resolve_record_file"}[kind]
    monkeypatch.setattr(media, target, lambda *args, **kwargs: located)
    result = module.export_media(account, source, "room", 1, tmp_path / "out",
                                 shard="message_0.db", kind=kind, data_index=0, filename="any")
    assert result["status"] == "ok"
    assert Path(result["output_path"]).read_bytes() == payload
    assert "data" not in result
    json.dumps(result)
    if kind == "record-file":
        assert "record-0" in result["output_path"]
