import sqlite3

from wechat_extract.chatroom import group_members, parse_group_cards


def _ld(number: int, payload: bytes) -> bytes:
    return bytes([(number << 3) | 2, len(payload)]) + payload


def member(wxid: str, card: str | None = None) -> bytes:
    body = _ld(1, wxid.encode()) + (_ld(2, card.encode()) if card else b"") + bytes([3 << 3, 1])
    return _ld(1, body)


def test_parse_group_cards():
    buf = member("wxid_a", "测试名片甲") + member("wxid_b") + bytes([2 << 3, 5])
    assert parse_group_cards(buf) == {"wxid_a": "测试名片甲", "wxid_b": None}
    assert parse_group_cards(b"\x0a\xff") == {}  # 截断不抛异常


def test_display_prefers_card_then_nickname_never_remark(tmp_path):
    db = tmp_path / "contact.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE chat_room (id INTEGER, username TEXT, owner TEXT, ext_buffer BLOB)")
    conn.execute("CREATE TABLE contact (username TEXT, local_type INTEGER, nick_name TEXT, remark TEXT)")
    conn.execute("CREATE TABLE stranger (username TEXT, nick_name TEXT)")
    conn.execute("INSERT INTO chat_room VALUES (1, 'r@chatroom', '', ?)",
                 (member("wxid_a", "群名片A") + member("wxid_b") + member("wxid_c") + member("wxid_d"),))
    conn.executemany("INSERT INTO contact VALUES (?,?,?,?)",
                     [("wxid_a", 1, "昵称A", "备注A"), ("wxid_b", 1, "昵称B", "备注B"), ("wxid_c", 3, "陌生人C", "")])
    conn.execute("INSERT INTO stranger VALUES ('wxid_d', '只在stranger')")
    conn.commit()
    conn.close()
    names = {m["username"]: m["display_name"] for m in group_members(db, "r@chatroom")}
    assert names == {"wxid_a": "群名片A", "wxid_b": "昵称B", "wxid_c": "陌生人C", "wxid_d": "只在stranger"}
