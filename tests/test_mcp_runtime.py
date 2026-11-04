import json
from pathlib import Path
import sqlite3
from contextlib import closing

import pytest

from wechat_extract import mcp_runtime


def sqlite_file(path, value=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sample(value)')
        conn.execute('INSERT INTO sample VALUES (?)', (value,))
        conn.commit()


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    source = tmp_path / 'source' / 'db_storage'
    source.mkdir(parents=True)
    key = tmp_path / 'key.txt'
    key.write_text('00' * 32)
    config = dict(mode='live', db_dir=str(source), account_dir=str(tmp_path / 'cache'),
                  source_account_dir=str(source.parent), output_dir=str(tmp_path / 'exports'),
                  key_file=str(key))
    config_file = tmp_path / 'config.json'
    calls = []

    class Decryptor:
        def __init__(self, secret):
            self.secret = secret

        def decrypt_database(self, src, dest):
            # 新缓存的来源必须是稳定快照，不能直接读原库。
            assert Path(src).is_relative_to(tmp_path / 'cache')
            calls.append((Path(src).name, self.secret))
            Path(dest).write_bytes(Path(src).read_bytes())
            return True

    monkeypatch.setattr(mcp_runtime, 'WeChatDatabaseDecryptor', Decryptor)

    def build(**changes):
        config.update(changes)
        config_file.write_text(json.dumps(config), encoding='utf8')
        return mcp_runtime.Runtime(config_file)

    return source, key, build, calls


def test_refresh_snapshot_and_reuse_then_source_wal_and_key_invalidate(setup_runtime, monkeypatch):
    source, key, build, calls = setup_runtime
    db = source / 'message' / 'message_0.db'
    sqlite_file(db)
    runtime = build()
    assert runtime.refresh()['refreshed'] == 1
    assert runtime.refresh()['skipped'] == 1
    assert len(calls) == 1
    sqlite_file(db, 2)
    assert runtime.refresh()['refreshed'] == 1
    Path(str(db) + '-wal').write_bytes(b'synthetic wal metadata')
    assert runtime.refresh()['refreshed'] == 1
    # 即便文件元数据未变（同步工具保留时间戳），实际密钥改变仍须失效。
    original_meta = mcp_runtime._meta
    key_meta = original_meta(key)
    monkeypatch.setattr(mcp_runtime, '_meta', lambda path: key_meta if path == key else original_meta(path))
    key.write_text('11' * 32)
    assert runtime.refresh()['refreshed'] == 1
    assert calls[-1][1] == '11' * 32
    assert not list(runtime.account_dir.glob('.mcp-stage-*'))
    with closing(sqlite3.connect(runtime.account_dir / 'message' / 'message_0.db')) as conn:
        assert conn.execute('SELECT count(*) FROM sample').fetchone()[0] == 2


def test_failed_refresh_raises_and_preserves_old_cache(setup_runtime, monkeypatch):
    source, _, build, _ = setup_runtime
    db = source / 'message_0.db'
    sqlite_file(db)
    runtime = build()
    runtime.refresh()
    cached = runtime.account_dir / db.name
    old = cached.read_bytes()
    sqlite_file(db, 2)
    monkeypatch.setattr(mcp_runtime, 'decrypt_to_output_atomic', lambda *args: False)
    with pytest.raises(RuntimeError, match='未返回旧缓存'):
        runtime.refresh()
    assert cached.read_bytes() == old


def test_disappeared_source_is_quarantined_not_read_as_stale(setup_runtime):
    source, _, build, _ = setup_runtime
    first, second = source / 'message_0.db', source / 'message_1.db'
    sqlite_file(first)
    sqlite_file(second)
    runtime = build()
    runtime.refresh()
    old = (runtime.account_dir / first.name).read_bytes()
    first.unlink()
    result = runtime.refresh()
    assert result['retired'] == 1 and result['skipped'] == 1
    assert not (runtime.account_dir / first.name).exists()
    retired = list(runtime.account_dir.parent.glob('.mcp-retired-*/message_0.db'))
    assert len(retired) == 1 and retired[0].read_bytes() == old
    assert len(list(runtime.account_dir.rglob('*.db'))) == 1
    second.unlink()
    with pytest.raises(RuntimeError, match='所需源数据库不存在'):
        runtime.refresh()
    assert not list(runtime.account_dir.rglob('*.db'))


def test_cross_account_cache_is_rejected_without_touching_files(setup_runtime):
    source, _, build, _ = setup_runtime
    sqlite_file(source / 'message_0.db')
    runtime = build()
    runtime.refresh()
    cached = runtime.account_dir / 'message_0.db'
    old = cached.read_bytes()
    other = source.parent.parent / 'other' / 'db_storage'
    sqlite_file(other / 'message_1.db')
    switched = build(db_dir=str(other), source_account_dir=str(other.parent))
    with pytest.raises(RuntimeError, match='其他源账号'):
        switched.refresh()
    assert cached.read_bytes() == old


def test_unmanaged_cache_and_path_overlap_rejected(setup_runtime):
    source, _, build, _ = setup_runtime
    sqlite_file(source / 'message_0.db')
    runtime = build()
    sqlite_file(runtime.account_dir / 'user.db')
    with pytest.raises(RuntimeError, match='未管理数据库'):
        runtime.refresh()
    with pytest.raises(ValueError, match='源账号目录'):
        build(account_dir=str(source.parent / 'msg' / 'cache'))


def test_snapshot_mode_does_not_refresh_or_require_key(setup_runtime, monkeypatch):
    source, _, build, _ = setup_runtime
    sqlite_file(source.parent.parent / 'cache' / 'message_0.db')
    runtime = build(mode='snapshot', db_dir=None, key_file=None)
    monkeypatch.setattr(runtime, '_keys', lambda: pytest.fail('snapshot read keys'))
    assert runtime.refresh()['mode'] == 'snapshot'
    assert runtime.refresh()['refreshed'] == 0


def test_state_path_traversal_is_rejected(setup_runtime):
    source, _, build, _ = setup_runtime
    sqlite_file(source / 'message_0.db')
    runtime = build()
    runtime.refresh()
    state = json.loads(runtime._state_path.read_text())
    state['files']['../outside.db'] = None
    runtime._state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='路径越界'):
        runtime.refresh()


