"""CLI 的成功退出码必须代表整个批次完成。"""

import sqlite3
import hashlib
import json

from wechat_extract.cli import main


def test_decrypt_partial_failure_has_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.delenv("WECHAT_TOOL_OUTPUT_DIR", raising=False)
    source = tmp_path / "wxid_example" / "db_storage" / "message"
    source.mkdir(parents=True)
    with sqlite3.connect(source / "good.db") as connection:
        connection.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY)")
    (source / "bad.db").write_bytes(b"broken")
    assert main([
        "decrypt", "--key", "ab" * 32,
        "--input", str(source.parent), "--output", str(tmp_path / "out"),
    ]) == 1
    (source / "bad.db").unlink()
    assert main([
        "decrypt", "--key", "ab" * 32,
        "--input", str(source.parent), "--output", str(tmp_path / "out2"),
    ]) == 0


def test_messages_cli_serializes_identity_and_rejects_ambiguous_id(tmp_path, capsys):
    chat = "cli@chatroom"
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    for number in range(2):
        with sqlite3.connect(tmp_path / f"message_{number}.db") as conn:
            conn.execute(f'CREATE TABLE "{table}" (local_id INTEGER, sort_seq INTEGER, local_type INTEGER, message_content BLOB)')
            conn.execute(f'INSERT INTO "{table}" VALUES (1, 10, 1, ?)', (b"hello",))
    argv = ["messages", "--account-dir", str(tmp_path), "--chat", chat]
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["shard"] for row in result] == ["message_0.db", "message_1.db"]
    assert result[0]["content_text"] == "hello"
    assert result[0]["message_type"]["category"] == "text"
    assert main([*argv, "--local-id", "1"]) == 1
    assert "shard" in capsys.readouterr().err
    assert main([*argv, "--local-id", "1", "--shard", "message_1.db"]) == 0
    assert json.loads(capsys.readouterr().out)["shard"] == "message_1.db"


def test_image_path_cli_keeps_shard_and_serializes_path(tmp_path, capsys):
    chat = "images@chatroom"
    chat_hash = hashlib.md5(chat.encode()).hexdigest()
    account, source = tmp_path / "decrypted", tmp_path / "source"
    account.mkdir()
    image_id = "d" * 32
    with sqlite3.connect(account / "message_2.db") as conn:
        conn.execute(f'CREATE TABLE "Msg_{chat_hash}" (local_id INTEGER, sort_seq INTEGER, local_type INTEGER, message_content TEXT)')
        conn.execute(f'INSERT INTO "Msg_{chat_hash}" VALUES (7, 1, 3, ?)',
                     (f'<msg><img md5="{image_id}"/></msg>',))
    path = source / "msg" / "attach" / chat_hash / "2026-10" / "Img" / f"{image_id}.dat"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    argv = ["image-path", "--account-dir", str(account), "--source-account-dir",
            str(source), "--chat", chat, "--local-id", "7", "--shard", "message_2.db"]
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["path"] == str(path.resolve())
    assert result["identity"]["shard"] == "message_2.db"
    path.unlink()
    assert main(argv) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "image_not_cached"


def test_export_cli_verifies_image_and_reports_missing(tmp_path, capsys):
    from PIL import Image

    chat = "export@chatroom"
    chat_hash = hashlib.md5(chat.encode()).hexdigest()
    account, source = tmp_path / "decrypted", tmp_path / "source"
    account.mkdir()
    image_id = "e" * 32
    with sqlite3.connect(account / "message_2.db") as conn:
        conn.execute(f'CREATE TABLE "Msg_{chat_hash}" (local_id INTEGER, sort_seq INTEGER, local_type INTEGER, message_content TEXT)')
        conn.execute(f'INSERT INTO "Msg_{chat_hash}" VALUES (7, 1, 3, ?)',
                     (f'<msg><img md5="{image_id}"/></msg>',))
    path = source / "msg" / "attach" / chat_hash / "2026-10" / "Img" / f"{image_id}.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (3, 2), "blue").save(path)
    argv = ["export-media", "--account-dir", str(account), "--source-account-dir",
            str(source), "--chat", chat, "--local-id", "7", "--shard", "message_2.db",
            "--output", str(tmp_path / "export")]
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["identity"]["shard"] == "message_2.db"
    assert result["width"] == 3
    assert result["kind"] == "image"
    path.unlink()
    assert main(argv) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "missing"
    secret = tmp_path / "invalid-key.json"
    secret.write_text(json.dumps({"aes_key": "sensitive-not-hex" * 2, "xor_key": 3}))
    assert main([*argv, "--image-key-file", str(secret)]) == 2
    captured = capsys.readouterr()
    assert "sensitive" not in captured.err
    assert not captured.out
