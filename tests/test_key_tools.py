"""只使用合成数据；不枚举或读取真实微信进程。"""

import contextlib
import hashlib
import hmac
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "extract_windows_key",
    Path(__file__).resolve().parents[1] / "tools" / "extract_windows_key.py",
)
key_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(key_tool)


def fake_page(passphrase):
    salt = bytes(range(16))
    key = key_tool.derive_enc_key(passphrase, salt)
    page = bytearray(salt + bytes([123]) * (4096 - 16))
    mac_key = hashlib.pbkdf2_hmac(
        "sha512", key, bytes(b ^ 0x3A for b in salt), 2, dklen=32,
    )
    page[-64:] = hmac.new(
        mac_key, page[16:-64] + struct.pack("<I", 1), hashlib.sha512,
    ).digest()
    return bytes(page), key


class WindowsKeyToolsTests(unittest.TestCase):
    def test_page_hmac_rejects_corruption_and_truncation(self):
        page, key = fake_page(bytes(range(32)))
        self.assertTrue(key_tool.verify_enc_key(key, page))
        self.assertFalse(key_tool.verify_enc_key(key, page[:-1]))
        self.assertFalse(key_tool.verify_enc_key(key, page[:100] + b"\0" + page[101:]))

    def test_mask_and_stub_parsing(self):
        mask = bytes(range(32))
        instructions = b"".join(
            b"\x48\xba" + mask[i:i + 8] + b"\x90" * 3
            for i in range(0, 32, 8)
        ) + b"\x48\x85\xc0"
        self.assertEqual(
            key_tool.extract_mask_candidates_from_bytes(instructions * 2), [mask],
        )
        pointer = 0x1234567890
        stub = pointer.to_bytes(8, "little")[:6] + key_tool.STUB_TAIL
        self.assertEqual(list(key_tool.iter_stub_pointers(b"xx" + stub)), [pointer])
        self.assertEqual(list(key_tool.iter_stub_pointers(stub[:-1])), [])

    def test_chunk_boundary_stub_is_read(self):
        pointer = 0x1234567890
        candidate = bytes(range(32))
        stub = pointer.to_bytes(8, "little")[:6] + key_tool.STUB_TAIL
        memory = b"\0" * 63 + stub + b"\0" * 50

        def read(_handle, address, size):
            if address == pointer:
                return candidate
            return memory[address:address + size]

        with patch.object(key_tool, "CHUNK_SZ", 64), patch.object(key_tool, "read_mem", read):
            self.assertEqual(
                key_tool.scan_key_candidates(None, [(0, len(memory))], lambda _: None),
                [candidate],
            )

    def test_main_writes_cli_compatible_text_without_logging_key(self):
        secret = bytes(range(32))
        page, _ = fake_page(secret)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "message.db"
            database.write_bytes(page)
            (root / "plain.db").write_bytes(b"SQLite format 3\0" + bytes(4080))
            output = root / "data" / "db-key.txt"
            self.assertEqual(len(key_tool.collect_db_files(root)), 1)
            with (
                patch.object(key_tool, "init_windows_api"),
                patch.object(key_tool, "get_pids", return_value=[(1, 0)]),
                patch.object(key_tool, "find_weixin_dll", return_value="synthetic.dll"),
                patch.object(key_tool, "extract_internal_db_keys", return_value=[bytes(32)]),
                patch.object(key_tool, "recover_passphrase", return_value=secret),
                contextlib.redirect_stdout(io.StringIO()) as captured,
            ):
                self.assertEqual(key_tool.main(["--db-dir", str(root), "--out", str(output)]), 0)
            self.assertEqual(output.read_text(), secret.hex() + "\n")
            self.assertNotIn(secret.hex(), captured.getvalue())
            with self.assertRaises(FileExistsError):
                key_tool.write_key(output, bytes(32))
            self.assertEqual(output.read_text().strip(), secret.hex())

    def test_output_path_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            key_tool.main(["--db-dir", "unused"])


if __name__ == "__main__":
    unittest.main()
