"""Command line entry point for wechat-extract."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
import os
import sys
from pathlib import Path

# The optional session_last_message cache table depends on heavy modules that
# are not part of this standalone package. Skip it by default (it is guarded
# by try/except upstream, but skipping avoids a warning on every run).
os.environ.setdefault("WECHAT_TOOL_BUILD_SESSION_LAST_MESSAGE", "0")

ENV_KEY_NAME = "WECHAT_DB_KEY"

_COMMANDS = ("decrypt", "watch", "messages", "image-path", "export-media")


def _resolve_key(args: argparse.Namespace) -> str:
    """Resolve the 64-char hex key from --key, --key-file or the environment."""
    key = str(args.key or "").strip()
    if key:
        return key

    if args.key_file:
        try:
            key = Path(args.key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"读取密钥文件失败: {args.key_file}: {exc}")
        if key:
            return key
        raise SystemExit(f"密钥文件为空: {args.key_file}")

    return str(os.environ.get(ENV_KEY_NAME, "") or "").strip()


def _validate_key() -> str:
    """Backwards-compatible shim; real validation lives in _resolve_and_check_key."""
    raise NotImplementedError


def _resolve_and_check_key(args: argparse.Namespace) -> str:
    key = _resolve_key(args)
    if not key:
        print(
            f"错误: 未提供解密密钥。请使用 --key、--key-file 或环境变量 {ENV_KEY_NAME}。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if len(key) != 64:
        print("错误: 密钥必须是 64 位十六进制字符串。", file=sys.stderr)
        raise SystemExit(2)
    try:
        bytes.fromhex(key)
    except ValueError:
        print("错误: 密钥必须是有效的十六进制字符串。", file=sys.stderr)
        raise SystemExit(2)
    return key


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--key",
        help=f"64 位十六进制数据库密钥。也可用 --key-file 或环境变量 {ENV_KEY_NAME} 提供。",
    )
    parser.add_argument(
        "--key-file",
        help="包含 64 位十六进制密钥的文本文件路径。",
    )
    parser.add_argument(
        "--input",
        required=True,
        dest="input",
        help="微信账号的 db_storage 目录路径，例如 .../xwechat_files/<wxid>/db_storage",
    )
    parser.add_argument(
        "--output",
        help=(
            "输出根目录（默认：当前工作目录下的 output/）。"
            "解密结果写入 <output>/databases/<账号名>/，日志写入 <output>/logs/。"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-extract",
        description=(
            "解密微信 4.x 数据库（SQLCipher 4.0 / WCDB 参数："
            "PBKDF2-SHA512 256000 轮、AES-256-CBC、HMAC-SHA512、页大小 4096）。"
            "本工具只负责解密，不负责密钥提取。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    decrypt_parser = subparsers.add_parser(
        "decrypt",
        help="一次性解密 db_storage 下的所有数据库（默认命令，可省略）",
    )
    _add_common_arguments(decrypt_parser)

    watch_parser = subparsers.add_parser(
        "watch",
        help="轮询监听源库变化，增量解密（不干扰微信数据库实例）",
    )
    _add_common_arguments(watch_parser)
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="轮询间隔秒数（默认 2.0）。",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮全量/增量解密即退出。",
    )
    messages = subparsers.add_parser("messages", help="读取明文账号目录的跨分片消息，输出 JSON")
    messages.add_argument("--account-dir", required=True, help="已解密的单账号目录")
    messages.add_argument("--chat", required=True, help="会话 username，不是昵称")
    messages.add_argument("--limit", type=int, default=100)
    messages.add_argument("--offset", type=int, default=0)
    messages.add_argument("--local-id", type=int, help="精确取一条消息")
    messages.add_argument("--shard", help="与 --local-id 同用，原样传回消息的 shard")
    image_path = subparsers.add_parser("image-path", help="按分片消息定位本地图片，输出 JSON")
    image_path.add_argument("--account-dir", required=True, help="已解密的单账号目录")
    image_path.add_argument("--source-account-dir", required=True, help="含 msg/attach 的实际账号目录")
    image_path.add_argument("--chat", required=True)
    image_path.add_argument("--local-id", required=True, type=int)
    image_path.add_argument("--shard", required=True, help="原样传回消息的 shard")
    image_path.add_argument("--tier", choices=("best", "original", "mid", "thumb"), default="best")
    export = subparsers.add_parser("export-media", help="按完整消息身份导出本地媒体")
    export.add_argument("--account-dir", required=True, help="已解密单账号目录")
    export.add_argument("--source-account-dir", required=True, help="原账号目录")
    export.add_argument("--chat", required=True)
    export.add_argument("--local-id", required=True, type=int)
    export.add_argument("--shard", required=True)
    export.add_argument("--output", default="output/media")
    export.add_argument("--kind", choices=("auto", "image", "voice", "video", "file", "record-file"), default="auto")
    export.add_argument("--tier", choices=("best", "original", "mid", "thumb"), default="best")
    export.add_argument("--image-key-file", help="可选 JSON 密钥文件，字段 aes_key、xor_key；默认从本机 kvcomm 推导")
    export.add_argument("--userprofile", help="本机 kvcomm 所在用户主目录")
    export.add_argument("--aes-size-mode", choices=("auto", "ciphertext", "plaintext"), default="auto")
    export.add_argument("--ffmpeg", default="ffmpeg")
    export.add_argument("--data-index", type=int, help="record-file 的 0 基项目序号")
    export.add_argument("--filename", help="record-file 的真实文件名")
    return parser


def _apply_output_env(args: argparse.Namespace) -> Path:
    """Set WECHAT_TOOL_OUTPUT_DIR (logs + decrypt 输出布局) and return output root."""
    if args.output:
        output_root = Path(args.output).expanduser().resolve()
    else:
        output_root = (Path.cwd() / "output").resolve()
    # app_paths.get_output_databases_dir() = WECHAT_TOOL_OUTPUT_DIR/databases
    os.environ["WECHAT_TOOL_OUTPUT_DIR"] = str(output_root)
    return output_root


def _run_decrypt(args: argparse.Namespace) -> int:
    key = _resolve_and_check_key(args)
    _apply_output_env(args)

    from .wechat_decrypt import decrypt_wechat_databases

    result = decrypt_wechat_databases(db_storage_path=args.input, key=key)

    print(result.get("message") or "")
    print(f"输出目录: {result.get('output_directory', '')}")
    total = int(result.get("total_databases") or 0)
    if total:
        print(f"成功: {result.get('successful_count', 0)}/{total}")
    failed_files = result.get("failed_files") or []
    if failed_files:
        print("失败的文件:")
        for path in failed_files:
            print(f"  - {path}")

    complete = (
        result.get("status") == "success"
        and total > 0
        and int(result.get("successful_count") or 0) == total
        and not result.get("failed_count")
        and not failed_files
    )
    return 0 if complete else 1


def _run_watch(args: argparse.Namespace) -> int:
    key = _resolve_and_check_key(args)
    output_root = _apply_output_env(args)

    if float(args.interval) <= 0:
        print("错误: --interval 必须大于 0。", file=sys.stderr)
        return 2

    from .live import watch

    stats = watch(
        db_storage=args.input,
        key=key,
        output_dir=output_root,
        interval=float(args.interval),
        once=bool(args.once),
    )

    print(
        "watch 结束: "
        f"轮次 {stats['cycles']}，本次解密 {stats['decrypted']}，"
        f"跳过未变化 {stats['skipped']}，失败 {stats['failed']}"
    )
    print(f"输出目录: {output_root / 'databases'}")
    for path in stats.get("files") or []:
        print(f"  - {path}")
    return 0 if stats["failed"] == 0 else 1


def _json_value(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"不能序列化 {type(value).__name__}")


def _run_read(args: argparse.Namespace) -> int:
    from .messages import MessageReader

    try:
        if args.command == "messages":
            reader = MessageReader(args.account_dir)
            if args.shard and args.local_id is None:
                raise ValueError("--shard 必须与 --local-id 同用")
            if args.local_id is None:
                result = reader.list_messages(args.chat, limit=args.limit, offset=args.offset)
            else:
                result = reader.get_message(args.chat, args.local_id, shard=args.shard)
            # API 保留原始 bytes；CLI 不展开压缩正文/packed_info 等二进制字段。
            if isinstance(result, list):
                result = [{k: v for k, v in row.items() if not isinstance(v, bytes)} for row in result]
            elif isinstance(result, dict):
                result = {k: v for k, v in result.items() if not isinstance(v, bytes)}
            success = result is not None
        else:
            from .media import resolve_image
            result = resolve_image(
                args.account_dir, args.source_account_dir, args.chat, args.local_id,
                shard=args.shard, tier=args.tier,
            )
            success = result["status"] == "ok"
        print(json.dumps(result, ensure_ascii=False, default=_json_value, indent=2))
        return 0 if success else 1
    except (OSError, ValueError, RuntimeError, LookupError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def _run_export(args: argparse.Namespace) -> int:
    from .export_media import export_media
    from .wxdat import ImageKeyProvider

    try:
        aes_key = xor_key = None
        if args.image_key_file:
            try:
                config = json.loads(Path(args.image_key_file).read_text(encoding="utf-8-sig"))
                aes_key = config["aes_key"]
                xor_key = config["xor_key"]
                if isinstance(xor_key, str):
                    xor_key = int(xor_key, 0)
                if not isinstance(aes_key, str) or isinstance(xor_key, bool) or not isinstance(xor_key, int):
                    raise ValueError()
                if len(aes_key) not in (16, 32) or not 0 <= xor_key <= 255:
                    raise ValueError()
                if len(aes_key) == 16:
                    aes_key.encode("ascii")
                elif len(bytes.fromhex(aes_key)) != 16:
                    raise ValueError()
            except (OSError, ValueError, TypeError, KeyError):
                print("错误: 图片密钥文件无效，需要 aes_key(16位ASCII或32位hex)、xor_key(0..255)", file=sys.stderr)
                return 2
        result = export_media(
            args.account_dir, args.source_account_dir, args.chat, args.local_id,
            args.output, shard=args.shard, kind=args.kind, tier=args.tier,
            aes_key=aes_key, xor_key=xor_key,
            key_provider=ImageKeyProvider(args.userprofile) if args.userprofile else None,
            aes_size_mode=args.aes_size_mode, ffmpeg=args.ffmpeg,
            data_index=args.data_index, filename=args.filename,
        )
        print(json.dumps(result, ensure_ascii=False, default=_json_value, indent=2))
        return 0 if result["status"] == "ok" else 1
    except (OSError, ValueError, RuntimeError, LookupError):
        # 导出错误不得把密钥文件内容或私有消息正文带进控制台。
        print("错误: 媒体导出失败；请核对消息身份、文件路径与读取权限", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 向后兼容：无子命令时按 decrypt 处理（沿用旧的 `--key ... --input ...` 无子命令用法）。
    # 注意：必须在构建 parser 前完成改写，否则顶层 parser 会把 --key 当作未知参数。
    if argv and argv[0] in ("-h", "--help"):
        build_parser().parse_args(argv)  # 打印顶层帮助后 SystemExit(0)
    if not argv or argv[0] not in _COMMANDS:
        argv = ["decrypt", *argv]

    args = build_parser().parse_args(argv)
    if args.command in ("messages", "image-path"):
        return _run_read(args)
    if args.command == "export-media":
        return _run_export(args)
    if args.command == "watch":
        return _run_watch(args)
    return _run_decrypt(args)


if __name__ == "__main__":
    raise SystemExit(main())
