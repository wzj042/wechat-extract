"""资源关联测试只创建合成数据库和附件。"""
from contextlib import closing
import hashlib
import sqlite3

import pytest

from wechat_extract.media import (
    AmbiguousMediaError, read_voice, resolve_file, resolve_image, resolve_video,
)
from wechat_extract.messages import ShardReadError


def message(account, *, shard="message_0.db", kind=3, server=101, timestamp=123, content="", download=1):
    account.mkdir(exist_ok=True)
    table = "Msg_" + hashlib.md5(b"room").hexdigest()
    with closing(sqlite3.connect(account / shard)) as conn:
        conn.execute(f'CREATE TABLE "{table}" (local_id,local_type,sort_seq,create_time,server_id,'
                     'message_content,packed_info_data,download_status)')
        conn.execute(f'INSERT INTO "{table}" VALUES (1,?,1,?,?,?,NULL,?)',
                     (kind, timestamp, server, content, download))
        conn.commit()


def resource(account, *, kind=3, server=101, timestamp=123, name=None, chat="room", identifier="a"*32):
    with closing(sqlite3.connect(account / "message_resource.db")) as conn:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS ChatName2Id(user_name);"
            "CREATE TABLE IF NOT EXISTS MessageResourceInfo(message_id,chat_id,message_local_id,"
            "message_local_type,message_svr_id,message_create_time,packed_info);"
            "CREATE TABLE IF NOT EXISTS MessageResourceDetail(message_id,packed_info);"
        )
        cid = conn.execute("INSERT INTO ChatName2Id VALUES (?)", (chat,)).lastrowid
        packed = b"\x12\x22\x0a\x20" + identifier.encode()
        mid = conn.execute("INSERT INTO MessageResourceInfo VALUES (?,?,?,?,?,?,?)",
                           (cid, cid, 1, kind, server, timestamp, packed)).lastrowid
        if name is not None:
            conn.execute("INSERT INTO MessageResourceDetail VALUES (?,?)", (mid, name.encode()))
        conn.commit()


def attachment(source, *parts, data=b"media"):
    path = source.joinpath("msg", *parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.resolve()


def test_resource_image_scopes_chat_time_type_and_server(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account, content='<msg><img md5="'+"c"*32+'"/></msg>')
    for kwargs in ({"chat": "other"}, {"timestamp": 999}, {"server": 999}, {"kind": 43}):
        resource(account, **kwargs)
    resource(account, identifier="b"*32)
    expected = attachment(source, "attach", hashlib.md5(b"room").hexdigest(), "2026-10", "Img", "b"*32+".dat")
    assert resolve_image(account, source, "room", 1, shard="message_0.db")["path"] == expected


def test_resource_duplicates_are_ambiguous(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account)
    resource(account)
    resource(account, identifier="b"*32)
    source.mkdir()
    with pytest.raises(AmbiguousMediaError):
        resolve_image(account, source, "room", 1, shard="message_0.db")


def test_weak_resource_cannot_bind_cross_shard_collision(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account, server=0, timestamp=0)
    message(account, shard="message_1.db", server=0, timestamp=0)
    resource(account, server=0, timestamp=0)
    attachment(source, "attach", hashlib.md5(b"room").hexdigest(), "2026-10", "Img", "a"*32+".dat")
    result = resolve_image(account, source, "room", 1, shard="message_0.db")
    assert result["status"] == "missing"


def voice_db(account, number, *, cid=1, data=b"#!SILK_V3", chat="room", server=101):
    with closing(sqlite3.connect(account/f"media_{number}.db")) as conn:
        conn.executescript("CREATE TABLE Name2Id(user_name);"
                           "CREATE TABLE VoiceInfo(chat_name_id,svr_id,voice_data);")
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)", (cid,chat))
        conn.execute("INSERT INTO VoiceInfo VALUES (?,?,?)", (cid,server,data))
        conn.commit()


def test_voice_uses_each_shards_chat_mapping_and_full_message_identity(tmp_path):
    account = tmp_path/"db"
    message(account, kind=34)
    voice_db(account, 0, cid=2, chat="other")
    voice_db(account, 1, cid=19)
    result = read_voice(account, "room", 1, shard="message_0.db")
    assert result["data"] == b"#!SILK_V3"
    assert result["media_shard"] == "media_1.db"
    assert result["identity"]["shard"] == "message_0.db"
    voice_db(account, 2, cid=34)
    with pytest.raises(AmbiguousMediaError):
        read_voice(account, "room", 1, shard="message_0.db")


@pytest.mark.parametrize("download,expected", [(0,"audio_not_downloaded"), (1,"audio_missing_from_media_db"),
                                             (None,"audio_availability_unknown")])
