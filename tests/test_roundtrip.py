"""Round-trip test: build a synthetic SQLCipher 4.0 / WCDB-encrypted database
with the same parameters the WeChat 4.x decryptor expects, then decrypt it
back with ``WeChatDatabaseDecryptor`` and verify the contents.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_extract.wechat_decrypt import (
    HMAC_SIZE,
    IV_SIZE,
    PAGE_SIZE,
    RESERVE_SIZE,
    SALT_SIZE,
    WeChatDatabaseDecryptor,
    _compute_page_hmac,
    _derive_mac_key,
    _derive_sqlcipher_enc_key,
)

ROWS = [(i, f"message-{i}", f"content {i} 中文内容") for i in range(50)]


@pytest.fixture(autouse=True)
def _isolate_runtime_dirs(tmp_path, monkeypatch):
    """Keep logs/output produced by the copied logging setup inside tmp_path."""
    monkeypatch.setenv("WECHAT_TOOL_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WECHAT_TOOL_BUILD_SESSION_LAST_MESSAGE", "0")


def _create_plain_sqlite(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA page_size={PAGE_SIZE}")
        conn.execute(
            "CREATE TABLE message (id INTEGER PRIMARY KEY, talker TEXT, content TEXT)"
        )
        conn.executemany(
            "INSERT INTO message (id, talker, content) VALUES (?, ?, ?)", ROWS
        )
        conn.commit()
    finally:
        conn.close()

    # Real WCDB/SQLCipher plaintext databases reserve RESERVE_SIZE bytes at the
    # end of every page (that is where the IV + HMAC live in encrypted form),
    # so B-tree cells never occupy the page tail. The decryptor zero-fills the
    # reserve tail of every output page, so the synthetic plaintext must do the
    # same or the schema records (stored at the page end) get corrupted.
    # Setting the reserved-space header byte (offset 20) and running VACUUM
    # makes SQLite rebuild every page with a zeroed reserve tail.
    data = bytearray(path.read_bytes())
    data[20] = RESERVE_SIZE
    path.write_bytes(bytes(data))

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()

    rebuilt = path.read_bytes()
    for page_start in range(0, len(rebuilt), PAGE_SIZE):
        tail = rebuilt[page_start + PAGE_SIZE - RESERVE_SIZE : page_start + PAGE_SIZE]
        assert tail == b"\x00" * RESERVE_SIZE, "page reserve tail not zero after VACUUM"


def _encrypt_page(
    enc_key: bytes, mac_key: bytes, plain_page: bytes, page_num: int, salt: bytes
) -> bytes:
    """Encrypt one plaintext page exactly like SQLCipher 4.0 / WCDB does."""
    offset = SALT_SIZE if page_num == 1 else 0
    iv = os.urandom(IV_SIZE)
    plaintext = plain_page[offset : PAGE_SIZE - RESERVE_SIZE]
    assert len(plaintext) % 16 == 0

    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()

    page = bytearray(PAGE_SIZE)
    if page_num == 1:
        page[:SALT_SIZE] = salt
    page[offset : offset + len(ciphertext)] = ciphertext
    page[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE - RESERVE_SIZE + IV_SIZE] = iv

    page_bytes = bytes(page)
    page_hmac = _compute_page_hmac(mac_key, page_bytes, page_num)
    return page_bytes[: PAGE_SIZE - HMAC_SIZE] + page_hmac


def _build_encrypted_db(plain_path: Path, encrypted_path: Path, key_material: bytes) -> bytes:
    """Encrypt a plaintext SQLite file with SQLCipher 4.0 / WCDB parameters.

    Returns the salt used (stored in the first 16 bytes of page 1).
    """
    plain_data = plain_path.read_bytes()
    assert len(plain_data) >= PAGE_SIZE
    assert len(plain_data) % PAGE_SIZE == 0

    salt = os.urandom(SALT_SIZE)
    # SQLCipher passphrase mode: enc_key derived via PBKDF2-SHA512 x256000.
    enc_key = _derive_sqlcipher_enc_key(key_material, salt)
    mac_key = _derive_mac_key(enc_key, salt)

    total_pages = len(plain_data) // PAGE_SIZE
    with encrypted_path.open("wb") as f:
        for page_num in range(1, total_pages + 1):
            plain_page = plain_data[(page_num - 1) * PAGE_SIZE : page_num * PAGE_SIZE]
            f.write(_encrypt_page(enc_key, mac_key, plain_page, page_num, salt))
    return salt


def test_key_derivation():
    key_material = bytes(range(32))
    salt = bytes(range(16))

    enc_key = _derive_sqlcipher_enc_key(key_material, salt)
    assert enc_key == hashlib.pbkdf2_hmac("sha512", key_material, salt, 256000, dklen=32)

    mac_key = _derive_mac_key(enc_key, salt)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    assert mac_key == hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    assert len(mac_key) == 32


def test_decrypt_roundtrip(tmp_path):
    key_hex = "ab" * 32  # 64-char hex key
    plain_path = tmp_path / "plain.db"
    encrypted_path = tmp_path / "message_0.db"
    output_path = tmp_path / "out" / "message_0.db"
    output_path.parent.mkdir(parents=True)

    _create_plain_sqlite(plain_path)
    _build_encrypted_db(plain_path, encrypted_path, bytes.fromhex(key_hex))

    decryptor = WeChatDatabaseDecryptor(key_hex)
    ok = decryptor.decrypt_database(str(encrypted_path), str(output_path))
    assert ok, decryptor.last_result.get("error")
    assert decryptor.last_result["key_mode"] == "sqlcipher_passphrase"
    assert decryptor.last_result["failed_pages"] == 0
    assert decryptor.last_result["diagnostic_status"] == "ok"

    conn = sqlite3.connect(str(output_path))
    try:
        rows = conn.execute(
            "SELECT id, talker, content FROM message ORDER BY id"
        ).fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()

    assert rows == ROWS
    assert integrity == "ok"


def test_decrypt_wrong_key_fails(tmp_path):
    key_hex = "cd" * 32
    plain_path = tmp_path / "plain.db"
    encrypted_path = tmp_path / "message_0.db"
    output_path = tmp_path / "out.db"

    _create_plain_sqlite(plain_path)
    _build_encrypted_db(plain_path, encrypted_path, bytes.fromhex(key_hex))

    wrong = WeChatDatabaseDecryptor("ef" * 32)
    ok = wrong.decrypt_database(str(encrypted_path), str(output_path))
    assert not ok
    assert wrong.last_result["error"] == "key_mismatch"
    assert not output_path.exists()


def test_decrypt_rejects_bad_key_format():
    with pytest.raises(ValueError):
        WeChatDatabaseDecryptor("too-short")
    with pytest.raises(ValueError):
        WeChatDatabaseDecryptor("zz" * 32)


def test_decrypt_plain_sqlite_is_copied(tmp_path):
    plain_path = tmp_path / "plain.db"
    output_path = tmp_path / "out.db"
    _create_plain_sqlite(plain_path)

    decryptor = WeChatDatabaseDecryptor("ab" * 32)
    ok = decryptor.decrypt_database(str(plain_path), str(output_path))
    assert ok
    assert decryptor.last_result["copied_as_sqlite"] is True
    assert output_path.read_bytes() == plain_path.read_bytes()
