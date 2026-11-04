"""按完整消息身份定位本地附件；不解密、不下载、不猜测歧义候选。

路径依据：2026-10-17 对微信 4.x 附件布局的实测记录（见 docs/storage-and-shards.md）。
分片身份参考 wechatauto-replica issue #20；实现独立编写。
"""

from datetime import datetime
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sqlite3
import xml.etree.ElementTree as ET

from .messages import MessageReader, AmbiguousMessageError, ShardReadError

_MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_HEX16 = re.compile(r"[0-9a-fA-F]{16}")
_TIERS = {"original": "_h", "mid": "", "thumb": "_t"}
_EXTENSIONS = (".dat", "", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


class AmbiguousMediaError(LookupError):
    """候选不能唯一绑定到消息；调用方不能默默取第一份。"""

    def __init__(self, candidates):
        self.candidates = tuple(str(path) for path in candidates)
        super().__init__("附件存在多个候选，拒绝猜测: " + ", ".join(self.candidates))


def _component(value):
    value = str(value)
    if (not value or value in (".", "..") or value[-1:] in (" ", ".")
            or any(char in value for char in '/\\:\0')):
        raise ValueError("非法路径组成部分")
    return value


def media_output_relative_path(row):
    """输出目录包含账号、分片相对路径、表与 local_id，禁止跨片覆盖。"""
    account = hashlib.sha256(str(Path(row["account"]).resolve()).encode()).hexdigest()[:16]
    shard_parts = str(row["shard"]).split("/")
    return Path(account, *(_component(p) for p in shard_parts),
                _component(row["table"]), _component(row["local_id"]))


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("protobuf 数据截断")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("protobuf varint 过长")


def _length_fields(blob):
    """读取 protobuf 长度字段，不把任意 32 位字符串误当资源 ID。"""
    data = bytes(blob)
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if field == 0:
            raise ValueError("protobuf 字段编号无效")
        if wire == 0:
            _, offset = _varint(data, offset)
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
            if offset > len(data):
                raise ValueError("protobuf 固定字段截断")
        elif wire == 2:
            size, offset = _varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("protobuf 长度字段截断")
            yield field, data[offset:end]
            offset = end
        else:
            raise ValueError("不支持的 protobuf wire type")


def _packed_ids(blob, outer_fields, pattern):
    if not blob:
        return set()
    ids = set()
    try:
        for field, content in _length_fields(blob):
            if field in outer_fields:
                for inner_field, value in _length_fields(content):
                    if inner_field == 1:
                        text = value.decode("ascii")
                        if pattern.fullmatch(text):
                            ids.add(text.lower())
    except (ValueError, UnicodeError, TypeError):
        # 未知版本不猜测。调用方可以尝试同一消息的 XML 明确字段。
        return set()
    return ids


def _image_ids(row):
    identifiers = _packed_ids(row.get("packed_info_data"), {2, 9}, _HEX32)
    text = row.get("content_text") or ""
    start = text.find("<")
    if start >= 0 and not re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        try:
            root = ET.fromstring(text[start:])
            nodes = [root] if root.tag == "img" else root.findall("img") if root.tag == "msg" else []
            for node in nodes:
                value = node.get("md5") or ""
                if _HEX32.fullmatch(value):
                    identifiers.add(value.lower())
        except ET.ParseError:
            pass
    return identifiers


def _result(row, *, path=None, reason="", tier=None):
    identity = {name: row[name] for name in ("account", "conversation", "shard", "table", "local_id")}
    return {
        "status": "ok" if path is not None else "missing",
        "path": path,
        "reason": reason,
        "identity": identity,
        "output_relative_path": media_output_relative_path(row),
        "tier": tier,
    }


def _message(account_dir, conversation, local_id, shard):
    if not isinstance(shard, str) or not shard:
        raise ValueError("媒体定位必须提供消息的 shard")
    return MessageReader(account_dir).get_message(conversation, local_id, shard=shard)


def _missing_message(account_dir, conversation, local_id, shard):
    return _result({
        "account": str(Path(account_dir).resolve()),
        "conversation": conversation,
        "table": "Msg_" + hashlib.md5(conversation.encode()).hexdigest(),
        "shard": shard,
        "local_id": local_id,
    }, reason="message_not_found")


def _months(source_account_dir, row):
    account = Path(source_account_dir).resolve(strict=True)
    chat_hash = hashlib.md5(row["conversation"].encode("utf-8")).hexdigest()
    base = account / "msg" / "attach" / chat_hash
    if not base.resolve().is_relative_to(account):
        raise ValueError("附件目录越出账号目录")
    if not base.is_dir():
        return []
    # 时间仅用于访问顺序：延迟下载可跨月，不能以时间排除其他候选。
    try:
        preferred = datetime.fromtimestamp(row.get("create_time") or 0).strftime("%Y-%m")
    except (ValueError, OverflowError, OSError):
        preferred = ""
    months = [path for path in base.iterdir() if _MONTH.fullmatch(path.name) and path.is_dir()]
    return sorted(months, key=lambda path: (path.name != preferred, path.name))


def _existing_file(path, source_account_dir, conversation):
    account = Path(source_account_dir).resolve(strict=True)
    expected = account / "msg" / "attach" / hashlib.md5(conversation.encode()).hexdigest()
    resolved = path.resolve()
    if not resolved.is_relative_to(expected):
        raise ValueError("附件路径越出指定会话目录")
    return resolved if resolved.is_file() else None


def resolve_image(account_dir, source_account_dir, conversation, local_id, *, shard, tier="best"):
    """定位指定分片图片；返回含完整 identity 的结果，不对缺图猜测其他消息。

    tier=best 时按 original/mid/thumb 选择；返回的 tier 表示文件命名档位，
    不声称其已完整下载。不同月份或不同资源 ID 同时匹配时拒绝猜测。
    """
    if tier not in (*_TIERS, "best"):
        raise ValueError("tier 必须是 best/original/mid/thumb")
    row = _message(account_dir, conversation, local_id, shard)
    if row is None:
        return _missing_message(account_dir, conversation, local_id, shard)
    if int(row["local_type"]) & 0xFFFFFFFF != 3:
        return _result(row, reason="not_image_message")
    ids = _image_ids(row)
    # XML 的内容 MD5 与资源文件 ID 可不同；即使 XML 有 ID 仍需查询精确资源关联。
    for resource in _resources(account_dir, row):
        ids.update(_resource_ids(resource["packed_info"]))
    if not ids:
        return _result(row, reason="message_has_no_image_id")
    hits = {}
    for month in _months(source_account_dir, row):
        for identifier in sorted(ids):
            for kind, suffix in _TIERS.items():
                candidates = set()
                for extension in _EXTENSIONS:
                    path = _existing_file(month / "Img" / (identifier + suffix + extension),
                                          source_account_dir, conversation)
                    if path is not None:
                        candidates.add(path)
                if len(candidates) > 1:
                    raise AmbiguousMediaError(sorted(candidates))
                if candidates:
                    hits.setdefault((month.name, identifier), {})[kind] = candidates.pop()
    eligible = {key: files for key, files in hits.items() if tier == "best" or tier in files}
    if len(eligible) > 1:
        raise AmbiguousMediaError(path for files in eligible.values() for path in files.values())
    if not eligible:
        hardlink = _hardlink_paths(account_dir, source_account_dir, row, ids, "image")
        classified = {path: "original" if path.stem.endswith("_h") else
                       "thumb" if path.stem.endswith("_t") else "mid" for path in hardlink}
        matching = {path: kind for path, kind in classified.items() if tier == "best" or kind == tier}
        if len(matching) > 1:
            raise AmbiguousMediaError(matching)
        if matching:
            path, kind = next(iter(matching.items()))
            return _result(row, path=path, tier=kind)
        return _result(row, reason="image_not_cached")
    files = next(iter(eligible.values()))
    chosen_tier = next(kind for kind in _TIERS if kind in files) if tier == "best" else tier
    return _result(row, path=files[chosen_tier], tier=chosen_tier)


def resolve_record_file(account_dir, source_account_dir, conversation, local_id, *,
                        shard, data_index, filename):
    """按消息 field9 的 Rec ID、0 基 data_index、真实文件名定位 F 附件。"""
    if isinstance(data_index, bool) or not isinstance(data_index, int) or data_index < 0:
        raise ValueError("data_index 必须是 0 基非负整数")
    filename = _component(filename)
    row = _message(account_dir, conversation, local_id, shard)
    if row is None:
        return _missing_message(account_dir, conversation, local_id, shard)
    resource_ids = _packed_ids(row.get("packed_info_data"), {9}, _HEX16)
    if not resource_ids:
        return _result(row, reason="message_has_no_record_id")
    if len(resource_ids) > 1:
        raise AmbiguousMediaError(sorted(resource_ids))
    resource_id = next(iter(resource_ids))
    hits = []
    for month in _months(source_account_dir, row):
        path = _existing_file(
            month / "Rec" / resource_id / "F" / str(data_index) / filename,
            source_account_dir, conversation,
        )
        if path is not None:
            hits.append(path)
    if len(hits) > 1:
        raise AmbiguousMediaError(hits)
    return _result(row, path=hits[0]) if hits else _result(row, reason="record_file_not_cached")


def _databases(account_dir, pattern):
    """只在指定解密账号下寻找资源库；符号链接不能越出账号。"""
    root = Path(account_dir).resolve(strict=True)
    paths = []
    for path in root.rglob("*.db"):
        if re.fullmatch(pattern, path.name, re.IGNORECASE):
            if not path.resolve().is_relative_to(root):
                raise ValueError("资源数据库越出解密账号目录")
            paths.append(path)
    return sorted(paths)


@contextmanager
def _connect(path):
    """数据库打开或查询失败均为受控读取错误，不能伪装成媒体缺失。"""
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        yield conn
    except sqlite3.Error as exc:
        raise ShardReadError(f"资源数据库读取失败 {path.name}: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def _resources(account_dir, row):
    """同一资源库映射会话，并严格核对选定消息的可用身份字段。"""
    server = int(row.get("server_id") or 0)
    if not server:
        # local_id 跨消息分片重复。无服务端编号时必须证明该会话内唯一。
        try:
            MessageReader(account_dir).get_message(row["conversation"], row["local_id"])
        except AmbiguousMessageError:
            return []
    hits = []
    for path in _databases(account_dir, r"(?:message__)?message_resource\.db"):
        with _connect(path) as conn:
            where = ["c.user_name=?", "i.message_local_id=?", "i.message_local_type IN (?,?)"]
            params = [row["conversation"], row["local_id"], row["local_type"], int(row["local_type"]) & 0xFFFFFFFF]
            if server:
                where.append("i.message_svr_id=?")
                params.append(server)
            if row.get("create_time"):
                where.append("i.message_create_time=?")
                params.append(row["create_time"])
            matches = conn.execute(
                "SELECT i.* FROM MessageResourceInfo i JOIN ChatName2Id c ON c.rowid=i.chat_id WHERE "
                + " AND ".join(where), params,
            ).fetchall()
            for match in matches:
                item = dict(match)
                item["database"] = path
                hits.append(item)
    if len(hits) > 1:
        raise AmbiguousMediaError(f"{r['database']}:{r['message_id']}" for r in hits)
    return hits


def _resource_ids(blob):
    if not blob:
        return set()
    if isinstance(blob, str):
        return {blob.lower()} if _HEX32.fullmatch(blob) else set()
    try:
        text = bytes(blob).decode("ascii")
        if _HEX32.fullmatch(text):
            return {text.lower()}
    except UnicodeError:
        pass
    return _packed_ids(blob, {2, 9}, _HEX32)


def _xml(row):
    text = row.get("content_text") or ""
    start = text.find("<")
    if start < 0 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def _local_path(path, source_account_dir, kind, conversation=None):
    account = Path(source_account_dir).resolve(strict=True)
    base = account / "msg" / kind
    if conversation is not None:
        base = base / hashlib.md5(conversation.encode()).hexdigest()
    resolved = path.resolve()
    if not resolved.is_relative_to(base) or not resolved.is_relative_to(account):
        raise ValueError("资源索引路径越出指定媒体目录")
    return resolved if resolved.is_file() else None


def _hardlink_paths(account_dir, source_account_dir, row, md5s, kind):
    """hardlink 只按明确 MD5 查询，目录映射来自同库，不挑第一条候选。"""
    hits = set()
    if not md5s:
        return hits
    account = Path(source_account_dir).resolve(strict=True)
    for path in _databases(account_dir, r"(?:hardlink__)?hardlink\.db"):
        with _connect(path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = f"{kind}_hardlink_info_v4"
            if table not in tables:
                continue
            dirs = {r[0]: r[1] for r in conn.execute("SELECT rowid,username FROM dir2id")}
            for md5 in sorted(md5s):
                for entry in conn.execute(f'SELECT * FROM "{table}" WHERE md5=? COLLATE NOCASE', (md5,)):
                    name = _component(entry["file_name"])
                    mapped = [str(dirs.get(entry[key], "")) for key in ("dir1", "dir2")]
                    months = [value for value in mapped if _MONTH.fullmatch(value)]
                    if kind == "image":
                        chat_hash = hashlib.md5(row["conversation"].encode()).hexdigest()
                        if any(_HEX32.fullmatch(value) and value.lower() != chat_hash for value in mapped):
                            continue
                        bases = [p for p in _months(account, row) if not months or p.name in months]
                        candidates = [p / "Img" / name for p in bases]
                    else:
                        base = account / "msg" / kind
                        bases = [p for p in base.iterdir() if p.is_dir() and _MONTH.fullmatch(p.name)
                                 and (not months or p.name in months)] if base.is_dir() else []
                        candidates = [p / name for p in bases]
                        if kind == "file":
                            candidates += [p / Path(name).stem / name for p in bases]
                    for candidate in candidates:
                        found = _local_path(candidate, account, "attach" if kind == "image" else kind,
                                            row["conversation"] if kind == "image" else None)
                        if found is not None:
                            if entry["file_size"] and found.stat().st_size != entry["file_size"]:
                                continue
                            hits.add(found)
    return hits


def _media_result(row, hits, reason):
    if len(hits) > 1:
        raise AmbiguousMediaError(sorted(hits))
    return _result(row, path=next(iter(hits))) if hits else _result(row, reason=reason)


def read_voice(account_dir, conversation, local_id, *, shard):
    """按各 media 分片自身 Name2Id + svr_id 读取原始语音字节，不转码。"""
    row = _message(account_dir, conversation, local_id, shard)
    if row is None:
        result = _missing_message(account_dir, conversation, local_id, shard)
        result.update(data=None, media_shard=None, format="silk")
        return result
    result = _result(row, reason="not_voice_message")
    result.update(data=None, media_shard=None, format="silk")
    if int(row["local_type"]) & 0xFFFFFFFF != 34:
        return result
    server = int(row.get("server_id") or 0)
    if not server:
        result["reason"] = "no_server_id"
        return result
    paths = _databases(account_dir, r"(?:message__)?media_\d+\.db")
    known, empty, hits = False, False, []
    for path in paths:
        with _connect(path) as conn:
            ids = [r[0] for r in conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (conversation,))]
            known |= bool(ids)
            for chat_id in ids:
                for voice in conn.execute(
                    "SELECT rowid,voice_data FROM VoiceInfo WHERE chat_name_id=? AND svr_id=?",
                    (chat_id, server),
                ):
                    if voice["voice_data"]:
                        hits.append((path, voice["rowid"], bytes(voice["voice_data"])))
                    else:
                        empty = True
    if len(hits) > 1:
        raise AmbiguousMediaError(f"{path}:{number}" for path, number, _ in hits)
    if hits:
        path, _, data = hits[0]
        result.update(status="ok", reason="", data=data,
                      media_shard=path.relative_to(Path(account_dir).resolve()).as_posix())
    else:
        result["reason"] = ("media_database_missing" if not paths else
                            "session_not_in_media_index" if not known else
                            "empty_blob" if empty else
                            "audio_not_downloaded" if row.get("download_status") == 0 else
                            "audio_missing_from_media_db" if row.get("download_status") is not None else
                            "audio_availability_unknown")
    return result


def _resolve_file_media(account_dir, source_account_dir, conversation, local_id, shard, kind):
    row = _message(account_dir, conversation, local_id, shard)
    if row is None:
        return _missing_message(account_dir, conversation, local_id, shard)
    local_type = int(row["local_type"])
    root = _xml(row)
    if kind == "video":
        if local_type & 0xFFFFFFFF not in (43, 62):
            return _result(row, reason="not_video_message")
        nodes = [] if root is None else ([root] if root.tag == "videomsg" else root.findall("videomsg"))
        md5s = {node.get("md5").lower() for node in nodes if _HEX32.fullmatch(node.get("md5") or "")}
        ids = _packed_ids(row.get("packed_info_data"), {3, 9}, _HEX32)
        names = {identifier + ".mp4" for identifier in ids | md5s}
    else:
        app = None if root is None else root if root.tag == "appmsg" else root.find("appmsg")
        subtype = app.findtext("type") if app is not None else None
        if local_type & 0xFFFFFFFF != 49 or not (local_type >> 32 == 6 or subtype == "6"):
            return _result(row, reason="not_file_message")
        md5s = set()
        if app is not None:
            for tag in ("appattach/filemd5", "appattach/md5", "md5"):
                value = app.findtext(tag) or ""
                if _HEX32.fullmatch(value):
                    md5s.add(value.lower())
        names = set()
    resources = _resources(account_dir, row)
    for resource in resources:
        md5s.update(_resource_ids(resource["packed_info"]))
        # Detail 的纯文件名不能证明磁盘文件归属：不同消息可以同名。
        # 普通文件只接受明确 MD5 到 hardlink 的绑定，不做跨月文件名扫描。
    hits = _hardlink_paths(account_dir, source_account_dir, row, md5s, kind)
    if kind == "video":
        names.update(value + ".mp4" for value in md5s)
    base = Path(source_account_dir).resolve(strict=True) / "msg" / kind
    if base.is_dir():
        for month in base.iterdir():
            if not month.is_dir() or not _MONTH.fullmatch(month.name):
                continue
            for name in names:
                candidate = _local_path(month / _component(name), source_account_dir, kind)
                if candidate is not None:
                    hits.add(candidate)
    return _media_result(row, hits, f"{kind}_not_cached")


def resolve_video(account_dir, source_account_dir, conversation, local_id, *, shard):
    return _resolve_file_media(account_dir, source_account_dir, conversation, local_id, shard, "video")


def resolve_file(account_dir, source_account_dir, conversation, local_id, *, shard):
    return _resolve_file_media(account_dir, source_account_dir, conversation, local_id, shard, "file")
