"""只读合并解密后的消息分片；消息身份始终包含账户、会话、分片和本地编号。"""

from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3

from .message_types import classify_message_type


class AmbiguousMessageError(LookupError):
    """同一会话的 local_id 在多个分片命中，调用方必须指定 shard。"""


class ShardReadError(RuntimeError):
    """分片损坏或结构不兼容，不能把不完整结果当成完整消息列表。"""


def decode_message_content(message_content, compress_content=None) -> str:
    """优先使用非空压缩内容；保留解码失败，不用乱码或空串掩盖损坏。"""
    value = compress_content if compress_content not in (None, b"", "") else message_content
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    value = bytes(value)
    if value.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            import zstandard
        except ImportError as exc:
            raise RuntimeError("读取压缩消息需要 zstandard，请安装项目运行依赖") from exc
        # 先验证已声明大小，再限定未知大小帧；decompress 会拒绝截断帧。
        maximum = 16 * 1024 * 1024
        try:
            size = zstandard.frame_content_size(value)
            if size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and size > maximum:
                raise ValueError("消息展开后超过 16 MiB")
            value = zstandard.ZstdDecompressor().decompress(value, max_output_size=maximum)
        except zstandard.ZstdError as exc:
            raise ValueError(f"消息 zstd 解压失败: {exc}") from exc
    return value.decode("utf-8")


class MessageReader:
    """account_dir 必须是单一账户的解密输出目录，不会读取真实微信原始目录。"""

    def __init__(self, account_dir: str | Path):
        self.account_dir = Path(account_dir).resolve()
        if not self.account_dir.is_dir():
            raise NotADirectoryError(self.account_dir)

    def shards(self) -> list[str]:
        """包括扁平、保留子目录和旧版双下划线缓存命名，标识不只取 basename。"""
        result = []
        for path in self.account_dir.rglob("*"):
            if path.is_file() and re.fullmatch(
                r"(?:message__)?(?:biz_)?message(?:_\d+)?\.db", path.name, re.IGNORECASE
            ):
                if not path.resolve().is_relative_to(self.account_dir):
                    raise ShardReadError(f"分片指向账户目录外: {path}")
                result.append(path.relative_to(self.account_dir).as_posix())
        return sorted(result)

    def _read(self, conversation, *, local_id=None, server_id=None, shard=None, cap=None, descending=True):
        if not isinstance(conversation, str) or not conversation:
            raise ValueError("conversation 必须是非空微信用户名或群 ID")
        expected = "Msg_" + hashlib.md5(conversation.encode("utf-8")).hexdigest()
        shards = self.shards()
        if shard is not None:
            if shard not in shards:
                raise FileNotFoundError(f"账户内没有指定分片: {shard}")
            shards = [shard]
        rows = []
        for label in shards:
            path = self.account_dir / label
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA query_only=ON")
                    table_row = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE",
                        (expected,),
                    ).fetchone()
                    if table_row is None:
                        continue
                    table = table_row["name"]
                    quoted = '"' + table.replace('"', '""') + '"'
                    names = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='Name2Id' COLLATE NOCASE"
                    ).fetchone()
                    senders = (
                        {r[0]: r[1] for r in conn.execute("SELECT rowid, user_name FROM Name2Id")}
                        if names else {}
                    )
                    query = f"SELECT * FROM {quoted}"
                    params = []
                    if local_id is not None:
                        query += " WHERE local_id=?"
                        params.append(local_id)
                    elif server_id is not None:
                        query += " WHERE server_id=?"
                        params.append(server_id)
                    query += " ORDER BY sort_seq " + ("DESC" if descending else "ASC") + ", local_id ASC"
                    if cap is not None:
                        query += " LIMIT ?"
                        params.append(cap)
                    for raw in conn.execute(query, params):
                        row = dict(raw)
                        text = decode_message_content(row.get("message_content"), row.get("compress_content"))
                        row.update(
                            account=str(self.account_dir), conversation=conversation,
                            table=table, shard=label,
                            sender_username=senders.get(row.get("real_sender_id")),
                            content_text=text,
                            # msgsource（含 @ 列表 atuserlist 等），与正文同样可能为 zstd 压缩
                            source_text=decode_message_content(row.get("source")),
                            message_type=classify_message_type(row["local_type"], text),
                        )
                        rows.append(row)
            except (sqlite3.Error, UnicodeError, ValueError, TypeError, RuntimeError, KeyError) as exc:
                raise ShardReadError(f"无法完整读取分片 {label}: {exc}") from exc
        # 顺序固定为 sort_seq、分片路径、local_id；跨片不根据相同时间/编号去重。
        rows.sort(key=lambda r: (
            -(r["sort_seq"] or 0) if descending else (r["sort_seq"] or 0),
            r["shard"], r["local_id"],
        ))
        return rows

    def list_messages(self, conversation: str, *, limit: int = 100, offset: int = 0,
                      descending: bool = True) -> list[dict]:
        """跨片合并后分页；任何一个分片失败都会报错，不返回无标记的部分结果。"""
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (limit, offset)):
            raise ValueError("limit 和 offset 必须是非负整数")
        if limit == 0:
            return []
        rows = self._read(conversation, cap=limit + offset, descending=descending)
        return rows[offset:offset + limit]

    def find_by_server_id(self, conversation: str, server_id: int) -> list[dict]:
        """按服务端消息 ID（如引用里的 svrid）回查本会话消息；可能跨片命中多条。"""
        if isinstance(server_id, bool) or not isinstance(server_id, int):
            raise TypeError("server_id 必须是整数")
        return self._read(conversation, server_id=server_id)

    def get_message(self, conversation: str, local_id: int, *, shard: str | None = None) -> dict | None:
        """按身份回读一条消息供媒体层消费；碰撞必须由调用方提供分片标识。"""
        if isinstance(local_id, bool) or not isinstance(local_id, int):
            raise TypeError("local_id 必须是整数")
        rows = self._read(conversation, local_id=local_id, shard=shard)
        if len(rows) > 1:
            labels = ", ".join(row["shard"] for row in rows)
            raise AmbiguousMessageError(
                f"local_id={local_id} 命中 {len(rows)} 条消息（{labels}），请指定 shard"
            )
        return rows[0] if rows else None
