"""群成员显示名：群名片 > 自身微信昵称 > wxid（不使用本账号给对方设的备注名）。

群名片存放在 contact.db 的 chat_room.ext_buffer（protobuf）：
    repeated field 1 = 成员 { 1: wxid, 2: 群名片(可空), 3: 标志, 4: 邀请人 wxid }
非好友群成员在 contact 表中为 local_type=3，或只存在于 stranger 表，昵称都取 nick_name。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(buf: bytes):
    """逐个产出 (field_number, wire_type, value)；只支持 varint 与 length-delimited。"""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            length, pos = _varint(buf, pos)
            value, pos = buf[pos:pos + length], pos + length
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield number, wire, value


def parse_group_cards(ext_buffer: bytes | None) -> dict[str, str | None]:
    """wxid -> 群名片（未设置为 None）。解析失败返回已解析部分。"""
    cards: dict[str, str | None] = {}
    if not ext_buffer:
        return cards
    try:
        for number, wire, value in _fields(bytes(ext_buffer)):
            if number != 1 or wire != 2:
                continue
            wxid = card = None
            for sub_number, sub_wire, sub_value in _fields(value):
                if sub_wire != 2:
                    continue
                if sub_number == 1:
                    wxid = sub_value.decode("utf-8", "replace")
                elif sub_number == 2:
                    card = sub_value.decode("utf-8", "replace").strip() or None
            if wxid:
                cards[wxid] = card
    except ValueError:
        pass
    return cards


def _nicknames(conn: sqlite3.Connection, usernames: set[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    if not usernames:
        return names
    marks = ",".join("?" * len(usernames))
    for table in ("contact", "stranger"):
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        for username, nick in conn.execute(f"SELECT username, nick_name FROM {table} WHERE username IN ({marks})",
                                           tuple(usernames)):
            if nick and username not in names:
                names[username] = nick if isinstance(nick, str) else bytes(nick).decode("utf-8", "replace")
    return names


def group_members(contact_db: Path, chatroom: str) -> list[dict]:
    """群成员列表：username、group_card、nickname、display_name（群名片 > 昵称 > wxid）。"""
    with closing(sqlite3.connect(Path(contact_db).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT ext_buffer FROM chat_room WHERE username=?", (chatroom,)).fetchone()
        cards = parse_group_cards(row[0] if row else None)
        nicks = _nicknames(conn, set(cards))
    return [{"username": u, "group_card": card, "nickname": nicks.get(u),
             "display_name": card or nicks.get(u) or u} for u, card in cards.items()]


def person_names(contact_db: Path, usernames: set[str]) -> dict[str, str]:
    """非群场景：自身微信昵称 > wxid（不用备注名）。"""
    with closing(sqlite3.connect(Path(contact_db).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        nicks = _nicknames(conn, set(usernames))
    return {u: nicks.get(u) or u for u in usernames}
