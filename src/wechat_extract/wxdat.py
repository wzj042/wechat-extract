#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxdat —— 微信 4.1.15.13 图片 .dat 解密统一上游模块

归并来源:
  WeFlow  wx_key.dll        取密钥 (kvcomm 扫描 + md5 派生)
  WeFlow  .node addon       V2 解密算法 (反汇编 + 差分定位)
  WeFlow  imageDecrypt.ts   wxgf -> JPEG (NALU 分组 + ffmpeg)
  wechat-decrypt            V2 15 字节头判定 (唯一判对的一方)

依赖 Python3 + cryptography，wxgf 转码另需 ffmpeg。

用法:
    from wechat_extract.wxdat import ImagePipeline
    p = ImagePipeline()
    p.decrypt_chat("example@chatroom", "wxid_example", outdir="out")
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .paths import find_account_dirs, resolve_account_dir

__all__ = ["ImageKeyProvider", "derive_image_keys", "dat_decrypt", "wxgf_to_jpg",
           "ImagePipeline", "find_chat_dir"]

MAGIC_V2 = b"\x07\x08\x56\x32\x08\x07"
MAGIC_V1 = b"\x07\x08\x56\x31\x08\x07"
HEADER_LEN = 15                       # magic6 + aes_size4 + xor_size4 + version1

KVCOMM_REL = r"\AppData\Roaming\Tencent\xwechat\net\kvcomm"
CODE_RE = re.compile(r"^key_(\d+)_.+\.statistic$", re.IGNORECASE)
ACCOUNT_BASES = [r"\Documents\xwechat_files", r"\xwechat_files",
                 r"\Documents\WeChat Files", r"\WeChat Files"]


# ─────────────────────────── 密钥 ───────────────────────────

def clean_wxid(wxid: str) -> str:
    """与 WeFlow keyService.cleanWxid 一致。"""
    v = (wxid or "").strip()
    if not v:
        return v
    if v.lower().startswith("wxid_"):
        second = v.find("_", 5)
        return v if second == -1 else v[:second]
    m = re.match(r"^(.+)_([a-zA-Z0-9]{4})$", v)
    return m.group(1) if m else v


def _scan_kvcomm(userprofile: str | None = None):
    """wx_key.dll 的行为: 从 kvcomm 目录的 key_<code>_*.statistic 文件名取 code。"""
    up = userprofile or os.environ.get("USERPROFILE") or str(Path.home())
    root = up + KVCOMM_REL
    codes = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            m = CODE_RE.match(name)
            if m:
                c = int(m.group(1), 10)
                if 0 < c <= 0xFFFFFFFF:
                    codes.append(c)
    return root, sorted(set(codes))


def list_accounts(userprofile: str | None = None):
    """枚举 xwechat_files 下的账号目录名。"""
    up = userprofile or os.environ.get("USERPROFILE") or str(Path.home())
    seen = set()
    for b in ACCOUNT_BASES:
        root = up + b
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if re.match(r"^wxid_[0-9a-z]{6,22}(_\w{4})?$", name):
                seen.add(name)
    return sorted(seen)


def derive_image_keys(code: int, wxid: str):
    """xorKey = code & 0xFF ; aesKey = md5(str(code)+clean_wxid).hex()[:16]  (16 位 ASCII)"""
    return code & 0xFF, hashlib.md5((str(code) + clean_wxid(wxid)).encode()).hexdigest()[:16]


class ImageKeyProvider:
    """取本机图片解密密钥。等价于 wx_key.dll + keyService.deriveImageKeys。"""

    def __init__(self, userprofile: str | None = None):
        self.userprofile = userprofile or os.environ.get("USERPROFILE") or str(Path.home())
        self.kvcomm, self.codes = _scan_kvcomm(self.userprofile)
        self.accounts = list_accounts(self.userprofile)

    def keys_for(self, wxid: str):
        """返回 [(xor_key:int, aes_key:ascii_bytes), ...]"""
        out = []
        for code in self.codes:
            xk, ak = derive_image_keys(code, wxid)
            out.append((xk, ak.encode("ascii")))
        return out

    def summary(self) -> str:
        return ("kvcomm=%s\ncodes=%s\naccounts=%s"
                % (self.kvcomm, self.codes, self.accounts))


# ─────────────────────────── V2 解密 ───────────────────────────

