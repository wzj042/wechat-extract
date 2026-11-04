"""有限范围发现微信账号目录；显式路径优先，不猜测多个账号。"""

from __future__ import annotations

import os
import re
from pathlib import Path

STORAGE_NAMES = ("xwechat_files", "WeChat Files", "xwechat_files_data")


def normalize_root(path: str | Path) -> Path:
    """保留盘根与 UNC 语义，避免将 D:\\ 误截成盘符相对路径 D:。"""
    text = os.path.expandvars(os.path.expanduser(str(path).strip()))
    if not text:
        raise ValueError("存储路径不能为空")
    if re.fullmatch(r"[A-Za-z]:", text):
        text += "\\"
    elif re.match(r"^[A-Za-z]:[^\\/]", text):
        raise ValueError("不支持盘符相对路径，请使用 D:\\完整路径")
    return Path(os.path.normpath(text)).absolute()


def _is_account(path: Path) -> bool:
    return (path / "db_storage").is_dir() or (path / "msg" / "attach").is_dir()


def find_account_dirs(root: str | Path) -> list[Path]:
    """识别账号、db_storage、账号容器或存储根，不递归扫描整块磁盘。"""
    base = normalize_root(root)
    if not base.is_dir():
        raise FileNotFoundError(f"存储目录不存在: {base}")
    if base.name.lower() == "db_storage" and _is_account(base.parent):
        return [base.parent]
    if _is_account(base):
        return [base]
    accounts = set()
    for container in (base, *(base / name for name in STORAGE_NAMES)):
        if container.is_dir():
            for child in container.iterdir():
                if child.is_dir() and _is_account(child):
                    accounts.add(child)
    return sorted(accounts, key=lambda p: str(p).casefold())


def resolve_account_dir(root: str | Path, account_name: str | None = None) -> Path:
    """明确账号目录；可用目录名筛选，多个匹配必须由调用者指定完整路径。"""
    accounts = find_account_dirs(root)
    if account_name:
        accounts = [p for p in accounts if p.name == account_name]
    if not accounts:
        raise FileNotFoundError(f"未找到账号目录: {root}" + (f"（{account_name}）" if account_name else ""))
    if len(accounts) != 1:
        raise ValueError("发现多个账号目录，请用 --account-dir 指定具体账号: "
                         + ", ".join(str(p) for p in accounts))
    return accounts[0]
