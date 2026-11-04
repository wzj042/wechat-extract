"""媒体关联全部使用临时合成分片与假附件，不读真实聊天。"""

import hashlib
from pathlib import Path
import sqlite3

import pytest

from wechat_extract.media import (
    AmbiguousMediaError,
    _existing_file,
    media_output_relative_path,
    resolve_image,
    resolve_record_file,
)


def add_message(account, shard, conversation, *, image_id=None, packed=b"", local_type=3):
    account.mkdir(parents=True, exist_ok=True)
    table = "Msg_" + hashlib.md5(conversation.encode()).hexdigest()
    with sqlite3.connect(account / shard) as conn:
        conn.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER, local_type INTEGER, sort_seq INTEGER,'
            ' create_time INTEGER, message_content TEXT, packed_info_data BLOB)'
        )
        content = f'<msg><img md5="{image_id}" /></msg>' if image_id else "<msg/>"
        conn.execute(f'INSERT INTO "{table}" VALUES (1, ?, 1, 0, ?, ?)',
                     (local_type, content, packed))


def attachment(source, conversation, *parts):
    path = source / "msg" / "attach" / hashlib.md5(conversation.encode()).hexdigest()
    path = path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic media")
    return path.resolve()


def test_same_local_id_uses_source_shard_and_unique_output(tmp_path):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    first, second = "a" * 32, "b" * 32
    add_message(account, "message_0.db", "room", image_id=first)
    add_message(account, "message_1.db", "room", image_id=second)
    a = attachment(source, "room", "2026-10", "Img", first + ".dat")
    b = attachment(source, "room", "2026-10", "Img", second + ".dat")
    left = resolve_image(account, source, "room", 1, shard="message_0.db")
    right = resolve_image(account, source, "room", 1, shard="message_1.db")
    assert left["path"] == a
    assert right["path"] == b
    assert left["status"] == "ok"
    assert left["identity"]["shard"] == "message_0.db"
    assert left["output_relative_path"] != right["output_relative_path"]
    assert left["output_relative_path"].parts[-3:] == (
        "message_0.db", "Msg_" + hashlib.md5(b"room").hexdigest(), "1",
    )


def test_does_not_cross_conversations_even_with_same_filename(tmp_path):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    image_id = "a" * 32
    add_message(account, "message_0.db", "room", image_id=image_id)
    attachment(source, "other", "2026-10", "Img", image_id + ".dat")
    assert resolve_image(account, source, "room", 1, shard="message_0.db")["status"] == "missing"
    expected = attachment(source, "room", "2026-11", "Img", image_id + ".dat")
    assert resolve_image(account, source, "room", 1, shard="message_0.db")["path"] == expected


def test_duplicate_across_months_is_ambiguous_and_tier_is_explicit(tmp_path):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    image_id = "b" * 32
    add_message(account, "message_0.db", "room", image_id=image_id)
    high = attachment(source, "room", "2026-10", "Img", image_id + "_h.dat")
    attachment(source, "room", "2026-10", "Img", image_id + "_t.dat")
    result = resolve_image(account, source, "room", 1, shard="message_0.db")
    assert result["path"] == high and result["tier"] == "original"
    assert resolve_image(account, source, "room", 1, shard="message_0.db", tier="mid")["status"] == "missing"
    attachment(source, "room", "2026-11", "Img", image_id + "_h.dat")
    with pytest.raises(AmbiguousMediaError) as exc:
        resolve_image(account, source, "room", 1, shard="message_0.db")
    assert len(exc.value.candidates) == 3


def test_record_field9_and_zero_based_index(tmp_path):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    resource_id = "a1" * 8
    packed = b"\x08\x01\x10\x02\x4a\x12\x0a\x10" + resource_id.encode() + b"\x58\x00"
    add_message(account, "message_0.db", "room", packed=packed, local_type=(19 << 32) | 49)
    first = attachment(source, "room", "2026-10", "Rec", resource_id, "F", "0", "report.pdf")
    attachment(source, "room", "2026-10", "Rec", resource_id, "F", "1", "report.pdf")
    result = resolve_record_file(account, source, "room", 1, shard="message_0.db",
                                 data_index=0, filename="report.pdf")
    assert result["path"] == first
    missing = resolve_record_file(account, source, "room", 1, shard="message_0.db",
                                  data_index=2, filename="report.pdf")
    assert missing["reason"] == "record_file_not_cached"


