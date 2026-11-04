"""MCP 私有配置与按需刷新。复用快照/解密器，不读取进程或修改源库。"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .live import snapshot_source, decrypt_to_output_atomic
from .wechat_decrypt import WeChatDatabaseDecryptor


def _meta(path):
    try:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    except FileNotFoundError:
        return None


@contextmanager
def _cache_lock(path):
    """多个 stdio 客户端共享同一个缓存时也不能并发写。"""
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        deadline = time.monotonic() + 60
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('缓存正在被其他进程刷新，请稍后重试') from None
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Runtime:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
        if not isinstance(self.config, dict):
            raise ValueError('MCP 配置必须是 JSON 对象')
        self.lock = threading.RLock()
        self.mode = self.config.get('mode', 'live')
        if self.mode not in ('live', 'snapshot'):
            raise ValueError('mode 必须为 live 或 snapshot')
        self.source_account_dir = self._path('source_account_dir', required=False)
        self.db_dir = self._path('db_dir', required=self.mode == 'live')
        if self.source_account_dir is None and self.db_dir is not None:
            self.source_account_dir = self.db_dir.parent if self.db_dir.name == 'db_storage' else self.db_dir
        if self.source_account_dir is None:
            raise ValueError('必须配置 source_account_dir')
        self.account_dir = self._path('account_dir')
        self.output_dir = self._path('output_dir')
        if self.db_dir is not None and (self.account_dir.is_relative_to(self.db_dir) or self.db_dir.is_relative_to(self.account_dir)):
            raise ValueError('MCP 明文缓存必须与源数据库目录分离')
        if self.mode == 'live' and self.account_dir.is_relative_to(self.source_account_dir):
            raise ValueError('live 缓存不得位于源账号目录内')
        if self.output_dir.is_relative_to(self.source_account_dir):
            raise ValueError('导出目录不得位于源账号目录内')
        if self.mode == 'snapshot' and not self.account_dir.is_dir():
            raise ValueError('快照目录不存在')
        self.account_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.account_dir / '.mcp-refresh.json'
        self._owner_path = self.account_dir / '.mcp-owner.json'
        self._key_file = self._path('key_file', required=False)
        self._keys_file = self._path('keys_file', required=False)
        self.last_refresh = None

    def _path(self, name, required=True):
        value = self.config.get(name)
        if not value:
            if required:
                raise ValueError(f'缺少配置项 {name}')
            return None
        path = Path(value).expanduser()
        return (self.config_path.parent / path).resolve() if not path.is_absolute() else path.resolve()

    @property
    def queries(self):
        from .mcp_queries import QueryService
        return QueryService(self.account_dir)

    def status(self):
        return {'status': 'ok', 'mode': self.mode, 'backend': 'wechat-extract',
                'source_exists': self.source_account_dir.is_dir(),
                'cached_databases': len(list(self.account_dir.rglob('*.db'))),
                'key_configured': self._key_file is not None or self._keys_file is not None,
                'last_refresh': self.last_refresh}

    def _keys(self):
        if self._key_file:
            value = self._key_file.read_text(encoding='utf-8-sig').strip()
            if value.startswith('{'):
                value = json.loads(value)['passphrase_hex']
            if not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{64}', value):
                raise ValueError('数据库密钥文件格式无效')
            return value, {}
        if self._keys_file:
            values = json.loads(self._keys_file.read_text(encoding='utf-8-sig'))
            return None, {k.replace('\\', '/'): v for k, v in values.items() if not k.startswith('_')}
        raise ValueError('live 模式需要 key_file 或 keys_file')

    def _entries(self, scope):
        scopes = {'contacts': {'contact'}, 'sessions': {'contact', 'session'},
                  'messages': {'contact', 'message'},
                  'media': {'contact', 'message', 'media', 'resource', 'hardlink'},
                  'all': {'contact', 'session', 'message', 'media', 'resource', 'hardlink'}}
        if scope not in scopes:
            raise ValueError('未知刷新范围')
        entries = []
        for path in sorted(self.db_dir.rglob('*.db')):
            name = path.name.lower()
            kind = ('contact' if name == 'contact.db' else 'session' if name == 'session.db' else
                    'resource' if name == 'message_resource.db' else 'hardlink' if name == 'hardlink.db' else
                    'media' if re.fullmatch(r'media_\d+\.db', name) else
                    'message' if re.fullmatch(r'(?:biz_)?message(?:_\d+)?\.db', name) else None)
            if kind not in scopes[scope]:
                continue
            if not path.resolve().is_relative_to(self.db_dir):
                raise ValueError('源数据库路径越出指定目录')
            entries.append(path)
        return entries

    def _load_state(self, source_id):
        try:
            saved = json.loads(self._state_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            if self._owner_path.exists():
                return self._recover_state(source_id)
            saved = None
        except ValueError:
            return self._recover_state(source_id)
        if saved is None:
            if any(self.account_dir.rglob('*.db')):
                raise RuntimeError('live 目录含未管理数据库；请使用新的专用缓存目录')
            return {}
        if not isinstance(saved, dict) or saved.get('source') != source_id:
            raise RuntimeError('缓存属于其他源账号；请使用独立缓存目录')
        state = saved.get('files')
        if not isinstance(state, dict):
            raise RuntimeError('缓存状态格式无效')
        for rel in state:
            path = Path(rel)
            if path.suffix.lower() != '.db' or path.is_absolute() or '..' in path.parts or not (self.account_dir / path).resolve().is_relative_to(self.account_dir):
                raise ValueError('缓存状态路径越界')
            if (self.account_dir / path).exists() and not (self.account_dir / path).is_file():
                raise ValueError('缓存状态必须指向数据库文件')
        unmanaged = {p.relative_to(self.account_dir).as_posix() for p in self.account_dir.rglob('*.db')} - set(state)
        if unmanaged:
            raise RuntimeError('live 目录含未管理数据库；拒绝混合读取')
        return state

    def _recover_state(self, source_id):
        # A separate durable ownership stamp is required: never adopt an unknown
        # cache or trust stale plaintext after losing the refresh manifest.
        try:
            owner = json.loads(self._owner_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise RuntimeError('缓存状态损坏且无有效归属记录；需人工核验') from exc
        if not isinstance(owner, dict) or owner.get('source') != source_id:
            raise RuntimeError('缓存属于其他源账号；请使用独立缓存目录')
        state = {}
        for path in self.account_dir.rglob('*.db'):
            if not path.resolve().is_relative_to(self.account_dir) or not path.is_file():
                raise ValueError('缓存状态路径越界')
            state[path.relative_to(self.account_dir).as_posix()] = None
        if self._state_path.exists():
            quarantine = Path(tempfile.mkdtemp(prefix='.mcp-state-corrupt-', dir=self.account_dir.parent))
            os.replace(self._state_path, quarantine / self._state_path.name)
        self._save_state(source_id, state)
        return state

    def _quarantine_missing(self, state):
        missing = [rel for rel in state if not (self.db_dir / rel).is_file()]
        if not missing:
            return 0
        # 放在缓存目录外，避免 MessageReader/资源扫描递归读回；保留文件供恢复。
        quarantine = Path(tempfile.mkdtemp(prefix='.mcp-retired-', dir=self.account_dir.parent))
        for rel in missing:
            for suffix in ('', '-wal', '-shm', '-journal'):
                path = Path(str(self.account_dir / rel) + suffix)
                if not path.resolve().is_relative_to(self.account_dir):
                    raise ValueError('缓存路径越出指定目录')
                if path.exists():
                    target = quarantine / (rel + suffix)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)
            state.pop(rel)
        return len(missing)

    def _save_state(self, source_id, state):
        self._atomic_json(self._state_path, {'source': source_id, 'files': state})

    @staticmethod
    def _atomic_json(path, value):
        staged = path.with_suffix('.tmp')
        with staged.open('w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)

    def refresh(self, scope='messages'):
        if self.mode == 'snapshot':
            return {'status': 'ok', 'mode': 'snapshot', 'refreshed': 0, 'skipped': 0}
        with self.lock, _cache_lock(self.account_dir / '.mcp-refresh.lock'):
            key, keys = self._keys()
            key_stamp = [_meta(self._key_file or self._keys_file),
                         hashlib.sha256(json.dumps([key, keys], sort_keys=True).encode()).hexdigest()]
            source_id = hashlib.sha256(str(self.db_dir).encode()).hexdigest()
            # The cross-process lock guarantees these are abandoned staging dirs.
            for abandoned in self.account_dir.glob('.mcp-stage-*'):
                if abandoned.is_dir() and not abandoned.is_symlink():
                    retired_stage = Path(tempfile.mkdtemp(prefix='.mcp-interrupted-', dir=self.account_dir.parent))
                    os.replace(abandoned, retired_stage / abandoned.name)
            state = self._load_state(source_id)
            self._atomic_json(self._owner_path, {'source': source_id})
            entries = self._entries(scope)
            retired = self._quarantine_missing(state)
            if retired:
                self._save_state(source_id, state)
            if not entries:
                raise RuntimeError('所需源数据库不存在')
            stats = {'status': 'ok', 'mode': 'live', 'refreshed': 0, 'skipped': 0, 'retired': retired}
            for source in entries:
                rel = source.relative_to(self.db_dir).as_posix()
                output = self.account_dir / rel
                if not output.resolve().is_relative_to(self.account_dir):
                    raise ValueError('缓存路径越出指定目录')
                before = [_meta(Path(str(source) + suffix)) for suffix in ('', '-wal', '-shm', '-journal')]
                signature = [before, key_stamp, _meta(output)]
                if state.get(rel) == signature and signature[-1] is not None:
                    stats['skipped'] += 1
                    continue
                raw = key if key is not None else keys.get(rel, {}).get('enc_key')
                if not isinstance(raw, str) or not re.fullmatch('[0-9a-fA-F]{64}', raw):
                    raise RuntimeError('所需数据库缺少密钥；请配置全局 passphrase 文件')
                # Register ownership before publishing a new cache file. A crash
                # between publishing the DB and its signature must be recoverable.
                state[rel] = None
                self._save_state(source_id, state)
                with tempfile.TemporaryDirectory(prefix='.mcp-stage-', dir=self.account_dir) as staging:
                    staged = snapshot_source(source, staging)
                    if not decrypt_to_output_atomic(WeChatDatabaseDecryptor(raw), staged, output):
                        raise RuntimeError('数据库刷新失败；未返回旧缓存内容')
                after = [_meta(Path(str(source) + suffix)) for suffix in ('', '-wal', '-shm', '-journal')]
                if before != after:
                    # 已发布的是一致快照，但下次调用必须重新刷新。
                    state[rel] = None
                else:
                    state[rel] = [after, key_stamp, _meta(output)]
                stats['refreshed'] += 1
                # 某个后续库失败时，已发布文件仍有归属记录；下次可以安全重试。
                self._save_state(source_id, state)
            self._save_state(source_id, state)
            self.last_refresh = stats
            return stats
