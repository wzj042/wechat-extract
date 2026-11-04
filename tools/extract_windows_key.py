"""Windows 微信 4.1+ 密钥提取；只在显式运行 main 时访问进程。

来源：wechat-decrypt 项目中 find_all_keys_v4.py 与 get_passphrase.py 的
未发布修改版（2026-10-17 补丁，参考 WCDA），详见 docs/sources.md。
保留 DLL 掩码、stub 与 HMAC 验真算法；不依赖 MCP 或私有配置。
输出纯 64 位十六进制文本，可直接传给 wechat-extract --key-file。
"""

import argparse
import csv
import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import hmac
import os
from pathlib import Path
import re
import struct
import subprocess

MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
MEM_PRIVATE = 0x20000
PAGE_SZ = 4096
CHUNK_SZ = 4 * 1024 * 1024
kernel32 = None
psapi = None


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
    ]


def init_windows_api():
    """显式初始化，导入/--help/合成测试均不触碰进程。"""
    global kernel32, psapi
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("需要 Windows 与 64 位 Python")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    signatures = [
        (kernel32.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        (kernel32.CloseHandle, [wt.HANDLE], wt.BOOL),
        (kernel32.ReadProcessMemory,
         [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
          ctypes.POINTER(ctypes.c_size_t)], wt.BOOL),
        (kernel32.VirtualQueryEx,
         [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t],
         ctypes.c_size_t),
        (kernel32.QueryFullProcessImageNameW,
         [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        (psapi.EnumProcessModulesEx,
         [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD],
         wt.BOOL),
        (psapi.GetModuleFileNameExW,
         [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD], wt.DWORD),
    ]
    for function, argtypes, restype in signatures:
        function.argtypes = argtypes
        function.restype = restype


def get_pids():
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, check=True,
    )
    pids = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2 and row[0].lower() == "weixin.exe":
            pids.append((int(row[1]), 0))
    if not pids:
        raise RuntimeError("Weixin.exe 未运行")
    return pids


def read_mem(handle, address, size):
    buffer = ctypes.create_string_buffer(size)
    count = ctypes.c_size_t()
    kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(count)
    )
    return buffer.raw[:count.value] if count.value else b""


def verify_enc_key(enc_key, page):
    if len(enc_key) != 32 or len(page) != PAGE_SZ:
        return False
    salt = bytes(byte ^ 0x3A for byte in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, salt, 2, dklen=32)
    digest = hmac.new(mac_key, page[16:PAGE_SZ - 64], hashlib.sha512)
    digest.update(struct.pack("<I", 1))
    return hmac.compare_digest(digest.digest(), page[-64:])


def collect_db_files(db_dir):
    """仅读取首页；跳过已经解密的 SQLite 文件。"""
    root = Path(db_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("--db-dir 必须是目录")
    files = []
    for path in sorted(root.rglob("*.db")):
        with path.open("rb") as handle:
            page = handle.read(PAGE_SZ)
        if len(page) == PAGE_SZ and not page.startswith(b"SQLite format 3\0"):
            files.append((path, path.stat().st_size, page))
    if not files:
        raise RuntimeError("指定目录没有可验真的加密数据库首页")
    return files


KEY_SZ = 32
KDF_ROUNDS = 256000

# stub 结构 32 字节：
#   偏移 0-7:   48 位用户态指针（高 2 字节为 0），指向真正的 32 字节 key
#   偏移 8-15:  00*8
#   偏移 16-17: 20 00   （uint64 值 0x20 = key 长度 32 的低 2 字节）
#   偏移 18-23: 00*6
#   偏移 24-25: 2f 00   （uint64 值 0x2f = 47 的低 2 字节，结构体字段）
#   偏移 26-31: 00*6
STUB_TAIL = bytes.fromhex("0000" + "00" * 8 + "2000" + "00" * 6 + "2f00" + "00" * 6)

# Weixin.dll 中写入 internal_db_key 常量的指令特征：
# 4 条 mov rdx,<imm64>（48 BA xx*8），中间穿插 3-8 字节存储指令，结尾 test rax,rax（48 85 C0）
DLL_KEY_PATTERN = re.compile(
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\x85\xC0",
    re.DOTALL,
)

CODE_SECTION_CHARACTERISTIC = 0x20000000


def xor_bytes(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def is_potential_key(key):
    """熵初筛：滤掉普通文本/全零等不可能是密钥的 32 字节候选。"""
    if len(key) != KEY_SZ:
        return False
    if len(set(key)) < 15:
        return False
    printable = sum(32 <= b <= 126 for b in key)
    return printable <= 24


def derive_enc_key(passphrase, salt):
    """SQLCipher 4 KDF：passphrase + salt -> 32 字节 enc_key"""
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ROUNDS, dklen=KEY_SZ)


def extract_mask_candidates_from_bytes(data):
    """在 PE 数据（整个文件或代码段）中匹配指令特征，返回 32 字节掩码候选列表。"""
    masks = []
    seen = set()
    for m in re.finditer(b"\x48\xBA", data):
        match = DLL_KEY_PATTERN.match(data, m.start())
        if match:
            key = match.group(1) + match.group(2) + match.group(3) + match.group(4)
            if key not in seen:
                seen.add(key)
                masks.append(key)
    return masks


def extract_internal_db_keys(dll_path, print_fn=print):
    """静态扫描 Weixin.dll，返回 internal_db_key（XOR 掩码）候选列表。"""
    chunks = []
    try:
        import pefile
        pe = pefile.PE(dll_path, fast_load=True)
        try:
            for section in pe.sections:
                if int(section.Characteristics) & CODE_SECTION_CHARACTERISTIC:
                    chunks.append(section.get_data())
        finally:
            pe.close()
    except ImportError:
        # 无 pefile 时退化为整文件扫描（可能多几个误报候选，HMAC 验真会过滤）
        with open(dll_path, "rb") as f:
            chunks.append(f.read())

    masks = []
    seen = set()
    for chunk in chunks:
        for key in extract_mask_candidates_from_bytes(chunk):
            if key not in seen:
                seen.add(key)
                masks.append(key)
    print_fn(f"[*] Weixin.dll 掩码候选: {len(masks)} 个 ({dll_path})")
    return masks


def find_weixin_dll(pid):
    """定位运行中微信进程加载的 Weixin.dll 磁盘路径。

    优先枚举进程模块列表（psapi），失败时回退到 exe 目录下 glob。
    """
    h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ | QUERY_INFORMATION
    if h:
        try:
            # 先取 exe 路径，供 glob 回退使用
            exe_path = None
            buf = ctypes.create_unicode_buffer(512)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(wt.DWORD(512))):
                exe_path = buf.value

            mods = (ctypes.c_void_p * 1024)()
            needed = wt.DWORD(0)
            if psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(needed), 0x03):
                count = min(needed.value // ctypes.sizeof(ctypes.c_void_p), 1024)
                for i in range(count):
                    name_buf = ctypes.create_unicode_buffer(512)
                    if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(mods[i]), name_buf, 512):
                        name = name_buf.value
                        if os.path.basename(name).lower() == "weixin.dll" and os.path.isfile(name):
                            return name
        finally:
            kernel32.CloseHandle(h)

        # glob 回退：exe 同级目录 / 版本子目录 / install 子目录
        if exe_path:
            exe_dir = os.path.dirname(exe_path)
            for pattern in ("Weixin.dll", os.path.join("*", "Weixin.dll"),
                            os.path.join("*", "*", "Weixin.dll")):
                for match in glob.glob(os.path.join(exe_dir, pattern)):
                    if os.path.isfile(match):
                        return match
    return None


