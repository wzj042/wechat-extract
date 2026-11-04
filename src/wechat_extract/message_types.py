"""WeChat 4.x message types, based on WCDA a0390ad (see docs/message-types.md).

This classifies types, not message contents. Unknown values stay unknown.
``content`` must already be decompressed and decoded to text by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


# (Chinese label, stable machine-readable category)
BASE_TYPES = {
    1: ("文本", "text"),
    3: ("图片", "image"),
    34: ("语音", "voice"),
    42: ("名片", "contact"),
    43: ("视频", "video"),
    47: ("动画表情", "emoji"),
    48: ("位置", "location"),
    49: ("应用消息", "app"),
    50: ("通话", "voip"),
    62: ("视频", "video"),
    10000: ("系统消息", "system"),
    10002: ("撤回消息", "revoke"),
}
APP_TYPES = {
    3: ("音乐", "music"),
    4: ("链接", "link"),
    5: ("链接", "link"),
    6: ("文件", "file"),
    8: ("动画表情卡", "emoji_card"),
    19: ("聊天记录", "chat_history"),
    33: ("小程序", "mini_program"),
    36: ("小程序", "mini_program"),
    44: ("小程序", "mini_program"),
    51: ("视频号", "finder"),
    57: ("引用消息", "quote"),
    62: ("拍一拍", "pat"),
    63: ("直播", "live"),
    68: ("链接", "link"),
    74: ("文件", "file"),
    87: ("群公告", "announcement"),
    88: ("直播", "live"),
    2000: ("转账", "transfer"),
    2001: ("红包", "red_packet"),
    2003: ("红包", "red_packet"),
}


@dataclass(frozen=True)
class MessageType:
    raw_type: int
    base_type: int
    packed_subtype: int
    xml_subtype: int | None
    subtype: int | None
    label: str
    category: str
    source: str
    conflict: bool = False
    system_type: str | None = None


def _payload_root(content: str | None) -> ET.Element | None:
    if not content:
        return None
    # Reject DTD/entity declarations; this function needs no entity expansion.
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        return None
    # Group messages may prefix the XML with "sender:\n".
    start = content.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(content[start:].strip())
    except ET.ParseError:
        return None


def _payload_block(root: ET.Element | None, name: str) -> ET.Element | None:
    if root is None:
        return None
    if root.tag.lower() == name:
        return root
    if root.tag.lower() == "msg":
        return next((child for child in root if child.tag.lower() == name), None)
    return None


def _child_text(element: ET.Element | None, name: str) -> str:
    if element is None:
        return ""
    return next(
        ((child.text or "").strip() for child in element if child.tag.lower() == name),
        "",
    )


def classify_message_type(local_type: int, content: str | None = None) -> MessageType:
    """Decode the low/high 32-bit type fields without losing their raw value.

    A nonzero packed subtype wins over XML. XML's direct appmsg/type is used
    when the packed subtype is zero. Structural hints are used only if both
    numeric subtype sources are absent/zero, never over an unknown subtype.
    A link-container type 4/5/68 without a URL can be an explicit patmsg.
    """
    if isinstance(local_type, bool) or not isinstance(local_type, int):
        raise TypeError("local_type must be an integer")
    if not -(1 << 63) <= local_type < (1 << 64):
        raise ValueError("local_type must fit in a signed or unsigned 64-bit integer")
    if content is not None and not isinstance(content, str):
        raise TypeError("content must be decoded text or None")

    bits = local_type & ((1 << 64) - 1)
    base, packed = bits & 0xFFFFFFFF, bits >> 32
    root = _payload_root(content)
    app = _payload_block(root, "appmsg")
    system = _payload_block(root, "sysmsg")
    xml_subtype = None
    if base == 49 and app is not None:
        try:
            xml_subtype = int(_child_text(app, "type"))
        except ValueError:
            pass
    subtype = packed or xml_subtype
    source = "packed" if packed else ("xml" if xml_subtype is not None else "base")
    conflict = bool(packed and xml_subtype is not None and packed != xml_subtype)
    label, category = BASE_TYPES.get(base, ("未知消息", "unknown"))
    system_type = system.get("type") if system is not None else None

    if base == 49:
        if subtype:
            label, category = APP_TYPES.get(subtype, ("未知应用消息", "unknown"))
            if (
                subtype in (4, 5, 68)
                and not _child_text(app, "url")
                and app is not None
                and (app.get("type") or "").lower() == "patmsg"
                and not any(c.tag.lower() == "refermsg" for c in app)
                and not conflict
            ):
                label, category, source = "拍一拍", "pat", "xml_hint"
        elif app is not None or system is not None:
            # Only direct blocks count: quoted/forwarded XML cannot change type.
            if app is not None and any(c.tag.lower() == "refermsg" for c in app):
                label, category, source = "引用消息", "quote", "xml_hint"
            elif (
                (app is not None and (app.get("type") or "").lower() == "patmsg")
                or (system_type or "").lower() == "patmsg"
            ):
                label, category, source = "拍一拍", "pat", "xml_hint"
            else:
                pay = None
                # wcpayinfo is a direct appmsg child, not a standalone root.
                if app is not None:
                    pay = next((c for c in app if c.tag.lower() == "wcpayinfo"), None)
                if pay is not None:
                    tags = {c.tag.lower() for c in pay}
                    if "paysubtype" in tags or "transferid" in tags:
                        label, category, source = "转账", "transfer", "xml_hint"
                    elif "redenvelope" in tags or "sendertitle" in tags:
                        label, category, source = "红包", "red_packet", "xml_hint"
        if category == "link" and subtype in (5, 68):
            try:
                is_article = urlsplit(_child_text(app, "url")).hostname == "mp.weixin.qq.com"
            except ValueError:
                is_article = False
            if is_article or _child_text(app, "sourceusername").startswith("gh_"):
                label, category = "公众号文章", "official_article"
    elif base == 10000 and system is not None:
        if (system_type or "").lower() == "patmsg":
            label, category = "拍一拍", "pat"
        elif (system_type or "").lower() == "revokemsg" or any(
            c.tag.lower() == "revokemsg" for c in system
        ):
            label, category = "撤回消息", "revoke"

    return MessageType(
        local_type, base, packed, xml_subtype, subtype, label, category,
        source, conflict, system_type,
    )
