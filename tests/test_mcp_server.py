"""通过 FastMCP 真正注册/调用工具；只使用合成账户及本地假后端。"""

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from wechat_extract import mcp_server as module


CHAT = "synthetic@chatroom"
TABLE = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()


def add_message(account, shard, local_id=1, local_type=1, text="synthetic", timestamp=10):
    account.mkdir(exist_ok=True)
    with sqlite3.connect(account / shard) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS Name2Id (user_name TEXT)")
        if not conn.execute("SELECT 1 FROM Name2Id").fetchone():
            conn.execute("INSERT INTO Name2Id VALUES (?)", (CHAT,))
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{TABLE}" (local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,sort_seq INTEGER,message_content TEXT)')
        conn.execute(f'INSERT INTO "{TABLE}" VALUES(?,?,?,?,?,?)',
                     (local_id, local_id + 100, local_type, timestamp, timestamp, text))


class Queries:
    def resolve_chat(self, name):
        return CHAT

    def history(self, name, **kwargs):
        return [dict(conversation=CHAT, arguments=kwargs)]

    def search(self, keyword, **kwargs):
        return dict(keyword=keyword, **kwargs)

    def contacts(self, query, limit):
        return [dict(query=query, limit=limit)]

    def recent_sessions(self, limit):
        return [dict(limit=limit)]

    def contact_tags(self):
        return [{"name": "synthetic"}]

    def tag_members(self, name):
        return [{"name": name}]


class Runtime:
    def __init__(self, root):
        self.account_dir = root / "account"
        self.account_dir.mkdir(exist_ok=True)
        self.source_account_dir = root / "source"
        self.output_dir = root / "output"
        self.config = {}
        self.queries = Queries()
        self.scopes = []

    def refresh(self, scope="messages"):
        self.scopes.append(scope)
        return {"status": "ok"}

    def status(self):
        return {"status": "ready"}


def call(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    return json.loads(result[0].text)


def test_tool_inventory_compatibility_and_refresh_scope(tmp_path):
    runtime = Runtime(tmp_path)
    server = module.create_server(runtime)
    expected = {"get_recent_sessions", "get_chat_history", "search_messages", "get_contacts",
                "get_contact_tags", "get_tag_members", "get_new_messages", "decode_image",
                "decode_file_message", "decode_record_item", "decode_transfer", "decode_refer",
                "decode_location", "get_chat_images", "get_voice_messages", "decode_voice",
                "export_media", "get_status", "get_new_events", "get_recent_events",
                "get_message_detail", "get_change_marker", "get_group_members"}
    assert len(expected) == 23
    assert {tool.name for tool in asyncio.run(server.list_tools())} == expected
    assert runtime.scopes == []
    assert call(server, "get_status")["status"] == "ready"
    assert runtime.scopes == []
    assert call(server, "get_contacts", query="x", limit=5)[0]["limit"] == 5
    assert runtime.scopes[-1] == "contacts"
    call(server, "get_recent_sessions")
    assert runtime.scopes[-1] == "sessions"
    result = call(server, "get_chat_history", chat_name="room", oldest_first=True)
    assert result[0]["arguments"]["oldest_first"] is True
    assert runtime.scopes[-1] == "messages"
    assert call(server, "search_messages", keyword="k", chat_name=["a", "b"])["chat_name"] == ["a", "b"]
    assert call(server, "get_chat_images", chat_name="room")[0]["arguments"]["msg_types"] == ["image"]


def test_xml_decoders_and_cross_shard_ambiguity(tmp_path):
    runtime = Runtime(tmp_path)
    transfer = '<msg><appmsg><type>2000</type><wcpayinfo><feedesc>￥1.00</feedesc><paysubtype>1</paysubtype></wcpayinfo></appmsg></msg>'
    add_message(runtime.account_dir, "message_0.db", local_type=49, text=transfer)
    add_message(runtime.account_dir, "message_1.db", local_type=49, text=transfer, timestamp=20)
    add_message(runtime.account_dir, "message_0.db", 2, 49, '<appmsg><type>57</type><refermsg><type>1</type><content>quoted</content></refermsg></appmsg>')
    add_message(runtime.account_dir, "message_0.db", 3, 48, '<msg><location x="12" y="34" label="synthetic"/></msg>')
    add_message(runtime.account_dir, "message_0.db", 4, 49, '<appmsg><type>19</type><recorditem><![CDATA[<recordinfo><datalist><dataitem datatype="1"><datadesc>nested text</datadesc></dataitem></datalist></recordinfo>]]></recorditem></appmsg>')
    server = module.create_server(runtime)
    assert call(server, "decode_transfer", chat_name="r", local_id=1)["status"] == "ambiguous"
    result = call(server, "decode_transfer", chat_name="r", local_id=1, create_time=20)
    assert result["identity"]["shard"] == "message_1.db"
    assert result["fields"]["feedesc"] == "￥1.00"
    assert call(server, "decode_refer", chat_name="r", local_id=2)["fields"]["content"] == "quoted"
    assert call(server, "decode_location", chat_name="r", local_id=3)["fields"]["label"] == "synthetic"
    assert call(server, "decode_record_item", chat_name="r", local_id=4, item_index=0)["fields"]["datadesc"] == "nested text"
    assert call(server, "decode_record_item", chat_name="r", local_id=4, item_index=1)["reason"] == "record_index_out_of_range"
    assert call(server, "decode_transfer", chat_name="r", local_id=1, shard="message_999.db")["status"] == "missing"


def test_new_messages_initial_baseline_and_full_identity(tmp_path):
    runtime = Runtime(tmp_path)
    add_message(runtime.account_dir, "message_0.db")
    server = module.create_server(runtime)
    assert call(server, "get_new_messages") == {"status": "ok", "baseline": True, "messages": []}
    add_message(runtime.account_dir, "message_1.db")
    result = call(server, "get_new_messages")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["shard"] == "message_1.db"
    assert call(server, "get_new_messages")["messages"] == []


def test_refresh_failure_cannot_serve_stale_or_leak_exception(tmp_path):
    runtime = Runtime(tmp_path)
    server = module.create_server(runtime)
    runtime.refresh = lambda **kwargs: {"status": "failed", "key": "private-key"}
    result = call(server, "get_contacts")
    assert result == {"status": "not_ready", "reason": "refresh_failed"}
    def broken(**kwargs):
        raise RuntimeError("private-key")
    runtime.refresh = broken
    assert "private-key" not in json.dumps(call(server, "get_contacts"))


def test_voice_local_decode_to_wav(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    add_message(runtime.account_dir, "message_0.db", local_type=34)
    silk = tmp_path / "voice.silk"
    silk.write_bytes(b"\x02#!SILK_V3synthetic")
    monkeypatch.setattr(module, "export_attachment", lambda *args, **kwargs: {
        "status": "ok", "output_path": str(silk), "identity": {"shard": "message_0.db"}})
    def decode(inp, out, rate):
        assert inp.read().startswith(b"#!SILK_V3") and rate == 24000
        out.write(b"\x00\x00" * 50)
    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=decode))
    server = module.create_server(runtime)
    result = call(server, "decode_voice", chat_name="r", local_id=1)
    assert result["format"] == "wav" and Path(result["output_path"]).read_bytes().startswith(b"RIFF")