def iter_stub_pointers(data):
    """在一块内存数据中匹配 stub 结构，产出候选 key 的 48 位指针。"""
    start = 6  # 指针低 6 字节在 tail 之前
    while True:
        idx = data.find(STUB_TAIL, start)
        if idx < 0:
            return
        ptr = int.from_bytes(data[idx - 6:idx] + b"\x00\x00", "little")
        if 0x10000 <= ptr <= 0x7FFFFFFFFFFF:
            yield ptr
        start = idx + 1


def scan_key_candidates(h, regions, print_fn=print):
    """扫进程私有内存区域，经 stub 指针解引用收集 32 字节 key 候选。"""
    candidates = []
    seen = set()
    for base, size in regions:
        # 31 字节重叠覆盖横跨块边界的 32 字节 stub。
        for offset in range(0, size, CHUNK_SZ):
            data = read_mem(h, base + offset, min(CHUNK_SZ + 31, size - offset))
            for ptr in iter_stub_pointers(data):
                key = read_mem(h, ptr, KEY_SZ)
                if key and len(key) == KEY_SZ and key not in seen:
                    seen.add(key)
                    if is_potential_key(key):
                        candidates.append(key)
    print_fn(f"[*] stub 指针解引用后 key 候选: {len(seen)} 个，熵过滤后: {len(candidates)} 个")
    return candidates


