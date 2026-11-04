from wechat_extract.events import (
    at_user_list, detect_events, self_username_from_account_dir, strip_group_prefix,
)
from wechat_extract.message_types import classify_message_type

ME = "wxid_me0001"
OTHER = "wxid_other01"
ROOM = "123@chatroom"
APP = 49


def row(local_type, content, *, sender=OTHER, source="", local_id=1):
    return {
        "conversation": ROOM, "shard": "message_0.db", "local_id": local_id, "server_id": 9, "create_time": 1,
        "local_type": local_type, "sender_username": sender, "content_text": content, "source_text": source,
        "message_type": classify_message_type(local_type, strip_group_prefix(content)),
    }


def appmsg(subtype, inner, title="t"):
    return f"<msg><appmsg appid=\"\" sdkver=\"0\"><title>{title}</title><type>{subtype}</type>{inner}</appmsg></msg>"


def pat_xml(patter, patted):
    return appmsg(62, f"<patinfo><fromusername>{patter}</fromusername><chatusername>{ROOM}</chatusername>"
                      f"<pattedusername>{patted}</pattedusername></patinfo>", title="拍了拍")


def test_self_username_from_account_dir():
    assert self_username_from_account_dir("wxid_abc123_1900") == "wxid_abc123"
    assert self_username_from_account_dir("wxid_abc123") == "wxid_abc123"
    assert self_username_from_account_dir("account") is None


def test_group_prefix_and_at_list():
    assert strip_group_prefix(f"{OTHER}:\n@me hi") == "@me hi"
    assert at_user_list(f"<msgsource><atuserlist><![CDATA[{ME},{OTHER}]]></atuserlist></msgsource>") == [ME, OTHER]


def test_pat_me_and_pat_other():
    (e,) = detect_events(row(APP, pat_xml(OTHER, ME), sender=None), ME)
    assert e["kind"] == "pat_me" and e["patter"] == OTHER and not e["from_self"]
    (e,) = detect_events(row(APP, pat_xml(OTHER, "wxid_third")), ME)
    assert e["kind"] == "pat"
    (e,) = detect_events(row(APP, pat_xml(ME, ME), sender=None), ME)
    assert e["kind"] == "pat_me" and e["from_self"]


def test_mentions():
    (e,) = detect_events(row(1, f"{OTHER}:\n@我 hi", source=f"<msgsource><atuserlist>{ME}</atuserlist></msgsource>"), ME)
    assert e["kind"] == "mention_me" and e["text"] == "@我 hi"
    (e,) = detect_events(row(1, "@所有人 x", source="<msgsource><atuserlist>notify@all</atuserlist></msgsource>"), ME)
    assert e["kind"] == "mention_all"
    assert detect_events(row(1, "plain", source=f"<msgsource><atuserlist>{OTHER}</atuserlist></msgsource>"), ME) == []


def test_quote_me_by_chatusr_and_by_svrid_lookup():
    direct = appmsg(57, f"<refermsg><chatusr>{ME}</chatusr><svrid>42</svrid></refermsg>", title="reply")
    (e,) = detect_events(row(APP, f"{OTHER}:\n{direct}"), ME)
    assert e["kind"] == "quote_me" and e["text"] == "reply" and e["quoted_server_id"] == 42

    group = appmsg(57, f"<refermsg><fromusr>{ROOM}</fromusr><svrid>77</svrid></refermsg>")
    assert detect_events(row(APP, group), ME) == []  # 无回查函数时无法断定
    (e,) = detect_events(row(APP, group), ME, lambda conv, svrid: ME if (conv, svrid) == (ROOM, 77) else None)
    assert e["kind"] == "quote_me"
    assert detect_events(row(APP, group), ME, lambda conv, svrid: OTHER) == []
