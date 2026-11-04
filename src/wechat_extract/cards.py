"""把微信应用消息（appmsg 卡片）解析成 agent 可直接使用的结构化字段，并给出下一步工具建议。

覆盖：链接/公众号文章(5)、文件(6)、聊天记录(19)、小程序(33/36)、引用(57)、拍一拍(62)、转账(2000) 等；
未知子类型也返回 title/des/url 等通用字段，不丢信息。
图片消息本身不是卡片，但公众号「划线分享」生成的图片在 <img imgsourceurl> 里带原文链接，也解析出来。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from .events import strip_group_prefix


def _root(text: str) -> ET.Element | None:
    if not text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        return None
    start = text.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def _t(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    value = node.findtext(path)
    value = value.strip() if value else None
    return value if value and value != "null" else None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def image_source(root: ET.Element | None) -> dict | None:
    """图片的来源文章：公众号划线分享等场景，<img imgsourceurl> 是 URL 编码的原文链接。"""
    img = root if root is not None and root.tag == "img" else (root.find("img") if root is not None else None)
    raw = unquote(img.get("imgsourceurl") or "") if img is not None else ""
    if not raw.startswith("http"):
        return None
    parts = urlsplit(raw)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    url = raw
    if parts.hostname == "mp.weixin.qq.com" and all(query.get(k) for k in ("__biz", "mid", "sn")):
        # 只保留定位文章的参数，去掉 share_id/scene 等一次性跟踪参数，便于抓取
        url = "https://mp.weixin.qq.com/s?" + urlencode(
            {k: query[k] for k in ("__biz", "mid", "idx", "sn") if query.get(k)})
    underline = query.get("fromsharesource") == "underline"
    start, end = query.get("start", ""), query.get("end", "")
    return _clean({
        "kind": "公众号划线分享" if underline else "图片来源链接", "url": url,
        "highlight": {"start": int(start), "end": int(end)} if underline and start.isdigit() and end.isdigit() else None,
    })


def _quoted_content(ref: ET.Element | None) -> tuple[str | None, dict | None]:
    """被引用消息的可读内容：文本原样；卡片取标题；图片标注 [图片] 并带上来源文章。"""
    content = _t(ref, "content")
    if not content:
        return None, None
    qtype = _t(ref, "type")
    inner = _root(strip_group_prefix(content)) if qtype in ("3", "49") else None
    if qtype == "3":
        return "[图片]", image_source(inner)
    if qtype == "49" and inner is not None:
        app = inner if inner.tag == "appmsg" else inner.find("appmsg")
        title = _t(app, "title")
        if title:
            return title, None
    return content, None


def parse_card(row: dict) -> dict | None:
    """row 为 MessageReader 行；非卡片消息返回 None（带来源文章的图片除外）。"""
    mtype = row.get("message_type")
    category = mtype.get("category") if isinstance(mtype, dict) else getattr(mtype, "category", None)
    body = strip_group_prefix(row.get("content_text") or "")
    root = _root(body)
    if root is None:
        return None
    app = root if root.tag == "appmsg" else root.find("appmsg")
    if app is None:
        source = image_source(root) if category == "image" else None
        if source is None:
            return None
        # 不给 title：调用方用 title 定位界面里的消息，图片在界面上只显示为 [图片]
        hints = ["图片是这篇公众号文章的划线摘录，“这篇/原文”指的是 url 对应的文章，可用网页抓取工具读取全文"
                 if source.get("highlight") is not None else "图片附带来源链接，可用网页抓取工具读取"]
        return _clean({"kind": category, "url": source["url"], "image_source": source, "next": hints})
    sub = _t(app, "type")
    card = {
        "kind": category, "app_type": int(sub) if sub and sub.isdigit() else sub,
        "title": _t(app, "title"), "description": _t(app, "des"),
        "url": _t(app, "webviewshared/shareUrlOriginal") or _t(app, "url"),
        "source": _t(app, "sourcedisplayname") or _t(root, "appinfo/appname"),
        "source_username": _t(app, "sourceusername"),
    }
    hints = []
    if sub == "6":
        card.update(file_name=_t(app, "title"), file_ext=_t(app, "appattach/fileext"),
                    file_size=int(_t(app, "appattach/totallen") or 0) or None,
                    media_expire_at=int(_t(root, "extcommoninfo/media_expire_at") or 0) or None)
        card.pop("url", None)
        hints.append("decode_file_message 导出文件后再读取内容")
    elif sub in ("33", "36"):
        weapp = app.find("weappinfo")
        card.update(miniprogram={k: v for k, v in {
            "name": _t(app, "sourcedisplayname"), "username": _t(weapp, "username"), "appid": _t(weapp, "appid"),
            "pagepath": _t(weapp, "pagepath"), "version": _t(weapp, "version")}.items() if v})
        card.pop("url", None)  # 小程序卡片的 url 只是升级提示页，没有可抓取内容
        hints.append("小程序内容无法离线获取，只能依据标题/来源/页面路径理解")
    elif sub == "51":
        # 视频号分享：<title> 是给旧客户端的兜底文字（“当前版本不支持展示该内容…”），真实内容在 finderFeed
        feed = app.find("finderFeed")
        media = feed.find("mediaList/media") if feed is not None else None
        desc = _t(feed, "desc")
        duration = _t(media, "videoPlayDuration")
        width, height = _t(media, "width"), _t(media, "height")
        card.update(
            title=desc or "视频号作品", description=None, url=None,
            source=_t(feed, "nickname"), source_username=_t(feed, "username"),
            finder={k: v for k, v in {
                "author": _t(feed, "nickname"), "desc": desc,
                "feed_type": {"4": "video", "2": "image", "9": "live"}.get(_t(feed, "feedType") or "", _t(feed, "feedType")),
                "media_count": int(_t(feed, "mediaCount") or 0) or None,
                "duration_s": int(float(duration)) if duration else None,
                "size": f"{int(float(width))}x{int(float(height))}" if width and height else None,
                "object_id": _t(feed, "objectId"),
                "cover_url": _t(media, "coverUrl") or _t(media, "thumbUrl"),
            }.items() if v})
        hints.append("视频号视频无法离线播放：可依据作者与文案理解")
    elif sub == "19":
        card.update(record_items=len(re.findall(r"<dataitem\b", body)))
        hints.append("decode_record_item 按 0 基序号读取聊天记录条目")
    elif sub == "57":
        ref = app.find("refermsg")
        content, source = _quoted_content(ref)
        card.update(reply_text=_t(app, "title"), quoted={k: v for k, v in {
            "sender_display": _t(ref, "displayname"), "sender": _t(ref, "chatusr"), "svrid": _t(ref, "svrid"),
            "type": _t(ref, "type"), "content": (content or "")[:500],
            "image_source": source}.items() if v})
        card.pop("url", None)
        if source:
            hints.append("被引用的图片来自公众号文章（quoted.image_source.url），问“这篇”时指的是它，可用网页抓取工具读取")
    elif sub == "62":
        pat = app.find("patinfo")
        card.update(patter=_t(pat, "fromusername"), patted=_t(pat, "pattedusername"))
    elif sub == "2000":
        card.update(transfer={k: v for k, v in {
            "fee": _t(app, "wcpayinfo/feedesc"), "memo": _t(app, "wcpayinfo/pay_memo"),
            "pay_subtype": _t(app, "wcpayinfo/paysubtype")}.items() if v})
    if card.get("url") and card.get("url", "").startswith("http"):
        hints.append("可用网页抓取工具读取 url 正文（公众号文章可能需要登录或被拦截）")
    card["next"] = hints
    return _clean(card)


def summarize(row: dict) -> str:
    """一行人类可读摘要，供调用方把消息渲染给 agent。"""
    mtype = row.get("message_type")
    label = mtype.get("label") if isinstance(mtype, dict) else getattr(mtype, "label", "")
    card = parse_card(row)
    if card is None:
        if label in ("文本", "text"):
            return strip_group_prefix(row.get("content_text") or "")
        return f"[{label}]"
    source = card.get("image_source")
    if source:
        # 首行保持 [图片]：调用方按首行文字在界面里定位消息
        return f"[{label}]\n{source['kind']}，原文: {source['url']}"
    parts = [f"[{label}] {card.get('title') or ''}".strip()]
    for key in ("description", "source", "url", "file_size"):
        if card.get(key):
            parts.append(f"{key}: {card[key]}")
    if card.get("miniprogram"):
        parts.append(f"miniprogram: {card['miniprogram']}")
    quoted = card.get("quoted")
    if quoted and quoted.get("content"):
        who = quoted.get("sender_display") or quoted.get("sender")
        text = " ".join(quoted["content"].split())
        line = f"引用{' ' + who if who else ''}: {text[:60]}{'…' if len(text) > 60 else ''}"
        if quoted.get("image_source"):
            line += f"（{quoted['image_source']['kind']}，原文: {quoted['image_source']['url']}）"
        parts.append(line)
    finder = card.get("finder")
    if finder:
        meta = [finder.get("feed_type"), f"{finder['duration_s']}秒" if finder.get("duration_s") else None,
                finder.get("size")]
        parts.append("视频号: " + " · ".join(m for m in meta if m))
    return " | ".join(parts)