def enum_private_regions(h):
    """枚举 MEM_PRIVATE 的已提交可读区域（stub 位于私有堆内存）。"""
    return [(base, size) for base, size, typ in _enum_regions_typed(h) if typ == MEM_PRIVATE]


def _enum_regions_typed(h):
    regs = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and mbi.RegionSize > 0:
            regs.append((mbi.BaseAddress, mbi.RegionSize, mbi.Type))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def recover_passphrase(pids, probe_page1, masks, print_fn=print):
    """扫各微信进程内存，返回验真通过的 passphrase（32 字节），失败返回 None。"""
    probe_salt = probe_page1[:16]
    for pid, _mem in pids:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            continue
        try:
            regions = enum_private_regions(h)
            total_mb = sum(s for _, s in regions) / 1024 / 1024
            print_fn(f"[*] V4 扫描 PID={pid} 私有内存 {total_mb:.0f}MB ({len(regions)} 区域)")
            candidates = scan_key_candidates(h, regions, print_fn)
        finally:
            kernel32.CloseHandle(h)

        for cand in candidates:
            for mask in masks:
                passphrase = xor_bytes(cand, mask)
                enc_key = derive_enc_key(passphrase, probe_salt)
                if verify_enc_key(enc_key, probe_page1):
                    print_fn(f"[+] V4 密钥验真通过 (PID={pid})")
                    return passphrase
    return None


def write_key(output, passphrase):
    """独占创建，不覆盖现有文件，也不将密钥打印到 stdout。"""
    if len(passphrase) != KEY_SZ:
        raise ValueError("密钥长度无效")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="ascii") as handle:
        handle.write(passphrase.hex() + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-dir", required=True, type=Path, help="微信 db_storage 目录")
    parser.add_argument("--out", required=True, type=Path, help="密钥文本输出，例如 data/db-key.txt")
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("输出文件已存在；请选择新路径")
    files = collect_db_files(args.db_dir)
    init_windows_api()
    pids = get_pids()
    dll = next((path for pid, _ in pids if (path := find_weixin_dll(pid))), None)
    if not dll:
        raise RuntimeError("未定位到 Weixin.dll，请检查当前用户的进程读取权限")
    masks = extract_internal_db_keys(dll)
    if not masks:
        raise RuntimeError("当前 Weixin.dll 未匹配到已知掩码特征")
    probe = max(files, key=lambda item: item[1])[2]
    passphrase = recover_passphrase(pids, probe, masks)
    if passphrase is None:
        raise RuntimeError("未找到通过数据库首页验真的密钥")
    verified = sum(
        verify_enc_key(derive_enc_key(passphrase, page[:16]), page)
        for _, _, page in files
    )
    write_key(args.out, passphrase)
    print(f"[+] 已写入密钥文件；首页验真 {verified}/{len(files)} 个数据库通过")
    if verified != len(files):
        print("[!] 部分数据库未通过；可能属于其他账号或使用不同密钥")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"[ERROR] {error}")
        raise SystemExit(1)
