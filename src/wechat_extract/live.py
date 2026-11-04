"""实时增量解密：快照源库、原子替换输出、只读访问。

设计原则（详见 README「并发与锁」）:

1. 源库只做字节级读取（``open(..., "rb")`` / ``shutil.copyfile``），绝不用
   sqlite3 打开源库——无锁共享读，微信进程可正常写入。
2. 输出原子替换：先解密到同目录临时文件，再 ``os.replace`` 覆盖目标，
   读者永远看到完整文件。
3. 读者通过 :func:`open_readonly` 的 ``mode=ro`` 只读连接访问解密输出。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from .logging_config import get_logger
from .wechat_decrypt import WeChatDatabaseDecryptor, scan_account_databases_from_path

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _file_meta(path: str | Path) -> tuple[int, int] | None:
    """返回 (mtime_ns, size)；文件不存在返回 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))), int(st.st_size))


def snapshot_source(db_path: str | Path, staging_dir: str | Path, retries: int = 5, base_delay: float = 0.1) -> Path:
    """把源数据库（含 ``-wal``/``-shm`` 旁路文件，若存在）字节复制到临时目录。

    复制前后对比主库及旁路文件的 (mtime_ns, size)，不一致说明复制期间微信正在写入，
    按指数退避重试；重试 ``retries`` 次后仍不稳定则抛 ``RuntimeError``。

    只做纯字节读取，不会用 sqlite3 打开源库，因此不会与微信进程产生锁冲突。

    返回临时目录中的库文件路径。
    """
    src = Path(db_path)
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    staged_db = staging / src.name

    attempts = max(int(retries), 1)
    for attempt in range(attempts):
        before = tuple(_file_meta(str(src) + suffix) for suffix in ("", *_SIDECAR_SUFFIXES))
        if before[0] is None:
            raise FileNotFoundError(f"源数据库不存在: {src}")

        shutil.copyfile(src, staged_db)
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = Path(str(src) + suffix)
            staged_sidecar = Path(str(staged_db) + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, staged_sidecar)
            elif staged_sidecar.exists():
                staged_sidecar.unlink()

        after = tuple(_file_meta(str(src) + suffix) for suffix in ("", *_SIDECAR_SUFFIXES))
        if before == after:
            return staged_db

        if attempt < attempts - 1:
            time.sleep(base_delay * (2 ** attempt))

    raise RuntimeError(f"源数据库在复制期间持续变化，快照失败: {src}")


def decrypt_to_output_atomic(
    decryptor: WeChatDatabaseDecryptor,
    staged_db: str | Path,
    output_path: str | Path,
    replace_retries: int = 5,
    base_delay: float = 0.1,
) -> bool:
    """解密临时库到 ``output_path``，先写 ``<output>.tmp-<pid>`` 再 ``os.replace``。

    Windows 上目标文件被读者占用时 ``os.replace`` 会抛 ``PermissionError``，
    做有限次指数退避重试。返回是否解密并替换成功。
    """
    logger = get_logger(__name__)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f"{output_path.name}.tmp-{os.getpid()}")

    try:
        ok = decryptor.decrypt_database(str(staged_db), str(tmp_path))
        if not ok:
            return False

        attempts = max(int(replace_retries), 1)
        for attempt in range(attempts):
            try:
                if any(Path(str(output_path) + suffix).exists() for suffix in _SIDECAR_SUFFIXES):
                    raise ValueError("输出数据库正在使用或遗留日志文件，请关闭读取连接后重试")
                os.replace(tmp_path, output_path)
                return True
            except PermissionError:
                if attempt >= attempts - 1:
                    logger.warning("输出文件被占用，原子替换失败: %s", output_path)
                    raise
                time.sleep(base_delay * (2 ** attempt))
        return False
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def iter_account_databases(db_storage: str | Path) -> list[tuple[str, Path, str]]:
    """列出 db_storage 下的数据库清单。

    返回 ``(账号名, db 文件路径, 相对输出路径)`` 列表；相对输出路径与
    ``decrypt`` 子命令的输出布局一致（扁平的 db 文件名）。
    扫描失败（路径不存在、多账号目录等）返回空列表。
    """
    scan = scan_account_databases_from_path(str(db_storage))
    if scan.get("status") != "success":
        return []

    entries: list[tuple[str, Path, str]] = []
    for account_name, databases in (scan.get("account_databases") or {}).items():
        for db_info in databases:
            db_path = str(db_info.get("path") or "").strip()
            db_name = str(db_info.get("name") or "").strip()
            if not db_path or not db_name:
                continue
            entries.append((str(account_name), Path(db_path), db_name))
    return entries


def watch(
    db_storage: str | Path,
    key: str,
    output_dir: str | Path,
    interval: float = 2.0,
    once: bool = False,
) -> dict:
    """轮询 db_storage，对变化的 db 做 快照 → 解密 → 原子替换。

    变化检测基于主库与 ``-wal`` 的 (mtime_ns, size)。``once=True`` 跑一轮即
    返回，否则按 ``interval`` 秒轮询直到 Ctrl+C 优雅退出。

    返回统计信息 ``{"cycles", "decrypted", "skipped", "failed", "files"}``。
    """
    logger = get_logger(__name__)
    decryptor = WeChatDatabaseDecryptor(key)
    base = Path(output_dir)

    state: dict[str, tuple] = {}
    stats = {"cycles": 0, "decrypted": 0, "skipped": 0, "failed": 0, "files": []}

    def _run_cycle() -> None:
        entries = iter_account_databases(db_storage)
        if not entries:
            logger.warning("[watch] 未找到可解密的数据库: %s", db_storage)
            stats["failed"] += 1
            return
        for account, db_path, rel_output in entries:
            meta = (_file_meta(db_path), _file_meta(str(db_path) + "-wal"))
            if meta[0] is None:
                continue
            output_path = base / "databases" / account / rel_output
            if state.get(str(db_path)) == meta and output_path.exists():
                stats["skipped"] += 1
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="wdd-stage-") as staging:
                    staged_db = snapshot_source(db_path, staging)
                    ok = decrypt_to_output_atomic(decryptor, staged_db, output_path)
            except Exception as exc:
                stats["failed"] += 1
                logger.warning("[watch] 处理失败: %s: %s", db_path, exc)
                continue
            if ok:
                state[str(db_path)] = meta
                stats["decrypted"] += 1
                stats["files"].append(str(output_path))
                logger.info("[watch] 已更新: %s", output_path)
            else:
                stats["failed"] += 1

    logger.info("[watch] start db_storage=%s output=%s interval=%s once=%s", db_storage, base, interval, once)
    try:
        while True:
            _run_cycle()
            stats["cycles"] += 1
            if once:
                break
            time.sleep(max(float(interval), 0.1))
    except KeyboardInterrupt:
        logger.info("[watch] 收到中断信号，优雅退出")

    logger.info(
        "[watch] done cycles=%s decrypted=%s skipped=%s failed=%s",
        stats["cycles"], stats["decrypted"], stats["skipped"], stats["failed"],
    )
    return stats


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """以 ``mode=ro`` 只读连接打开 SQLite 数据库。

    约束：

    - 仅供读取**解密输出库**；读者一律使用该只读连接，配合输出的原子替换，
      读到的始终是某个完整版本，且不会对输出库加写锁。
    - 不可用于源加密库（密文不是 SQLite 格式，查询会失败）。
    """
    resolved = Path(db_path).expanduser().resolve()
    uri_path = quote(str(resolved).replace("\\", "/"), safe="/")
    return sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
