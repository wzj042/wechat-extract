from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from wechat_extract import live


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_snapshot_rejects_sidecar_changes(tmp_path, monkeypatch, suffix):
    source = tmp_path / "source.db"
    source.write_bytes(b"database")
    wal = Path(str(source) + suffix)
    wal.write_bytes(b"before")
    copyfile = live.shutil.copyfile

    def mutate_after_copy(src, dst):
        result = copyfile(src, dst)
        if Path(src) == wal:
            wal.write_bytes(wal.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(live.shutil, "copyfile", mutate_after_copy)
    with pytest.raises(RuntimeError, match="快照失败"):
        live.snapshot_source(source, tmp_path / "stage", retries=2, base_delay=0)


def test_empty_watch_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "iter_account_databases", lambda _: [])
    stats = live.watch(str(tmp_path), "ab" * 32, tmp_path / "out", once=True)
    assert stats["failed"] == 1
    assert stats["decrypted"] == 0


def test_atomic_publish_refuses_old_output_wal(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE messages(content TEXT)")
        connection.commit()
    output = tmp_path / "old.db"
    output.write_bytes(b"old output must survive")
    wal = Path(str(output) + "-wal")
    wal.write_bytes(b"reader sidecar must survive")
    decryptor = live.WeChatDatabaseDecryptor("ab" * 32)
    with pytest.raises(ValueError, match="遗留日志"):
        live.decrypt_to_output_atomic(decryptor, source, output)
    assert output.read_bytes() == b"old output must survive"
    assert wal.read_bytes() == b"reader sidecar must survive"


def test_watch_once_materializes_latest_wal(tmp_path, monkeypatch):
    source = tmp_path / "message_0.db"
    monkeypatch.setattr(
        live, "iter_account_databases",
        lambda _: [("synthetic_account", source, source.name)],
    )
    monkeypatch.setenv("WECHAT_TOOL_OUTPUT_DIR", str(tmp_path / "logs"))
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE messages(content TEXT)")
        connection.commit()
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO messages VALUES('WAL only message')")
        connection.commit()
        before = source.read_bytes()
        stats = live.watch(str(tmp_path), "ab" * 32, tmp_path / "out", once=True)
        assert stats["decrypted"] == 1
        assert stats["failed"] == 0
        assert source.read_bytes() == before
        with closing(live.open_readonly(stats["files"][0])) as result:
            assert result.execute("SELECT content FROM messages").fetchone() == ("WAL only message",)
