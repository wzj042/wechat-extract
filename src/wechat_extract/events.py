"""从单条消息识别与“我”相关的互动事件：拍一拍、@、引用我的消息。

只做识别，不做响应；输入是 MessageReader 返回的行（需含 content_text、source_text、message_type）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable

MENTION_ALL = "notify@all"


def self_username_from_account_dir(name: str) -> str | None:
    """原始账号目录名形如 wxid_xxx_1900，去掉末尾的数字后缀得到 wxid。"""
    m = re.fullmatch(r"(wxid_[0-9a-z]+)(?:_\d+)?", name, re.I)
    return m.group(1) if m else None


def _xml(text: str) -> ET.Element | None:
    if not text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        return None
    start = text.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def _appmsg(text: str) -> ET.Element | None:
    root = _xml(text)
    if root is None:
        return None
    return root if root.tag == "appmsg" else root.find("appmsg")


def strip_group_prefix(text: str) -> str:
    """群消息正文以“发送者wxid:\\n”开头，界面不显示这一前缀。"""
    return re.sub(r"^[\w@.-]+:\n", "", text or "", count=1)


def at_user_list(source_text: str) -> list[str]:
    m = re.search(r"<atuserlist>(.*?)</atuserlist>", source_text or "", re.S)
    if not m:
        return []
    raw = re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))
    return [u for u in (x.strip() for x in raw.split(",")) if u]


def detect_events(row: dict, self_username: str,
                  sender_of_server_id: Callable[[str, int], str | None] | None = None) -> list[dict]:
    """返回该消息触发的事件列表。

    sender_of_server_id(conversation, svrid) 用于引用判定：群聊 refermsg 的 fromusr 是群 ID，
    只有回查被引用消息的发送者才能确定“引用的是不是我”。
    """
    events: list[dict] = []
    mtype = row.get("message_type")
    category = mtype.get("category") if isinstance(mtype, dict) else getattr(mtype, "category", None)
    sender = row.get("sender_username")
    base = {
        "conversation": row.get("conversation"), "shard": row.get("shard"), "local_id": row.get("local_id"),
        "server_id": row.get("server_id"), "create_time": row.get("create_time"), "sender": sender,
        "from_self": sender == self_username,
    }
    body = strip_group_prefix(row.get("content_text") or "")

    if category == "pat":
        app = _appmsg(body)
        pat = app.find("patinfo") if app is not None else None
        if pat is not None:
            patter = pat.findtext("fromusername") or None
            patted = pat.findtext("pattedusername") or None
            events.append({**base, "kind": "pat_me" if patted == self_username else "pat",
                           "sender": patter, "from_self": patter == self_username,
                           "patter": patter, "patted": patted, "text": (app.findtext("title") or "").strip()})
        return events

    ats = at_user_list(row.get("source_text") or "")
    if self_username in ats:
        events.append({**base, "kind": "mention_me", "text": body})
    elif MENTION_ALL in ats:
        events.append({**base, "kind": "mention_all", "text": body})

    if category == "quote":
        app = _appmsg(body)
        ref = app.find("refermsg") if app is not None else None
        if ref is not None:
            quoted_sender = ref.findtext("chatusr") or None
            svrid_text = (ref.findtext("svrid") or "").strip()
            if not quoted_sender and sender_of_server_id and svrid_text.isdigit():
                quoted_sender = sender_of_server_id(row.get("conversation"), int(svrid_text))
            if quoted_sender == self_username:
                events.append({**base, "kind": "quote_me", "text": (app.findtext("title") or "").strip(),
                               "quoted_server_id": int(svrid_text) if svrid_text.isdigit() else None})
    return events
