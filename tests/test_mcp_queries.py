import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta

import pytest
import zstandard

from wechat_extract.mcp_queries import QueryService
from wechat_extract.messages import ShardReadError


@pytest.fixture
def service(tmp_path):
    with closing(sqlite3.connect(tmp_path / "contact.db")) as conn:
        conn.executescript("CREATE TABLE contact(username,nick_name,remark,alias,local_type,extra_buffer);"
                           "CREATE TABLE contact_label(label_id_,label_name_,sort_order_);")
        conn.executemany("INSERT INTO contact VALUES (?,?,?,?,?,?)", [
            ("alice", "Alice", "同名", "alias_a", 1, b"\xf2\x01\x011"),
            ("bob", "Bob", "同名", "", 1, None),
            ("member", "成员", "", "", 3, None),
        ])
        conn.execute("INSERT INTO contact_label VALUES (1,'朋友',1)")
        conn.commit()
    with closing(sqlite3.connect(tmp_path / "session.db")) as conn:
        conn.execute("CREATE TABLE SessionTable(username,last_timestamp,summary,unread_count)")
        conn.executemany("INSERT INTO SessionTable VALUES (?,?,?,?)", [("alice",10,"hi",1),("bob",20,"ok",0)])
        conn.commit()
    for number in range(2):
        with closing(sqlite3.connect(tmp_path / f"message_{number}.db")) as conn:
            conn.execute("CREATE TABLE Name2Id(user_name)")
            conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)", (number+3,"alice"))
            for chat in ("alice", "bob"):
                table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
                conn.execute(f'CREATE TABLE "{table}"(local_id,local_type,sort_seq,create_time,server_id,real_sender_id,message_content)')
                for index in range(3):
                    stamp = 100 + number*10 + index
                    content = f"match {chat} {stamp}" if index else "not included"
                    if index == 2:
                        content = zstandard.ZstdCompressor().compress(content.encode())
                    conn.execute(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?)',
                                 (index+1, 3 if index == 1 else 1, stamp,stamp,stamp,number+3,content))
            conn.commit()
    return QueryService(tmp_path)


def test_contacts_tags_sessions_and_exact_resolution(service):
    assert [c["username"] for c in service.contacts()] == ["alice", "bob"]
    assert service.resolve_chat("alias_a") == "alice"
    assert service.resolve_chat("alice") == "alice"
    with pytest.raises(ValueError, match="歧义"):
        service.resolve_chat("同名")
    assert service.contact_tags() == [{"id":1,"name":"朋友","sort_order":1,"member_count":1}]
    assert service.tag_members("朋友")[0]["username"] == "alice"
    assert service.recent_sessions()[0]["username"] == "bob"
    json.dumps(service.contacts(), ensure_ascii=False)


def test_global_filtered_pagination_and_identity(service):
    all_rows = service.search("match", limit=200)
    assert len(all_rows) == 8
    assert service.search("match", limit=3, offset=2) == all_rows[2:5]
    assert {r["conversation"] for r in all_rows} == {"alice","bob"}
    assert len({(r["shard"],r["table"],r["local_id"]) for r in all_rows}) == 8
    assert all(r["sender_username"] == "alice" for r in all_rows)
    assert service.search("match",chat_name=["alias_a"],limit=200) == [r for r in all_rows if r["conversation"]=="alice"]
    json.dumps(all_rows)


def test_history_types_filtered_before_paging_and_order(service):
    rows=service.history("alice",msg_types=["text"],oldest_first=True,limit=1,offset=1)
    assert rows[0]["create_time"] == 102
    assert service.history("alice",limit=2)[0]["create_time"] == 112