def test_message_packed_id_and_malformed_bytes_do_not_guess(tmp_path):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    image_id = "c" * 32
    add_message(account, "message_0.db", "room",
                packed=b"\x12\x22\x0a\x20" + image_id.encode())
    expected = attachment(source, "room", "2026-10", "Img", image_id + ".dat")
    assert resolve_image(account, source, "room", 1, shard="message_0.db")["path"] == expected
    add_message(account, "message_1.db", "room", packed=b"\0" + image_id.encode())
    assert resolve_image(account, source, "room", 1, shard="message_1.db")["reason"] == "message_has_no_image_id"


def test_path_traversal_and_wrong_shard_are_rejected(tmp_path):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    source.mkdir()
    add_message(account, "message_0.db", "room", image_id="a" * 32)
    for name in ("../file.txt", r"..\file.txt", "C:secret", "file:stream"):
        with pytest.raises(ValueError):
            resolve_record_file(account, source, "room", 1, shard="message_0.db",
                                data_index=0, filename=name)
    with pytest.raises(FileNotFoundError):
        resolve_image(account, source, "room", 1, shard="../message_0.db")
    with pytest.raises(ValueError):
        _existing_file(tmp_path / "outside.dat", source, "room")
    with pytest.raises(ValueError):
        media_output_relative_path({"account": str(account), "shard": "../x.db",
                                    "table": "Msg_a", "local_id": 1})
    with pytest.raises(ValueError):
        resolve_image(account, source, "room", 1, shard=None)


def test_missing_message_and_same_name_accounts_do_not_collide(tmp_path):
    account, source = tmp_path / "one" / "account", tmp_path / "source"
    source.mkdir()
    add_message(account, "message_0.db", "room", image_id="a" * 32)
    assert resolve_image(account, source, "room", 2, shard="message_0.db")["reason"] == "message_not_found"
    row = {"account": str(account), "shard": "message_0.db", "table": "Msg_a", "local_id": 1}
    first = media_output_relative_path(row)
    row["account"] = str(tmp_path / "two" / "account")
    assert media_output_relative_path(row) != first


@pytest.mark.parametrize("suffix,tier", [
    ("", "mid"), ("_t", "thumb"), ("_h", "original"), (".png", "mid"),
])
def test_exact_extensionless_and_plain_image_names(tmp_path, suffix, tier):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    image_id = "a" * 32
    add_message(account, "message_0.db", "room", image_id=image_id)
    expected = attachment(source, "room", "2026-10", "Img", image_id + suffix)
    result = resolve_image(account, source, "room", 1, shard="message_0.db")
    assert result["path"] == expected and result["tier"] == tier and result["status"] == "ok"


def test_same_tier_multiple_formats_are_ambiguous(tmp_path):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    image_id = "a" * 32
    add_message(account, "message_0.db", "room", image_id=image_id)
    attachment(source, "room", "2026-10", "Img", image_id + ".dat")
    attachment(source, "room", "2026-10", "Img", image_id)
    with pytest.raises(AmbiguousMediaError) as exc:
        resolve_image(account, source, "room", 1, shard="message_0.db")
    assert len(exc.value.candidates) == 2


@pytest.mark.parametrize("content", [
    '<msg><nested><img md5="' + "a" * 32 + '"/></nested></msg>',
    '<!DOCTYPE msg [<!ENTITY image "' + "a" * 32 + '">]><msg><img md5="&image;"/></msg>',
])
def test_nested_image_or_xml_entity_does_not_supply_identity(tmp_path, content):
    account, source = tmp_path / "decrypted", tmp_path / "source"
    add_message(account, "message_0.db", "room")
    table = "Msg_" + hashlib.md5(b"room").hexdigest()
    with sqlite3.connect(account / "message_0.db") as conn:
        conn.execute(f'UPDATE "{table}" SET message_content=?', (content,))
    attachment(source, "room", "2026-10", "Img", "a" * 32 + ".dat")
    result = resolve_image(account, source, "room", 1, shard="message_0.db")
    assert result["reason"] == "message_has_no_image_id"
