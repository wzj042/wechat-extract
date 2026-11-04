"""微信本地数据只读 MCP（23 个工具，stdio 或本机 HTTP）；刷新与查询在同一锁内串行执行。"""

import argparse
from contextlib import closing
from dataclasses import asdict, is_dataclass
from functools import wraps
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import wave
import xml.etree.ElementTree as ET

from mcp.server.fastmcp import FastMCP

from .cards import parse_card, summarize
from .chatroom import group_members, person_names
from .events import detect_events, self_username_from_account_dir, strip_group_prefix
from .export_media import export_media as export_attachment
from .messages import AmbiguousMessageError, MessageReader


def _json(value):
    def convert(item):
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, bytes):
            return {"binary_bytes": len(item)}
        raise TypeError(type(item).__name__)
    return json.dumps(value, ensure_ascii=False, default=convert)


def _identity(row):
    return {key: row[key] for key in ("account", "conversation", "shard", "table", "local_id")}


def _xml(text):
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("unsafe_xml")
    start = text.find("<")
    if start < 0:
        raise ValueError("not_xml")
    return ET.fromstring(text[start:])


def _fields(node):
    if node is None:
        return None
    result = dict(node.attrib)
    for child in node:
        value = _fields(child) if len(child) or child.attrib else (child.text or "")
        if child.tag in result:
            previous = result[child.tag]
            result[child.tag] = previous + [value] if isinstance(previous, list) else [previous, value]
        else:
            result[child.tag] = value
    return result


def _message_snapshot(account_dir):
    """扫描身份列建立游标；首次不读取或返回历史正文，不用单一时间戳去重。"""
    reader = MessageReader(account_dir)
    result = {}
    for shard in reader.shards():
        path = reader.account_dir / shard
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            names = {}
            if any(t.lower() == "name2id" for t in tables):
                names = {hashlib.md5(r[0].encode()).hexdigest(): r[0]
                         for r in conn.execute("SELECT user_name FROM Name2Id") if r[0]}
            for table in tables:
                if not re.fullmatch(r"Msg_[0-9a-f]{32}", table, re.I):
                    continue
                for row in conn.execute(f'SELECT local_id,server_id,create_time,sort_seq FROM "{table}"'):
                    key = (str(reader.account_dir), shard, table, *tuple(row))
                    result[key] = dict(row, account=str(reader.account_dir), shard=shard,
                                       table=table, conversation=names.get(table[4:].lower()))
    return result