def test_time_range_inclusive_and_date_end(service):
    base=datetime(2026,10,21,tzinfo=timezone(timedelta(hours=8))).timestamp()
    table="Msg_"+hashlib.md5(b"alice").hexdigest()
    with closing(sqlite3.connect(service.account_dir/"message_0.db")) as conn:
        conn.execute(f'DELETE FROM "{table}"')
        for index,stamp in enumerate((base-1,base,base+86399,base+86400)):
            conn.execute(f'INSERT INTO "{table}" VALUES (?,1,?,?,?,3,?)',(index+1,stamp,stamp,index,"boundary"))
        conn.commit()
    rows=service.history("alice",start_time="2026-10-21",end_time="2026-10-21")
    assert [r["create_time"] for r in rows]==[base+86399,base]
    assert len(service.history("alice",start_time="2026-10-21T00:00:00+08:00",end_time="2026-10-21T00:00:00+08:00"))==1
    with pytest.raises(ValueError):
        service.history("alice",start_time="2026-10-22",end_time="2026-10-21")


@pytest.mark.parametrize("kwargs", [{"limit":501},{"limit":True},{"offset":10001},{"offset":-1}])
def test_bounded_pagination(service,kwargs):
    with pytest.raises(ValueError):
        service.history("alice",**kwargs)


def test_missing_optional_contact_columns_and_unknown_global_chat(tmp_path):
    with closing(sqlite3.connect(tmp_path/"contact.db")) as conn:
        conn.execute("CREATE TABLE contact(username,nick_name,remark)")
        conn.execute("INSERT INTO contact VALUES ('user','昵称','')")
        conn.commit()
    with closing(sqlite3.connect(tmp_path/"message_0.db")) as conn:
        conn.execute("CREATE TABLE Msg_unknown(local_id,local_type,sort_seq,create_time,message_content)")
        conn.execute("INSERT INTO Msg_unknown VALUES (1,1,1,1,'searchable')")
        conn.commit()
    service=QueryService(tmp_path)
    assert service.contacts()[0]["alias"]==""
    assert service.search("searchable")[0]["table"]=="Msg_unknown"
    assert service.search("searchable")[0]["conversation"] is None


def test_corrupt_shard_is_not_partial_success(service):
    (service.account_dir/"message_2.db").write_bytes(b"bad database")
    with pytest.raises(ShardReadError):
        service.search("match")


def test_session_only_chat_and_scan_ceiling(service, monkeypatch):
    with closing(sqlite3.connect(service.account_dir/"session.db")) as conn:
        conn.execute("INSERT INTO SessionTable VALUES ('session_only',99,'summary',0)")
        conn.commit()
    assert service.resolve_chat("session_only") == "session_only"
    import wechat_extract.mcp_queries as module
    monkeypatch.setattr(module,"MAX_SCAN",5)
    with pytest.raises(ValueError, match="候选"):
        service.search("match", chat_name="alice")


def test_single_message_requires_identity_and_accepts_time(service):
    from wechat_extract.messages import AmbiguousMessageError
    with pytest.raises(AmbiguousMessageError):
        service.message("alice",1)
    assert service.message("alice",1,create_time=110)["shard"] == "message_1.db"
    assert service.message("alice",1,shard="message_0.db")["create_time"] == 100
    assert service.message("alice",1,create_time=999) is None
    json.dumps(service.message("alice",1,shard="message_0.db"))
    assert len(service.history("alice",limit=500)) == 6


def test_small_history_does_not_decode_full_large_chat(service,monkeypatch):
    import wechat_extract.mcp_queries as module
    monkeypatch.setattr(module,"MAX_SCAN",5)
    assert service.history("alice",limit=1)[0]["create_time"] == 112
    assert service.history("alice",limit=1,msg_types=["text"])[0]["create_time"] == 112


def test_unique_fuzzy_resolution_and_app_alias(service):
    assert service.resolve_chat("alias_") == "alice"
    assert service.tag_members("朋")[0]["username"] == "alice"
    table="Msg_"+hashlib.md5(b"alice").hexdigest()
    with closing(sqlite3.connect(service.account_dir/"message_0.db")) as conn:
        conn.execute(f'INSERT INTO "{table}" VALUES (9,?,999,999,999,3,?)',((6<<32)|49,"<msg><appmsg><type>6</type></appmsg></msg>"))
        conn.commit()
    assert service.history("alice",msg_types=["app"])[0]["local_type"] == (6<<32)|49
