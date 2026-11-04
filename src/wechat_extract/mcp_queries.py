"""MCP 的只读查询后端；跨片过滤后分页，不持有刷新前缓存。"""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import heapq
import sqlite3

from .media import _length_fields
from .messages import MessageReader, ShardReadError, AmbiguousMessageError, decode_message_content
from .message_types import classify_message_type

MAX_SCAN = 100_000
LOCAL_TIME = timezone(timedelta(hours=8))
TYPE_ALIASES = {"text": 1, "image": 3, "voice": 34, "namecard": 42, "video": 43,
                "emoji": 47, "location": 48, "app": 49, "voip": 50, "system": 10000}


def _page(limit, offset=0):
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500
            or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000):
        raise ValueError("limit 必须为 0..500，offset 必须为 0..10000 的整数")


def _times(start, end):
    def parse(value, last=False):
        if not value:
            return None
        if not isinstance(value, str):
            raise ValueError("时间须为 ISO 日期或日期时间字符串")
        try:
            result = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("时间格式无效，请用 YYYY-MM-DD 或 ISO 日期时间") from exc
        if len(value) == 10 and last:
            result += timedelta(days=1)
            return result.replace(tzinfo=LOCAL_TIME).timestamp() - 0.000001
        return (result if result.tzinfo else result.replace(tzinfo=LOCAL_TIME)).timestamp()
    low, high = parse(start), parse(end, True)
    if low is not None and high is not None and low > high:
        raise ValueError("start_time 不能晚于 end_time")
    return low, high


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""