def create_server(runtime):
    """仅注册工具；创建服务不解密、不读取真实账户，不在 stdout 打印日志。"""
    server = FastMCP("wechat-extract", log_level="WARNING")
    lock = threading.RLock()
    seen = None
    seen_events = None

    def tool(scope="messages"):
        def decorate(function):
            @wraps(function)
            def guarded(*args, **kwargs):
                with lock:
                    try:
                        if scope is not None:
                            refreshed = runtime.refresh(scope=scope)
                            if isinstance(refreshed, dict) and refreshed.get("status") in {"error", "failed", "not_ready"}:
                                return _json({"status": "not_ready", "reason": "refresh_failed"})
                        return _json(function(*args, **kwargs))
                    except AmbiguousMessageError:
                        return _json({"status": "ambiguous", "reason": "provide_shard"})
                    except (ValueError, TypeError, ET.ParseError):
                        return _json({"status": "error", "reason": "invalid_or_ambiguous_request"})
                    except FileNotFoundError:
                        return _json({"status": "missing", "reason": "resource_not_found"})
                    except Exception:
                        # 不能把密钥、正文或第三方异常中的参数回显到协议响应。
                        return _json({"status": "error", "reason": "operation_failed"})
            server.tool()(guarded)
            return function
        return decorate

    def selected(chat_name, local_id, shard=None, create_time=0):
        conversation = runtime.queries.resolve_chat(chat_name)
        reader = MessageReader(runtime.account_dir)
        if shard is not None or not create_time:
            row = reader.get_message(conversation, local_id, shard=shard)
            return row if row is None or not create_time or row.get("create_time") == create_time else None
        rows = [row for row in reader._read(conversation, local_id=local_id)
                if row.get("create_time") == create_time]
        if len(rows) > 1:
            raise AmbiguousMessageError("provide_shard")
        return rows[0] if rows else None

    def export_row(row, kind="auto", tier="best", data_index=None, filename=None):
        if row is None:
            return {"status": "missing", "reason": "message_not_found"}
        config = runtime.config
        result = export_attachment(
            runtime.account_dir, runtime.source_account_dir, row["conversation"], row["local_id"],
            runtime.output_dir, shard=row["shard"], kind=kind, tier=tier,
            aes_key=config.get("image_aes_key"), xor_key=config.get("image_xor_key"),
            aes_size_mode=config.get("aes_size_mode", "ciphertext"),
            ffmpeg=config.get("ffmpeg", "ffmpeg"), data_index=data_index, filename=filename,
            emoji_hosts=config.get("emoji_cdn_hosts"),
        )
        if isinstance(result, dict):
            for key, target in (("output_path", "agent_path"), ("preview_path", "preview_agent_path")):
                mapped = agent_path(result.get(key))
                if mapped:
                    result[target] = mapped
        return result

    def agent_path(path):
        """容器内 agent 看不到宿主路径：按 agent_path_map 把导出路径映射成容器内挂载路径。"""
        if not path:
            return None
        output = str(Path(path).resolve())
        mapped = None
        for host, mounted in (runtime.config.get("agent_path_map") or {}).items():
            host = str(Path(host).resolve())
            if output.lower().startswith(host.lower()):
                mapped = mounted.rstrip("/") + output[len(host):].replace("\\", "/")
        return mapped

    def is_emoji(row):
        return row is not None and row["message_type"].base_type == 47

    def decode_structured(chat_name, local_id, shard, create_time, category):
        row = selected(chat_name, local_id, shard, create_time)
        if row is None:
            return {"status": "missing", "reason": "message_not_found"}
        if row["message_type"].category != category:
            return {"status": "unsupported", "reason": "message_type_mismatch", "identity": _identity(row)}
        root = _xml(row["content_text"])
        app = root if root.tag == "appmsg" else root.find("appmsg")
        node = (root if root.tag == "location" else root.find("location")) if category == "location" else (
            app.find("wcpayinfo" if category == "transfer" else "refermsg") if app is not None else None)
        if node is None:
            return {"status": "missing", "reason": "xml_block_missing", "identity": _identity(row)}
        return {"status": "ok", "identity": _identity(row), "fields": _fields(node)}

    @tool("sessions")
    def get_recent_sessions(limit: int = 20) -> str:
        """读取最近会话。"""
        return runtime.queries.recent_sessions(limit)

    @tool()
    def get_chat_history(chat_name: str, limit: int = 50, offset: int = 0, start_time: str = "", end_time: str = "", oldest_first: bool = False, msg_types: list[str] | None = None) -> str:
        """读取会话消息，结果包含完整分片身份；sender_display 为群名片 > 昵称 > wxid。"""
        return with_sender_names(runtime.queries.history(
            chat_name, limit=limit, offset=offset, start_time=start_time,
            end_time=end_time, oldest_first=oldest_first, msg_types=msg_types))

    @tool("contacts")
    def get_group_members(chat_name: str) -> str:
        """群成员名单：username、group_card（群名片）、nickname（微信昵称）、display_name（群名片 > 昵称 > wxid）。"""
        conversation = runtime.queries.resolve_chat(chat_name)
        if not conversation.endswith("@chatroom"):
            raise ValueError("not_a_group")
        contact = runtime.queries._database("contact.db")
        return {"status": "ok", "chat": conversation,
                "members": group_members(contact, conversation) if contact else []}

    @tool()
    def search_messages(keyword: str, chat_name: str | list[str] | None = None, start_time: str = "", end_time: str = "", limit: int = 20, offset: int = 0) -> str:
        """按关键词搜索一个、多个或全部会话。"""
        return runtime.queries.search(keyword, chat_name=chat_name or "", start_time=start_time,
                                      end_time=end_time, limit=limit, offset=offset)

    @tool("contacts")
    def get_contacts(query: str = "", limit: int = 50) -> str:
        """查找联系人。"""
        return runtime.queries.contacts(query, limit)

    @tool("contacts")
    def get_contact_tags() -> str:
        """读取联系人标签。"""
        return runtime.queries.contact_tags()

    @tool("contacts")
    def get_tag_members(tag_name: str) -> str:
        """读取标签下的联系人。"""
        return runtime.queries.tag_members(tag_name)

    @tool()
    def get_new_messages() -> str:
        """首次建立基线；以后返回新增身份，最多100条，重启后重新建立基线。"""
        nonlocal seen
        snapshot = _message_snapshot(runtime.account_dir)
        if seen is None:
            seen = set(snapshot)
            return {"status": "ok", "baseline": True, "messages": []}
        fresh = sorted(set(snapshot) - seen, key=lambda key: (snapshot[key]["sort_seq"] or 0, key))[:100]
        messages = []
        for key in fresh:
            identity = snapshot[key]
            conversation = identity["conversation"]
            if conversation:
                row = MessageReader(runtime.account_dir).get_message(conversation, identity["local_id"], shard=identity["shard"])
                if row is not None:
                    row = dict(row, card=parse_card(row), summary=summarize(row))
                messages.append(row if row is not None else identity)
            else:
                messages.append(dict(identity, reason="conversation_unresolved"))
        seen.update(fresh)
        with_sender_names(messages)
        return {"status": "ok", "baseline": False, "messages": messages,
                "has_more": bool(set(snapshot) - seen)}

    def display_names(conversation, usernames):
        """发送者显示名：群聊 群名片 > 昵称 > wxid；私聊 昵称 > wxid。不使用备注名。"""
        usernames = {u for u in usernames if u}
        contact = runtime.queries._database("contact.db")
        if contact is None or not usernames:
            return {}
        names = {}
        if conversation and conversation.endswith("@chatroom"):
            names = {m["username"]: m["display_name"] for m in group_members(contact, conversation)}
        missing = usernames - set(names)
        if missing:
            names.update(person_names(contact, missing))
        return names

    def with_sender_names(rows):
        """给消息行补 sender_display，避免 agent 拿 wxid 自行编称呼。"""
        if not isinstance(rows, list):
            return rows
        by_conversation = {}
        for row in rows:
            if isinstance(row, dict) and row.get("sender_username"):
                by_conversation.setdefault(row.get("conversation"), set()).add(row["sender_username"])
        names = {conv: display_names(conv, users) for conv, users in by_conversation.items()}
        for row in rows:
            if isinstance(row, dict) and row.get("sender_username"):
                row["sender_display"] = names.get(row.get("conversation"), {}).get(row["sender_username"],
                                                                                 row["sender_username"])
        return rows

    def self_username():
        name = runtime.config.get("self_username") or self_username_from_account_dir(Path(runtime.source_account_dir).name)
        if not name:
            raise ValueError("self_username_unknown")
        return name

    def events_of(rows, include_self):
        me = self_username()
        reader = MessageReader(runtime.account_dir)

        def sender_of(conversation, server_id):
            hits = reader.find_by_server_id(conversation, server_id)
            return hits[0]["sender_username"] if len(hits) == 1 else None
        names = {}
        result = []
        for row in rows:
            for event in detect_events(row, me, sender_of):
                if event["from_self"] and not include_self:
                    continue
                conversation = event["conversation"]
                if conversation not in names:
                    names[conversation] = runtime.queries.display_name(conversation)
                people = display_names(conversation, {event.get("sender"), event.get("patter"), event.get("patted")})
                extra = {key + "_display": people.get(event[key], event[key])
                         for key in ("sender", "patter", "patted") if event.get(key)}
                result.append(dict(event, **extra, chat_display_name=names[conversation], identity=_identity(row)))
        return result

    @tool()
    def get_new_events(include_self: bool = False) -> str:
        """与“我”相关的新互动事件：pat_me/pat、mention_me/mention_all、quote_me。首次调用只建立基线。"""
        nonlocal seen_events
        snapshot = _message_snapshot(runtime.account_dir)
        if seen_events is None:
            seen_events = set(snapshot)
            return {"status": "ok", "baseline": True, "events": []}
        fresh = sorted(set(snapshot) - seen_events, key=lambda key: (snapshot[key]["sort_seq"] or 0, key))[:200]
        reader = MessageReader(runtime.account_dir)
        rows = []
        for key in fresh:
            identity = snapshot[key]
            if identity["conversation"]:
                row = reader.get_message(identity["conversation"], identity["local_id"], shard=identity["shard"])
                if row is not None:
                    rows.append(row)
        seen_events.update(fresh)
        return {"status": "ok", "baseline": False, "events": events_of(rows, include_self),
                "has_more": bool(set(snapshot) - seen_events)}

    @tool()
    def get_recent_events(chat_name: str, limit: int = 50, since: int = 0, include_self: bool = False) -> str:
        """回放某会话最近 limit 条消息中的互动事件（since 为 Unix 秒），按时间正序；用于补漏与测试。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 500:
            raise ValueError("limit 须为 1–500")
        conversation = runtime.queries.resolve_chat(chat_name)
        rows = [row for row in MessageReader(runtime.account_dir).list_messages(conversation, limit=limit)
                if (row.get("create_time") or 0) >= since]
        return {"status": "ok", "events": events_of(list(reversed(rows)), include_self)}

    @tool("media")
    def decode_image(chat_name: str, local_id: int, shard: str | None = None, create_time: int = 0, tier: str = "best") -> str:
        """精确导出缓存图片，不下载或操纵微信 UI；动画表情改为从微信 CDN 取回原件（另含 preview_path 抽帧拼图）。"""
        row = selected(chat_name, local_id, shard, create_time)
        return export_row(row, "emoji" if is_emoji(row) else "image", tier)

    @tool()
    def get_message_detail(chat_name: str, local_id: int, shard: str | None = None, create_time: int = 0) -> str:
        """单条消息的结构化详情：文本，或卡片字段（链接/公众号/小程序/文件/聊天记录/引用/转账）及下一步工具建议。"""
        row = selected(chat_name, local_id, shard, create_time)
        if row is None:
            return {"status": "missing", "reason": "message_not_found"}
        card = parse_card(row)
        return {"status": "ok", "identity": _identity(row), "sender": row.get("sender_username"),
                "create_time": row.get("create_time"), "type": row["message_type"], "summary": summarize(row),
                "card": card, "text": None if card else strip_group_prefix(row.get("content_text") or "")}

    @tool(None)
    def get_change_marker() -> str:
        """消息库变化标记：只读取原始 db/WAL 的大小与修改时间，不解密、不刷新缓存。标记变了才值得调用 get_new_messages。"""
        if not runtime.db_dir:
            return {"status": "unsupported", "reason": "snapshot_mode"}
        db_dir = Path(runtime.db_dir)
        parts = []
        for pattern in ("message/*.db", "message/*.db-wal", "session/*.db-wal"):
            for path in sorted(db_dir.glob(pattern)):
                try:
                    st = path.stat()
                except OSError:
                    continue
                parts.append(f"{path.relative_to(db_dir).as_posix()}:{st.st_size}:{st.st_mtime_ns}")
        return {"status": "ok", "marker": hashlib.sha1("|".join(parts).encode()).hexdigest()[:16], "files": len(parts)}

    @tool("media")
    def decode_file_message(chat_name: str, local_id: int, create_time: int = 0, shard: str | None = None) -> str:
        """导出指定文件消息。"""
        return export_row(selected(chat_name, local_id, shard, create_time), "file")

    @tool("media")
    def decode_record_item(chat_name: str, local_id: int, item_index: int, create_time: int = 0, shard: str | None = None) -> str:
        """解析合并转发的0基条目；文件条目尝试导出本地附件。"""
        if item_index < 0:
            raise ValueError("item_index")
        row = selected(chat_name, local_id, shard, create_time)
        if row is None:
            return {"status": "missing", "reason": "message_not_found"}
        if row["message_type"].category != "chat_history":
            return {"status": "unsupported", "reason": "message_type_mismatch"}
        root = _xml(row["content_text"])
        app = root if root.tag == "appmsg" else root.find("appmsg")
        record = app.find("recorditem") if app is not None else None
        if record is None:
            return {"status": "missing", "reason": "recorditem_missing"}
        inner = _xml(record.text) if record.text and record.text.strip().startswith("<") else record
        items = list(inner.iter("dataitem"))
        if item_index >= len(items):
            return {"status": "missing", "reason": "record_index_out_of_range"}
        item = items[item_index]
        result = {"status": "ok", "identity": _identity(row), "item_index": item_index, "fields": _fields(item)}
        if item.get("datatype") == "8":
            filename = item.findtext("datatitle") or ""
            if filename:
                result["attachment"] = export_row(row, "record-file", data_index=item_index, filename=filename)
        return result

    @tool()
    def decode_transfer(chat_name: str, local_id: int, create_time: int = 0, shard: str | None = None) -> str:
        """解析选定转账的 wcpayinfo 字段。"""
        return decode_structured(chat_name, local_id, shard, create_time, "transfer")

    @tool()
    def decode_refer(chat_name: str, local_id: int, create_time: int = 0, shard: str | None = None) -> str:
        """解析选定引用消息的 refermsg 字段。"""
        return decode_structured(chat_name, local_id, shard, create_time, "quote")

    @tool()
    def decode_location(chat_name: str, local_id: int, create_time: int = 0, shard: str | None = None) -> str:
        """解析选定位置消息的全部属性和直系字段。"""
        return decode_structured(chat_name, local_id, shard, create_time, "location")

    @tool()
    def get_chat_images(chat_name: str, limit: int = 20, offset: int = 0, start_time: str = "", end_time: str = "") -> str:
        """列出图片消息和分片身份。"""
        return runtime.queries.history(chat_name, limit=limit, offset=offset, start_time=start_time, end_time=end_time, msg_types=["image"])

    @tool()
    def get_voice_messages(chat_name: str, limit: int = 20, offset: int = 0, start_time: str = "", end_time: str = "") -> str:
        """列出语音消息和分片身份。"""
        return runtime.queries.history(chat_name, limit=limit, offset=offset, start_time=start_time, end_time=end_time, msg_types=["voice"])

    @tool("media")
    def decode_voice(chat_name: str, local_id: int, shard: str | None = None, create_time: int = 0) -> str:
        """通过已安装的 silk-python 本地转成 WAV，缺少解码器时明确 not_ready。"""
        row = selected(chat_name, local_id, shard, create_time)
        exported = export_row(row, "voice")
        if exported["status"] != "ok":
            return exported
        try:
            import pysilk
        except ImportError:
            return dict(exported, status="not_ready", reason="local_silk_decoder_unavailable")
        data = Path(exported["output_path"]).read_bytes()
        decoded = io.BytesIO()
        pysilk.decode(io.BytesIO(data[1:] if data.startswith(b"\x02") else data), decoded, 24000)
        pcm = decoded.getvalue()
        if not pcm or len(pcm) % 2:
            return dict(exported, status="incomplete", reason="invalid_decoded_pcm")
        target = Path(exported["output_path"]).with_suffix(".wav")
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".wav-") as directory:
            staged = Path(directory) / "voice.wav"
            with wave.open(str(staged), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(pcm)
            os.replace(staged, target)
        return dict(exported, output_path=str(target), format="wav", validation="local_silk_decode", pcm_bytes=len(pcm))

    @tool("media")
    def export_media(chat_name: str, local_id: int, shard: str | None = None, create_time: int = 0, kind: str = "auto", tier: str = "best", data_index: int | None = None, filename: str | None = None) -> str:
        """统一导出指定消息的附件：image/voice/video/file/record-file 只用本地缓存；emoji（动画表情）从微信 CDN 取回原件并校验 md5，动图另给 preview_path 抽帧拼图。"""
        return export_row(selected(chat_name, local_id, shard, create_time), kind, tier, data_index, filename)

    @tool(None)
    def get_status() -> str:
        """只读服务状态，不触发刷新或密钥提取。"""
        return runtime.status()

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="WeChat 本地 MCP（stdio，或仅本机 HTTP + Bearer）")
    parser.add_argument("--config", default=os.environ.get("WECHAT_MCP_CONFIG"))
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, help="HTTP 端口；--transport http 时必填")
    parser.add_argument("--token-env", default="WECHAT_MCP_TOKEN")
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("需要 --config 或 WECHAT_MCP_CONFIG")
    if args.transport == "http" and args.port is None:
        parser.error("HTTP 模式需要 --port")
    # 底层 CLI 默认向 stdout 写日志；stdio 的 stdout 只能承载 JSON-RPC。
    os.environ["WECHAT_TOOL_ENABLE_CONSOLE_LOG"] = "0"
    os.environ["WECHAT_TOOL_LOG_LEVEL"] = "WARNING"
    logging.basicConfig(level=logging.WARNING)
    from .mcp_runtime import Runtime
    runtime = Runtime(args.config)
    os.environ["WECHAT_TOOL_OUTPUT_DIR"] = str(runtime.output_dir)
    server = create_server(runtime)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        from .httpserve import serve_http
        serve_http(server, args.host, args.port, os.environ.get(args.token_env, ""))


if __name__ == "__main__":
    main()
