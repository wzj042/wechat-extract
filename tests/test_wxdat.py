import struct
import hashlib
import os

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_extract.wxdat import MAGIC_V2, dat_decrypt, derive_image_keys
from wechat_extract import wxdat


def test_v2_roundtrip_and_truncated_tail():
    xor_key, aes_key = derive_image_keys(12345, "wxid_example_abcd")
    plain = b"\xff\xd8\xff" + b"image" * 6
    padding = 16 - len(plain) % 16
    encryptor = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).encryptor()
    encrypted = encryptor.update(plain + bytes([padding]) * padding) + encryptor.finalize()
    tail = b"tail"
    data = (MAGIC_V2 + struct.pack("<LLB", len(encrypted), len(tail), 1)
            + encrypted + bytes(b ^ xor_key for b in tail))
    decoded, meta = dat_decrypt(data, xor_key, aes_key)
    assert decoded == plain + tail
    assert meta["ext"] == "jpg"
    with pytest.raises(ValueError, match="XOR"):
        dat_decrypt(data[:-1], xor_key, aes_key)


@pytest.mark.parametrize("ok,fail,expected", [(1, 0, 0), (1, 1, 1), (0, 0, 1)])
def test_cli_exit_status(monkeypatch, tmp_path, ok, fail, expected):
    class SyntheticPipeline:
        def __init__(self, **kwargs):
            pass

        def decrypt_chat(self, *args):
            return ok, fail, tmp_path

    monkeypatch.setattr(wxdat, "ImagePipeline", SyntheticPipeline)
    assert wxdat.main(["--wxid-dir", "wxid_example", "--chat", "example@chatroom"]) == expected


def test_explicit_custom_account_exports_duplicate_names_by_month(tmp_path, monkeypatch):
    account = tmp_path / "custom_disk" / "xwechat_files" / "wxid_example_abcd"
    chat = "example@chatroom"
    chat_base = account / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest()
    for month in ("2026-09", "2026-10"):
        source = chat_base / month / "Img" / "same.dat"
        source.parent.mkdir(parents=True)
        source.write_bytes(month.encode())
    profile = tmp_path / "empty_profile"
    profile.mkdir()
    pipeline = wxdat.ImagePipeline(userprofile=str(profile), account_dir=account)
    used_accounts = []

    def keys_for(name):
        used_accounts.append(name)
        return [(1, b"synthetic key")]

    monkeypatch.setattr(pipeline.kp, "keys_for", keys_for)
    monkeypatch.setattr(pipeline, "_decrypt_one",
                        lambda data, keys, **kwargs: (data, {"isWxgf": False, "ext": "jpg"}))
    out = tmp_path / "out"
    assert pipeline.decrypt_chat(chat, None, out)[:2] == (2, 0)
    assert used_accounts == ["wxid_example_abcd"]
    source_id = hashlib.sha256(os.path.normcase(str(account)).encode()).hexdigest()[:8]
    for month in ("2026-09", "2026-10"):
        assert (out / f"{account.name}-{source_id}" / chat_base.name / month / "Img" / "same.jpg").read_bytes() == month.encode()


def test_legacy_userprofile_argument_finds_profile_and_documents(tmp_path):
    account = tmp_path / "xwechat_files" / "wxid_example_abcd"
    (account / "msg" / "attach").mkdir(parents=True)
    pipeline = wxdat.ImagePipeline(userprofile=str(tmp_path))
    assert pipeline.attach_root(account.name) == account / "msg" / "attach"
    duplicate = tmp_path / "Documents" / "xwechat_files" / account.name
    (duplicate / "db_storage").mkdir(parents=True)
    with pytest.raises(ValueError, match="多个账号"):
        pipeline.resolve_account(account.name)
    explicit = wxdat.ImagePipeline(userprofile=str(tmp_path), account_dir=account)
    assert explicit.resolve_account() == account


def test_cli_accepts_account_path_without_wxid(tmp_path, monkeypatch):
    calls = []

    class SyntheticPipeline:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def decrypt_chat(self, *args):
            calls.append(args)
            return 1, 0, tmp_path

    monkeypatch.setattr(wxdat, "ImagePipeline", SyntheticPipeline)
    assert wxdat.main(["--account-dir", str(tmp_path), "--chat", "example@chatroom"]) == 0
    assert calls[0]["account_dir"] == str(tmp_path)
    assert calls[1][1] is None


@pytest.mark.parametrize("mode,plain_size", [("ciphertext", 29), ("plaintext", 29), ("plaintext", 32)])
@pytest.mark.parametrize("tail", [b"", b"xor tail"])
def test_both_header_layouts_keep_raw_middle(mode, plain_size, tail):
    key = b"0123456789abcdef"
    plain = b"\xff\xd8\xff" + b"a" * (plain_size - 3)
    padding = 16 - len(plain) % 16
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(plain + bytes([padding]) * padding) + encryptor.finalize()
    declared_size = len(encrypted) if mode == "ciphertext" else len(plain)
    middle = b"raw bytes which must never disappear"
    data = (MAGIC_V2 + struct.pack("<LLB", declared_size, len(tail), 1)
            + encrypted + middle + bytes(b ^ 0x88 for b in tail))
    decoded, meta = dat_decrypt(data, 0x88, key, aes_size_mode=mode)
    assert decoded == plain + middle + tail
    assert meta["rawSize"] == len(middle)
    automatic, detected = dat_decrypt(data, 0x88, key)
    assert automatic == plain + middle + tail
    assert detected["aesSizeMode"] == mode


def test_plaintext_header_requires_matching_padding():
    key = b"0123456789abcdef"
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(b"x" * 32) + encryptor.finalize()
    data = MAGIC_V2 + struct.pack("<LLB", 31, 0, 1) + encrypted
    with pytest.raises(ValueError, match="PKCS7"):
        dat_decrypt(data, 0x88, key, aes_size_mode="plaintext")
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(b"x" * 30 + b"\x02\x02") + encryptor.finalize()
    data = MAGIC_V2 + struct.pack("<LLB", 31, 0, 1) + encrypted
    with pytest.raises(ValueError, match="明文长度"):
        dat_decrypt(data, 0x88, key, aes_size_mode="plaintext")


def test_exports_are_isolated_by_source_and_chat(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    output = tmp_path / "export"
    for source in ("disk_a", "disk_b"):
        account = tmp_path / source / "wxid_example"
        for chat in ("chat_a", "chat_b"):
            chat_hash = hashlib.md5(chat.encode()).hexdigest()
            dat = account / "msg" / "attach" / chat_hash / "2026-10" / "Img" / "same.dat"
            dat.parent.mkdir(parents=True)
            dat.write_bytes(f"{source}/{chat}".encode())
            pipeline = wxdat.ImagePipeline(userprofile=str(profile), account_dir=account)
            monkeypatch.setattr(pipeline.kp, "keys_for", lambda _: [(1, b"synthetic")])
            monkeypatch.setattr(pipeline, "_decrypt_one",
                                lambda data, keys, **kwargs: (data, {"isWxgf": False, "ext": "jpg"}))
            assert pipeline.decrypt_chat(chat, None, output)[:2] == (1, 0)
    exported = list(output.rglob("same.jpg"))
    assert len(exported) == 4
    assert {p.read_bytes() for p in exported} == {
        f"{source}/{chat}".encode() for source in ("disk_a", "disk_b") for chat in ("chat_a", "chat_b")
    }