def test_partial_failure_allows_retry_without_unmanaged_cache(setup_runtime, monkeypatch):
    source, _, build, calls = setup_runtime
    sqlite_file(source / 'message_0.db')
    sqlite_file(source / 'message_1.db')
    runtime = build()
    actual = mcp_runtime.decrypt_to_output_atomic
    def fail_second(decryptor, src, dest):
        return False if Path(src).name == 'message_1.db' else actual(decryptor, src, dest)
    monkeypatch.setattr(mcp_runtime, 'decrypt_to_output_atomic', fail_second)
    with pytest.raises(RuntimeError):
        runtime.refresh()
    monkeypatch.setattr(mcp_runtime, 'decrypt_to_output_atomic', actual)
    stats = runtime.refresh()
    assert stats['skipped'] == 1 and stats['refreshed'] == 1


@pytest.mark.parametrize('corruption', [b'\0'*500, b'{', None])
def test_reboot_recovers_owned_manifest_and_redecrypts(setup_runtime, corruption):
    source, _, build, calls = setup_runtime
    sqlite_file(source / 'message_0.db')
    runtime = build(); runtime.refresh()
    if corruption is None:
        runtime._state_path.unlink()
    else:
        runtime._state_path.write_bytes(corruption)
    restarted = build()
    assert restarted.refresh()['refreshed'] == 1
    assert len(calls) == 2
    assert restarted.refresh()['skipped'] == 1


def test_corruption_without_ownership_does_not_adopt_cache(setup_runtime):
    source, _, build, _ = setup_runtime
    sqlite_file(source / 'message_0.db')
    runtime = build(); runtime.refresh()
    runtime._state_path.write_bytes(b'\0'*20)
    runtime._owner_path.unlink()
    with pytest.raises(RuntimeError, match='归属'):
        runtime.refresh()


def test_interrupted_staging_is_quarantined(setup_runtime):
    source, _, build, _ = setup_runtime
    sqlite_file(source / 'message_0.db')
    runtime = build(); runtime.refresh()
    sqlite_file(runtime.account_dir / '.mcp-stage-crashed' / 'message_0.db')
    assert build().refresh()['skipped'] == 1
    assert list(runtime.account_dir.parent.glob('.mcp-interrupted-*/.mcp-stage-crashed/message_0.db'))