def dat_decrypt(data: bytes, xor_key: int, aes_key, strip_pkcs7: bool = True,
                *, aes_size_mode: str = "auto"):
    """解 V2 .dat。返回 (明文, meta)。

    aes_key: 16 位 ASCII 串 / bytes(16) / 32 位 hex 串
    布局: [15B 头][AES-ECB 密文][未加密中段][xor_size 单字节 XOR]。
    已验证变体的头部 aes_size 为密文长度；plaintext 模式则为填充前明文长度。
    auto 按 AES 对齐和边界处的加密填充块识别，仍可显式指定模式。
    """
    if len(data) < HEADER_LEN:
        raise ValueError("文件太小，无法解析")
    if data[:6] not in (MAGIC_V2, MAGIC_V1):
        raise ValueError("不是 V1/V2 格式")
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    version = data[14]
    key = _coerce_aes_key(aes_key)
    if aes_size_mode == "auto":
        # AES 密文必为整块。明文长度恰好对齐时，PKCS7 会另加一个
        # 完整的填充块；只在 AES/中段范围内比对，不能借用 XOR 尾部。
        if aes_size % 16:
            aes_size_mode = "plaintext"
        else:
            boundary = HEADER_LEN + aes_size
            aes_size_mode = "ciphertext"
            if boundary + 16 <= len(data) - xor_size:
                encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
                marker = encryptor.update(bytes([16]) * 16) + encryptor.finalize()
                if data[boundary:boundary + 16] == marker:
                    aes_size_mode = "plaintext"
    if aes_size_mode not in ("ciphertext", "plaintext"):
        raise ValueError("aes_size_mode 必须为 auto、ciphertext 或 plaintext")
    encrypted_size = aes_size if aes_size_mode == "ciphertext" else (aes_size // 16 + 1) * 16

    off = HEADER_LEN
    ct = data[off:off + encrypted_size]
    if len(ct) != encrypted_size:
        raise ValueError("AES 数据长度超过文件实际长度")

    if xor_size > len(data) - HEADER_LEN - encrypted_size:
        raise ValueError("XOR 数据长度超过文件实际长度或与 AES 数据重叠")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    dec = decryptor.update(ct) + decryptor.finalize()
    if aes_size_mode == "plaintext":
        unpadded = _pkcs7_unpad(dec)
        if len(unpadded) != aes_size:
            raise ValueError("AES 明文长度与头部不符")
        if strip_pkcs7:
            dec = unpadded
    elif strip_pkcs7:
        try:
            dec = _pkcs7_unpad(dec)
        except ValueError:
            pass                       # 该版本未加 padding

    tail = data[len(data) - xor_size:] if xor_size else b""
    middle = data[HEADER_LEN + encrypted_size:len(data) - xor_size]
    out = dec + middle + bytes(b ^ xor_key for b in tail)
    meta = dict(version=version, aesSize=aes_size, xorSize=xor_size,
                aesSizeMode=aes_size_mode, encryptedSize=encrypted_size, rawSize=len(middle),
                isWxgf=out[:4] == b"wxgf", ext=_sniff(out))
    return out, meta


def _coerce_aes_key(aes_key) -> bytes:
    if isinstance(aes_key, str):
        s = aes_key.strip()
        if len(s) == 16:
            return s.encode("ascii")
        if len(s) == 32:
            return bytes.fromhex(s)
        raise ValueError("aes_key 需为 16 位 ASCII 或 32 位 hex")
    if isinstance(aes_key, (bytes, bytearray)) and len(aes_key) == 16:
        return bytes(aes_key)
    raise ValueError("aes_key 长度需为 16")


def _pkcs7_unpad(buf: bytes) -> bytes:
    if not buf:
        raise ValueError("空")
    pad = buf[-1]
    if not 1 <= pad <= 16 or len(buf) < pad or buf[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 填充内容非法")
    return buf[:-pad]


def _sniff(buf: bytes) -> str:
    if buf[:3] == b"\xff\xd8\xff":
        return "jpg"
    if buf[:4] == b"\x89PNG":
        return "png"
    if buf[:4] == b"GIF8":
        return "gif"
    if buf[:4] == b"RIFF":
        return "webp"
    return ""


# ─────────────────────────── wxgf -> JPEG ───────────────────────────

def _nalu_units(buf: bytes):
    starts, i, n = [], 4, len(buf)
    while i < n - 3:
        p4 = buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 0 and buf[i + 3] == 1
        p3 = buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 1
        if p4 or p3:
            starts.append(i)
            i += 4 if p4 else 3
            continue
        i += 1
    units = []
    for k, s in enumerate(starts):
        hdr = 4 if buf[s:s + 4] == b"\x00\x00\x00\x01" else 3
        end = starts[k + 1] if k + 1 < len(starts) else n
        payload = buf[s + hdr:end]
        if len(payload) >= 2:
            units.append(payload)
    return units


def _nalu_type(u: bytes) -> int:
    return (u[0] >> 1) & 0x3F


def _wxgf_candidates(buf: bytes):
    units = _nalu_units(buf)
    cands, seen = [], set()

    def add(d):
        if not d or len(d) < 100:
            return
        k = bytes(d[:64]) + str(len(d)).encode()
        if k not in seen:
            seen.add(k)
            cands.append(d)

    vps = [i for i, u in enumerate(units) if _nalu_type(u) == 32]
    groups = []
    for i, s in enumerate(vps):
        e = vps[i + 1] if i + 1 < len(vps) else len(units)
        gu = units[s:e]
        if gu and any(_nalu_type(u) in (1, 19, 20) for u in gu):
            merged = b"".join(b"\x00\x00\x00\x01" + u for u in gu)
            groups.append(merged)
    for g in sorted(groups, key=len, reverse=True):
        add(g)
    add(b"".join(b"\x00\x00\x00\x01" + u for u in units))
    add(buf[4:])
    return cands


def wxgf_to_jpg(buf: bytes, out_path, ffmpeg: str = "ffmpeg") -> bool:
    """wxgf -> JPEG。算法复刻自 WeFlow imageDecryptService.unwrapWxgf。"""
    if buf[:4] != b"wxgf":
        Path(out_path).write_bytes(buf)
        return True
    # 有些 wxgf 直接内嵌了原图
    for i in range(4, min(len(buf) - 12, 4096)):
        if buf[i] == 0xFF and buf[i + 1] == 0xD8 and buf[i + 2] == 0xFF:
            Path(out_path).write_bytes(buf[i:])
            return True
        if buf[i:i + 4] == b"\x89PNG":
            Path(out_path).write_bytes(buf[i:])
            return True
    for cand in _wxgf_candidates(buf):
        r = subprocess.run([ffmpeg, "-v", "error", "-f", "hevc", "-i", "pipe:0",
                            "-frames:v", "1", "-q:v", "2", "-y", str(out_path)],
                           input=cand, capture_output=True)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            return True
    return False


# ─────────────────────────── 定位与批处理 ───────────────────────────

def find_chat_dir(attach_root: Path, chat_username: str) -> Path | None:
    """图片目录: attach/<md5(username)>/<月>/Img/ (来自 MCP decode_image.py)"""
    h = hashlib.md5(chat_username.encode()).hexdigest()
    base = attach_root / h
    return base if base.is_dir() else None


class ImagePipeline:
    """统一的取密钥 -> 解密 -> 转 JPEG 流程。"""

    def __init__(self, userprofile: str | None = None, account_dir: str | Path | None = None,
                 aes_size_mode: str = "auto"):
        self.kp = ImageKeyProvider(userprofile)
        self.userprofile = self.kp.userprofile
        self.account_dir = account_dir
        self.aes_size_mode = aes_size_mode

    def resolve_account(self, wxid_dir: str | None = None) -> Path:
        if self.account_dir is not None:
            return resolve_account_dir(self.account_dir, wxid_dir)
        accounts = set()
        for root in (Path(self.userprofile), Path(self.userprofile) / "Documents"):
            if root.is_dir():
                accounts.update(p for p in find_account_dirs(root)
                                if wxid_dir is None or p.name == wxid_dir)
        if not accounts:
            raise FileNotFoundError(f"未找到账号 {wxid_dir or ''}，请用 --account-dir 指定实际目录")
        if len(accounts) != 1:
            raise ValueError("发现多个账号目录，请用 --account-dir 指定具体账号")
        return accounts.pop()

    def attach_root(self, wxid_dir: str | None = None) -> Path:
        account = self.resolve_account(wxid_dir)
        root = account / "msg" / "attach"
        if not root.is_dir():
            raise FileNotFoundError(f"找不到 msg/attach: {account}")
        return root

    def decrypt_chat(self, chat_username: str, wxid_dir: str | None,
                     outdir, want=("jpg", "png", "gif", "webp"), also_wxgf=True):
        """解密一个会话(群)的全部 .dat 并转成 JPEG, 返回 (成功数, 失败数, 输出目录)。"""
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        account = self.resolve_account(wxid_dir)
        root = account / "msg" / "attach"
        if not root.is_dir():
            raise FileNotFoundError(f"找不到 msg/attach: {account}")
        base = find_chat_dir(root, chat_username)
        if base is None:
            raise FileNotFoundError("找不到会话目录: %s" % chat_username)
        source_id = hashlib.sha256(os.path.normcase(str(account)).encode()).hexdigest()[:8]
        export_root = outdir / f"{account.name}-{source_id}" / base.name

        keys = self.kp.keys_for(account.name)
        if not keys:
            raise RuntimeError("未取到密钥, 请先在微信里打开过图片。%s" % self.kp.summary())

        old_files = [p for p in glob.glob(os.path.join(glob.escape(str(base)), "*", "Img", "*.dat"))
                     if not p.endswith("_t.dat") and not p.endswith("_h.dat")]
        if not old_files:
            return 0, 0, outdir
        old_files = [Path(p) for p in old_files]
        old_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        ok = fail = 0
        for f in old_files:
            out, meta = self._decrypt_one(f.read_bytes(), keys, aes_size_mode=self.aes_size_mode)
            if out is None:
                fail += 1
                continue
            ext = ".bin" if meta["isWxgf"] else ("." + meta["ext"] if meta["ext"] else ".bin")
            dest = (export_root / f.relative_to(base)).with_suffix(ext)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if meta["isWxgf"]:
                if not also_wxgf:
                    ok += 1
                    continue
                jpg = dest.with_suffix(".jpg")
                if wxgf_to_jpg(out, jpg):
                    dest.unlink(missing_ok=True)
                    ok += 1
                else:
                    fail += 1
            else:
                if meta["ext"] in want or meta["ext"] == "":
                    dest.write_bytes(out)
                    ok += 1
                else:
                    fail += 1
        return ok, fail, outdir

    @staticmethod
    def _decrypt_one(data: bytes, keys, *, aes_size_mode: str = "auto"):
        for xk, ak in keys:
            try:
                out, meta = dat_decrypt(data, xk, ak, aes_size_mode=aes_size_mode)
            except Exception:
                continue
            if out[:4] == b"wxgf" or out[:3] == b"\xff\xd8\xff" or out[:4] == b"\x89PNG":
                return out, meta
        return None, None


# ─────────────────────────── CLI ───────────────────────────

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="微信 V2 图片 .dat 解密（纯 Python 上游实现）")
    ap.add_argument("--wxid-dir", help="账号目录名, 如 wxid_example_abcd；多个账号时配合路径筛选")
    ap.add_argument("--account-dir", help="账号目录、db_storage、xwechat_files 或自定义存储根")
    ap.add_argument("--chat", help="会话 username, 如 example@chatroom")
    ap.add_argument("--out", default="wxdat_out")
    ap.add_argument("--userprofile", help="用户主目录（默认当前用户 USERPROFILE）")
    ap.add_argument("--aes-size-mode", choices=("auto", "ciphertext", "plaintext"), default="auto",
                    help="头部 AES 长度语义（默认 auto 按文件识别，可显式覆盖）")
    ap.add_argument("--keys", action="store_true", help="只打印密钥信息")
    a = ap.parse_args(argv)

    if not a.keys and not a.chat:
        ap.error("解密图片需要 --chat")
    if not a.account_dir and not a.wxid_dir:
        ap.error("需要 --account-dir 或 --wxid-dir")
    p = ImagePipeline(userprofile=a.userprofile, account_dir=a.account_dir, aes_size_mode=a.aes_size_mode)
    if a.keys:
        account_name = p.resolve_account(a.wxid_dir).name if a.account_dir else a.wxid_dir
        print(p.kp.summary())
        for xk, ak in p.kp.keys_for(account_name):
            print("  xorKey=0x%02x aesKey=%s" % (xk, ak.decode()))
        return 0
    ok, fail, outdir = p.decrypt_chat(a.chat, a.wxid_dir, a.out)
    print("成功 %d, 失败 %d -> %s" % (ok, fail, outdir.resolve()))
    return 0 if ok > 0 and fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
