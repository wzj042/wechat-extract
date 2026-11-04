"""合成 SQLite 分片回归，不读取真实聊天。"""

import hashlib
import sqlite3

import pytest
import zstandard

from wechat_extract.messages import (
    AmbiguousMessageError, MessageReader, ShardReadError, decode_message_content,
)


CHAT = "synthetic@chatroom"
TABLE = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()


def make_shard(root, name, sender, *, local_id=7, seq=100, md5="aaaa", compressed=False):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (2,?)", (sender,))
        conn.execute(f"""CREATE TABLE "{TABLE}" (
            local_id INTEGER, local_type INTEGER, real_sender_id INTEGER,
            create_time INTEGER, sort_seq INTEGER, message_content BLOB,
            compress_content BLOB, packed_info_data BLOB, server_id INTEGER)""")
        text = f'<msg><img md5="{md5}"/></msg>'
        conn.execute(
            f'INSERT INTO "{TABLE}" VALUES (?,?,?,?,?,?,?,?,?)',
            (local_id, 3, 2, 1700000000, seq,
             None if compressed else text,
             zstandard.ZstdCompressor(write_content_size=False).compress(text.encode()) if compressed else None,
             md5.encode(), 12345),
        )
    return path


def test_collision_identity_and_sender_namespace(tmp_path):
    make_shard(tmp_path, "message_0.db", "alice", md5="aaaa")
    make_shard(tmp_path, "message/message_0.db", "bob", md5="bbbb", compressed=True)
    # 错误命名空间故意放相反映射，读取器不得使用它。
    with sqlite3.connect(tmp_path / "message_resource.db") as conn:
        conn.execute("CREATE TABLE SenderName2Id (user_name TEXT)")
        conn.execute("INSERT INTO SenderName2Id(rowid,user_name) VALUES (2,'wrong')")
    reader = MessageReader(tmp_path)
    rows = reader.list_messages(CHAT)
    assert len(rows) == 2
    assert {r["shard"] for r in rows} == {"message_0.db", "message/message_0.db"}
    assert {r["sender_username"] for r in rows} == {"alice", "bob"}
    assert {r["packed_info_data"] for r in rows} == {b"aaaa", b"bbbb"}
    assert {r["message_type"].category for r in rows} == {"image"}
    assert all(r["account"] == str(tmp_path.resolve()) and r["table"] == TABLE for r in rows)
    assert all(r["conversation"] == CHAT and r["local_id"] == 7 for r in rows)
    with pytest.raises(AmbiguousMessageError, match="指定 shard"):
        reader.get_message(CHAT, 7)
    for row in rows:
        assert reader.get_message(CHAT, 7, shard=row["shard"]) == row
    assert "bbbb" in reader.get_message(CHAT, 7, shard="message/message_0.db")["content_text"]
    with pytest.raises(FileNotFoundError):
        reader.get_message(CHAT, 7, shard="message_999.db")
    with pytest.raises(FileNotFoundError):
        reader.get_message(CHAT, 7, shard="../message_0.db")
    assert reader.get_message(CHAT, 999, shard="message_0.db") is None


def test_global_sort_pagination_and_no_false_dedup(tmp_path):
    make_shard(tmp_path, "message_0.db", "one", seq=2)
    make_shard(tmp_path, "message_1.db", "two", seq=3)
    make_shard(tmp_path, "message_2.db", "three", seq=1)
    reader = MessageReader(tmp_path)
    assert [r["sort_seq"] for r in reader.list_messages(CHAT)] == [3, 2, 1]
    assert [r["sort_seq"] for r in reader.list_messages(CHAT, descending=False)] == [1, 2, 3]
    assert [r["sort_seq"] for r in reader.list_messages(CHAT, limit=1, offset=1)] == [2]
    assert reader.list_messages("missing_user") == []
    assert reader.list_messages(CHAT, limit=0) == []
    with pytest.raises(ValueError):
        reader.list_messages(CHAT, offset=-1)


def test_damage_is_not_missing_messages(tmp_path):
    make_shard(tmp_path, "message_0.db", "one")
    (tmp_path / "message_1.db").write_bytes(b"this is not sqlite")
    with pytest.raises(ShardReadError, match="message_1.db"):
        MessageReader(tmp_path).list_messages(CHAT)
    with pytest.raises(ShardReadError):
        MessageReader(tmp_path).get_message(CHAT, 7)
    # 明确指定好分片时不需要读取坏分片。
    assert MessageReader(tmp_path).get_message(CHAT, 7, shard="message_0.db") is not None


def test_missing_sender_stays_unknown_and_schema_errors_surface(tmp_path):
    path = make_shard(tmp_path, "message_0.db", "one")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE Name2Id")
    assert MessageReader(tmp_path).get_message(CHAT, 7)["sender_username"] is None
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE Name2Id (wrong_name TEXT)")
    with pytest.raises(ShardReadError, match="message_0.db"):
        MessageReader(tmp_path).list_messages(CHAT)


def test_flat_legacy_name_and_no_writes(tmp_path):
    path = make_shard(tmp_path, "message__message_3.db", "one")
    before = path.read_bytes()
    reader = MessageReader(tmp_path)
    assert reader.shards() == ["message__message_3.db"]
    assert len(reader.list_messages(CHAT)) == 1
    assert path.read_bytes() == before
    assert not (tmp_path / "message__message_3.db-journal").exists()


def test_decoding_and_type_handling(tmp_path):
    frame = zstandard.ZstdCompressor(write_content_size=False).compress("中文消息".encode())
    assert decode_message_content(frame) == "中文消息"
    assert decode_message_content("fallback", frame) == "中文消息"
    assert decode_message_content(b"plain") == "plain"
    with pytest.raises(ValueError):
        decode_message_content(frame[:-2])
    path = make_shard(tmp_path, "message_0.db", "one")
    with sqlite3.connect(path) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET local_type=?, message_content=?',
                     (49 | (74 << 32), "<appmsg><type>74</type></appmsg>"))
    assert MessageReader(tmp_path).get_message(CHAT, 7)["message_type"].category == "file"
    with sqlite3.connect(path) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET message_content=?', (b"\xff",))
    with pytest.raises(ShardReadError):
        MessageReader(tmp_path).list_messages(CHAT)


def test_business_shard_is_not_deduplicated(tmp_path):
    make_shard(tmp_path, "message_0.db", "one")
    make_shard(tmp_path, "biz_message_0.db", "two")
    reader = MessageReader(tmp_path)
    assert len(reader.list_messages(CHAT)) == 2
    with pytest.raises(AmbiguousMessageError):
        reader.get_message(CHAT, 7)