class QueryService:
    def __init__(self, account_dir):
        self.reader = MessageReader(account_dir)
        self.account_dir = self.reader.account_dir

    @contextmanager
    def _open(self, path):
        if not path.resolve().is_relative_to(self.account_dir):
            raise ShardReadError("查询数据库越出账号目录")
        conn = None
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            yield conn
        except sqlite3.Error as exc:
            raise ShardReadError(f"查询数据库失败 {path.name}: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def _database(self, name):
        paths = sorted(p for p in self.account_dir.rglob("*.db")
                       if p.name == name or p.name.endswith("__" + name))
        if len(paths) > 1:
            raise ValueError(f"找到多个 {name}，请使用单一账号快照")
        return paths[0] if paths else None

    def _contacts(self):
        path = self._database("contact.db")
        if path is None:
            return []
        with self._open(path) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(contact)")}
            if "username" not in columns:
                raise ShardReadError("contact 表缺少 username")
            sql = "SELECT * FROM contact"
            if "local_type" in columns:
                sql += " WHERE local_type IS NULL OR local_type != 3"
            rows = conn.execute(sql + " LIMIT ?", (MAX_SCAN + 1,)).fetchall()
        if len(rows) > MAX_SCAN:
            raise ValueError("联系人超过查询上限")
        result = []
        for raw in rows:
            row = dict(raw)
            item = {key: _text(row.get(key)) for key in ("username", "nick_name", "remark", "alias", "description")}
            item["display_name"] = item["remark"] or item["nick_name"] or item["username"]
            item["extra_buffer"] = row.get("extra_buffer")
            result.append(item)
        return result

    def contacts(self, query="", limit=50):
        _page(limit)
        if not isinstance(query, str):
            raise ValueError("query 必须是字符串")
        matched = [row for row in self._contacts() if not query or any(
            query.casefold() in row[key].casefold() for key in ("username", "alias", "remark", "nick_name"))]
        return [{k: v for k, v in row.items() if k != "extra_buffer"}
                for row in sorted(matched, key=lambda r: r["username"])[:limit]]

    def _tags(self):
        path = self._database("contact.db")
        if path is None:
            return []
        with self._open(path) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='contact_label'").fetchone():
                return []
            rows = conn.execute("SELECT label_id_,label_name_,sort_order_ FROM contact_label ORDER BY sort_order_ LIMIT ?",
                                (MAX_SCAN + 1,)).fetchall()
        if len(rows) > MAX_SCAN:
            raise ValueError("标签超过查询上限")
        tags = {str(r[0]): {"id": r[0], "name": _text(r[1]), "sort_order": r[2], "members": []} for r in rows}
        for row in self._contacts():
            blob = row.pop("extra_buffer")
            if not blob:
                continue
            try:
                ids = {part.strip() for field, data in _length_fields(blob) if field == 30
                       for part in data.decode("utf-8").split(",")}
            except (ValueError, UnicodeError, TypeError) as exc:
                raise ShardReadError("联系人标签数据无法解析") from exc
            for identifier in ids:
                if identifier in tags:
                    tags[identifier]["members"].append(row)
        return list(tags.values())

    def contact_tags(self):
        return [{k: v for k, v in row.items() if k != "members"} | {"member_count": len(row["members"])}
                for row in self._tags()]

    def tag_members(self, tag_name):
        if not isinstance(tag_name, str) or not tag_name.strip():
            raise ValueError("tag_name 必须是非空字符串")
        tags = self._tags()
        matches = [tag for tag in tags if tag["name"] == tag_name]
        if not matches:
            matches = [tag for tag in tags if tag_name.casefold() in tag["name"].casefold()]
        if len(matches) > 1:
            raise ValueError("标签名称有歧义")
        return matches[0]["members"] if matches else []

    def display_name(self, username):
        """会话/联系人在界面上显示的名称（备注 > 昵称 > username）。"""
        for row in self._contacts():
            if row["username"] == username:
                return row["display_name"]
        return username

    def recent_sessions(self, limit=20):
        _page(limit)
        path = self._database("session.db")
        if path is None:
            return []
        names = {r["username"]: r["display_name"] for r in self._contacts()}
        with self._open(path) as conn:
            rows = conn.execute("SELECT * FROM SessionTable WHERE last_timestamp>0 ORDER BY last_timestamp DESC,username LIMIT ?",
                                (limit,)).fetchall()
        fields = ("username", "unread_count", "summary", "last_timestamp", "last_msg_type", "last_msg_sender", "last_sender_display_name")
        return [{key: _text(dict(row).get(key)) if key in ("username", "summary", "last_msg_sender", "last_sender_display_name")
                 else dict(row).get(key) for key in fields} | {"display_name": names.get(row["username"], row["username"])} for row in rows]

    def _usernames(self):
        names = {r["username"] for r in self._contacts()}
        session = self._database("session.db")
        if session is not None:
            with self._open(session) as conn:
                names.update(_text(r[0]) for r in conn.execute("SELECT username FROM SessionTable LIMIT ?", (MAX_SCAN + 1,)))
        for shard in self.reader.shards():
            with self._open(self.account_dir / shard) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='Name2Id'").fetchone():
                    names.update(_text(r[0]) for r in conn.execute("SELECT user_name FROM Name2Id LIMIT ?", (MAX_SCAN + 1,)))
            if len(names) > MAX_SCAN:
                raise ValueError("账号名称索引超过查询上限")
        return names

    def resolve_chat(self, chat_name):
        if not isinstance(chat_name, str) or not chat_name:
            raise ValueError("chat_name 必须为非空字符串")
        if chat_name in self._usernames():
            return chat_name
        matches = {r["username"] for r in self._contacts()
                   if chat_name in (r["alias"], r["remark"], r["nick_name"])}
        if len(matches) > 1:
            raise ValueError("会话名称有歧义，请指定 username")
        if matches:
            return matches.pop()
        # 允许联系人已删除但消息表仍在的精确 username。
        table = "Msg_" + hashlib.md5(chat_name.encode()).hexdigest()
        for shard in self.reader.shards():
            with self._open(self.account_dir / shard) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE", (table,)).fetchone():
                    return chat_name
        fuzzy = {r["username"] for r in self._contacts() if any(chat_name.casefold() in r[key].casefold()
                  for key in ("username", "alias", "remark", "nick_name"))}
        if len(fuzzy) > 1:
            raise ValueError("会话名称有歧义，请指定 username")
        if fuzzy:
            return fuzzy.pop()
        raise ValueError("未找到会话")

    def _query(self, chats, keyword, limit, offset, start_time, end_time, oldest_first=False, msg_types=None):
        _page(limit, offset)
        low, high = _times(start_time, end_time)
        if limit == 0:
            return []
        if msg_types is not None and (not isinstance(msg_types, list) or any(not isinstance(v, (str, int)) for v in msg_types)):
            raise ValueError("msg_types 必须是类型名称或编号列表")
        contacts = self._contacts()
        names = {r["username"]: r["display_name"] for r in contacts}
        usernames = self._usernames()
        table_users = {"msg_" + hashlib.md5(name.encode()).hexdigest(): name for name in usernames}
        wanted = {"msg_" + hashlib.md5(name.encode()).hexdigest(): name for name in chats} if chats else None
        if wanted:
            table_users.update(wanted)
        count = 0

        def rows():
            nonlocal count
            for shard in self.reader.shards():
                with self._open(self.account_dir / shard) as conn:
                    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'")]
                    senders = {r[0]: _text(r[1]) for r in conn.execute("SELECT rowid,user_name FROM Name2Id")} if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='Name2Id'").fetchone() else {}
                    for table in tables:
                        if wanted is not None and table.lower() not in wanted:
                            continue
                        quoted = '"' + table.replace('"', '""') + '"'
                        where, params = [], []
                        if low is not None:
                            where.append("create_time>=?"); params.append(low)
                        if high is not None:
                            where.append("create_time<=?"); params.append(high)
                        sql = "SELECT * FROM " + quoted + (" WHERE " + " AND ".join(where) if where else "")
                        direction = "ASC" if oldest_first else "DESC"
                        sql += f" ORDER BY create_time {direction},sort_seq {direction},local_id ASC"
                        matched = 0
                        for raw in conn.execute(sql, params):
                            count += 1
                            if count > MAX_SCAN:
                                raise ValueError("查询超过 100000 条候选，请缩小会话或时间范围")
                            row = dict(raw)
                            text = decode_message_content(row.get("message_content"), row.get("compress_content"))
                            if keyword and keyword.casefold() not in text.casefold():
                                continue
                            kind = classify_message_type(row["local_type"], text)
                            if msg_types and not any(str(value).lower() in (str(row["local_type"]), str(kind.base_type), kind.category, kind.label)
                                                    or TYPE_ALIASES.get(str(value).strip().lower()) == kind.base_type for value in msg_types):
                                continue
                            sender = senders.get(row.get("real_sender_id"))
                            item = {key: row.get(key) for key in ("local_id", "server_id", "create_time", "sort_seq", "local_type")}
                            item.update(account=str(self.account_dir), shard=shard, table=table,
                                        conversation=table_users.get(table.lower()), sender_username=sender,
                                        sender=names.get(sender, sender), content_text=text, message_type=asdict(kind))
                            yield item
                            matched += 1
                            if matched >= limit + offset:
                                break
        def order(row):
            direction = 1 if oldest_first else -1
            return (direction * (row["create_time"] or 0), direction * (row["sort_seq"] or 0), row["shard"], row["table"], row["local_id"])
        try:
            return heapq.nsmallest(limit + offset, rows(), key=order)[offset:]
        except (UnicodeError, sqlite3.Error) as exc:
            raise ShardReadError(f"消息读取失败: {exc}") from exc

    def history(self, chat_name, limit=50, offset=0, start_time="", end_time="", oldest_first=False, msg_types=None):
        if not isinstance(oldest_first, bool):
            raise ValueError("oldest_first 必须是布尔值")
        return self._query([self.resolve_chat(chat_name)], "", limit, offset, start_time, end_time, oldest_first, msg_types)

    def message(self, chat_name, local_id, shard=None, create_time=0):
        """单条消息回读；跨片同号须显式分片或唯一时间消歧。"""
        if isinstance(local_id, bool) or not isinstance(local_id, int):
            raise ValueError("local_id 必须是整数")
        if isinstance(create_time, bool) or not isinstance(create_time, int) or create_time < 0:
            raise ValueError("create_time 必须为非负整数")
        username = self.resolve_chat(chat_name)
        rows = self.reader._read(username, local_id=local_id, shard=shard)
        if create_time:
            rows = [row for row in rows if row.get("create_time") == create_time]
        if len(rows) > 1:
            raise AmbiguousMessageError("消息编号跨分片重复，请指定 shard")
        if not rows:
            return None
        row = rows[0]
        keys = ("account", "conversation", "shard", "table", "local_id", "server_id", "create_time",
                "sort_seq", "local_type", "sender_username", "content_text")
        result = {key: row.get(key) for key in keys}
        names = {r["username"]: r["display_name"] for r in self._contacts()}
        result.update(sender=names.get(row["sender_username"], row["sender_username"]),
                      message_type=asdict(row["message_type"]))
        return result

    def search(self, keyword, chat_name="", limit=20, offset=0, start_time="", end_time=""):
        if not isinstance(keyword, str) or not keyword:
            raise ValueError("keyword 必须为非空字符串")
        if not isinstance(chat_name, (str, list)):
            raise ValueError("chat_name 必须是字符串或字符串列表")
        selected = chat_name if isinstance(chat_name, list) else [chat_name] if chat_name else []
        return self._query([self.resolve_chat(name) for name in selected], keyword, limit, offset, start_time, end_time)
