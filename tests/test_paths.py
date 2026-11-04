from pathlib import Path

import pytest

from wechat_extract.paths import find_account_dirs, normalize_root, resolve_account_dir


def test_drive_roots_and_unc_keep_absolute_semantics():
    assert str(normalize_root("D:\\")) == "D:\\"
    assert str(normalize_root("D:")) == "D:\\"
    assert str(normalize_root("\\\\server\\share\\")) == "\\\\server\\share\\"
    assert str(normalize_root("\\\\server\\share\\xwechat_files\\")) == "\\\\server\\share\\xwechat_files"
    with pytest.raises(ValueError, match="盘符相对"):
        normalize_root("D:relative")
    with pytest.raises(ValueError, match="不能为空"):
        normalize_root("")


@pytest.mark.parametrize("container", ["xwechat_files", "WeChat Files", "xwechat_files_data"])
def test_resolve_all_explicit_path_levels(tmp_path, container):
    account = tmp_path / container / "custom_wechat_name"
    storage = account / "db_storage"
    storage.mkdir(parents=True)
    for root in (tmp_path, account.parent, account, storage):
        assert resolve_account_dir(root) == account
        assert resolve_account_dir(root, "custom_wechat_name") == account
    assert find_account_dirs(tmp_path) == [account]


def test_multiple_accounts_are_not_guessed(tmp_path):
    accounts = [tmp_path / "xwechat_files" / name for name in ("alice", "bob")]
    for account in accounts:
        (account / "db_storage").mkdir(parents=True)
    with pytest.raises(ValueError, match="多个账号"):
        resolve_account_dir(tmp_path)
    assert resolve_account_dir(tmp_path, "bob") == accounts[1]
    with pytest.raises(FileNotFoundError, match="未找到账号"):
        resolve_account_dir(tmp_path, "missing")


def test_media_only_account_is_supported_but_legacy_filestorage_is_not(tmp_path):
    account = tmp_path / "images_only"
    (account / "msg" / "attach").mkdir(parents=True)
    old = tmp_path / "old"
    (old / "FileStorage").mkdir(parents=True)
    assert find_account_dirs(tmp_path) == [account]
    with pytest.raises(FileNotFoundError, match="未找到账号"):
        resolve_account_dir(old)


def test_invalid_explicit_path_does_not_fallback(tmp_path):
    with pytest.raises(FileNotFoundError, match="不存在"):
        resolve_account_dir(tmp_path / "missing")