def test_voice_missing_reasons(tmp_path, download, expected):
    account = tmp_path/"db"
    message(account, kind=34, download=download)
    voice_db(account, 0, server=999)
    assert read_voice(account,"room",1,shard="message_0.db")["reason"] == expected


def test_voice_empty_blob_is_distinct(tmp_path):
    account = tmp_path/"db"
    message(account, kind=34)
    voice_db(account, 0, data=b"")
    assert read_voice(account,"room",1,shard="message_0.db")["reason"] == "empty_blob"


def test_file_resource_filename_alone_does_not_bind_foreign_file(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account, kind=(6<<32)|49)
    resource(account, kind=49, name="report.pdf")
    attachment(source,"file","2026-10","report.pdf")
    result = resolve_file(account,source,"room",1,shard="message_0.db")
    assert result["status"] == "missing"
    attachment(source,"file","2026-11","report.pdf")
    assert resolve_file(account,source,"room",1,shard="message_0.db")["status"] == "missing"


def test_video_only_matches_exact_message_identifier(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account,kind=43,content='<msg><videomsg md5="'+"b"*32+'"/></msg>')
    attachment(source,"video","2026-10","a"*32+".mp4")
    expected=attachment(source,"video","2026-10","b"*32+".mp4")
    assert resolve_video(account,source,"room",1,shard="message_0.db")["path"] == expected


def test_hardlink_exact_md5_and_directory_mapping(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account,kind=43,content='<msg><videomsg md5="'+"b"*32+'"/></msg>')
    with closing(sqlite3.connect(account/"hardlink.db")) as conn:
        conn.executescript("CREATE TABLE dir2id(username);"
                           "CREATE TABLE video_hardlink_info_v4(md5,file_name,file_size,dir1,dir2);")
        conn.execute("INSERT INTO dir2id VALUES ('2026-10')")
        conn.execute("INSERT INTO video_hardlink_info_v4 VALUES (?,?,?,1,1)", ("b"*32,"exact.mp4",5))
        conn.execute("INSERT INTO video_hardlink_info_v4 VALUES (?,?,?,1,1)", ("c"*32,"wrong.mp4",5))
        conn.commit()
    expected=attachment(source,"video","2026-10","exact.mp4")
    attachment(source,"video","2026-10","wrong.mp4")
    assert resolve_video(account,source,"room",1,shard="message_0.db")["path"] == expected


def test_resource_filename_traversal_is_not_followed(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account, kind=(6<<32)|49)
    resource(account, kind=49, name="../outside.pdf")
    source.mkdir()
    assert resolve_file(account,source,"room",1,shard="message_0.db")["status"] == "missing"


def test_hardlink_rejects_path_traversal(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account,kind=43,content='<msg><videomsg md5="'+"b"*32+'"/></msg>')
    source.mkdir()
    with closing(sqlite3.connect(account/"hardlink.db")) as conn:
        conn.executescript("CREATE TABLE dir2id(username);"
                           "CREATE TABLE video_hardlink_info_v4(md5,file_name,file_size,dir1,dir2);")
        conn.execute("INSERT INTO video_hardlink_info_v4 VALUES (?,?,0,0,0)", ("b"*32,"../../outside.mp4"))
        conn.commit()
    with pytest.raises(ValueError, match="非法路径"):
        resolve_video(account,source,"room",1,shard="message_0.db")


def test_file_uses_exact_hardlink_md5_binding(tmp_path):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account, kind=(6<<32)|49,
            content='<msg><appmsg><type>6</type><appattach><filemd5>'+"b"*32+
                    '</filemd5></appattach></appmsg></msg>')
    with closing(sqlite3.connect(account/"hardlink.db")) as conn:
        conn.executescript("CREATE TABLE dir2id(username);"
                           "CREATE TABLE file_hardlink_info_v4(md5,file_name,file_size,dir1,dir2);"
                           "INSERT INTO dir2id VALUES ('2026-10');")
        conn.execute("INSERT INTO file_hardlink_info_v4 VALUES (?,?,5,1,1)", ("b"*32,"report.pdf"))
        conn.commit()
    expected = attachment(source,"file","2026-10","report.pdf")
    attachment(source,"file","2026-11","report.pdf")
    assert resolve_file(account,source,"room",1,shard="message_0.db")["path"] == expected


@pytest.mark.parametrize("corrupt", [False, True])
def test_resource_database_errors_are_controlled_read_errors(tmp_path, corrupt):
    account, source = tmp_path/"db", tmp_path/"source"
    message(account)
    source.mkdir()
    path = account/"message_resource.db"
    if corrupt:
        path.write_bytes(b"not sqlite")
    else:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE unrelated(value)")
    with pytest.raises(ShardReadError, match="资源数据库读取失败"):
        resolve_image(account,source,"room",1,shard="message_0.db")
