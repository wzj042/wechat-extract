"""Read-only release preflight; findings never include matched secret values.

Checks tracked working files, staged files and reachable Git blobs. This is a small heuristic
scanner, not a credential audit or a determination of licensing rights.
"""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


MAX_BLOB = 2 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "service_token": re.compile(rb"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
    "personal_windows_path": re.compile(rb"(?i)[a-z]:[/\\]Users[/\\](?!<|example[/\\]|user[/\\])[^/\\\s\"']+"),
    "numeric_chat_id": re.compile(rb"\b[0-9]{5,}@chatroom\b"),
}
PRIVATE_DIRS = {"data", "output", "out", "runtime", "logs", ".venv", ".test-tmp", ".uv-cache"}


def git(root, *args, data=None):
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args],
        input=data, capture_output=True, check=False,
    )
    if result.returncode:
        # Git errors can contain paths or credential-bearing remote URLs.
        raise RuntimeError("git_read_failed")
    return result.stdout


def private_path(name):
    path = PurePosixPath(name)
    base = path.name.lower()
    return (any(part.lower() in PRIVATE_DIRS for part in path.parts)
            or base in {".env", "db-key.txt", "config.json", "mcp-config.json"}
            or (base.startswith("mcp-config.") and base.endswith(".json") and base != "mcp-config.example.json")
            or (base.startswith(".env.") and base != ".env.example")
            or base.endswith((".db", ".sqlite", ".sqlite3", ".dat", ".key", ".pem", ".pfx", ".p12"))
            or ".db-" in base or "passphrase" in base
            or ("keys" in base and base.endswith(".json")))


def audit_privacy(root, denylist=()):
    findings = []
    private_literals = [value.encode("utf-8") for value in denylist if value]

    def finding(scope, path, rule):
        item = {"scope": scope, "path": path, "rule": rule}
        if item not in findings:
            findings.append(item)

    def scan(scope, name, content):
        if len(content) > MAX_BLOB:
            finding(scope, name, "oversized_file_requires_review")
            return
        if b"\0" in content:
            finding(scope, name, "binary_file_requires_review")
            return
        for rule, pattern in PATTERNS.items():
            if pattern.search(content):
                finding(scope, name, rule)
        if any(value in content for value in private_literals):
            finding(scope, name, "local_denylist_match")

    tracked = git(root, "ls-files", "-z").decode("utf-8").split("\0")
    for name in filter(None, tracked):
        if private_path(name):
            finding("working_tree", name, "private_data_path")
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            finding("working_tree", name, "linked_file_requires_review")
        elif path.is_file():
            with path.open("rb") as stream:
                scan("working_tree", name, stream.read(MAX_BLOB + 1))
        else:
            finding("working_tree", name, "tracked_file_missing")

    objects = {}
    for line in git(root, "rev-list", "--objects", "--all").decode("utf-8").splitlines():
        oid, _, name = line.partition(" ")
        objects.setdefault(oid, []).append(("history", name))
        if name and private_path(name):
            finding("history", name, "private_data_path")
    # rev-list assigns only one name to an object shared by several paths.
    # Inspect every reachable tree so an old private path cannot hide behind
    # an identical blob also used by a harmless file.
    for commit in git(root, "rev-list", "--all").decode().splitlines():
        for entry in filter(None, git(root, "ls-tree", "-r", "-z", commit).decode("utf-8").split("\0")):
            metadata, name = entry.split("\t", 1)
            if private_path(name):
                finding("history", name, "private_data_path")
            if metadata.split()[0] in {"160000", "120000"}:
                finding("history", name, "linked_entry_requires_review")
    for entry in filter(None, git(root, "ls-files", "--stage", "-z").decode("utf-8").split("\0")):
        metadata, name = entry.split("\t", 1)
        mode, oid, stage = metadata.split()
        if stage != "0" or mode in {"160000", "120000"}:
            finding("index", name, "unmerged_or_linked_entry_requires_review")
            continue
        objects.setdefault(oid, []).append(("index", name))
        if private_path(name):
            finding("index", name, "private_data_path")
    payload = "".join(oid + "\n" for oid in objects).encode()
    info = git(root, "cat-file", "--batch-check", data=payload).decode().splitlines()
    blobs = []
    total = 0
    for line in info:
        oid, kind, size = line.split()
        if kind != "blob":
            continue
        size = int(size)
        if size > MAX_BLOB:
            for scope, name in objects[oid]:
                finding(scope, name, "oversized_file_requires_review")
        else:
            total += size
            blobs.append(oid)
    if total > MAX_TOTAL:
        raise RuntimeError("history_scan_budget_exceeded")
    data = git(root, "cat-file", "--batch", data="".join(oid + "\n" for oid in blobs).encode())
    offset = 0
    for oid in blobs:
        end = data.index(b"\n", offset)
        size = int(data[offset:end].split()[2])
        for scope, name in objects[oid]:
            scan(scope, name, data[end + 1:end + 1 + size])
        offset = end + size + 2
    identities = git(root, "log", "--all", "--format=%ae%n%ce").splitlines()
    warnings = ["git_author_and_committer_emails_will_be_published"] if any(identities) else []
    return {"status": "blocked" if findings else "passed", "findings": findings,
            "warnings": warnings, "tracked_files": len(list(filter(None, tracked))),
            "git_blobs": len(blobs),
            "limits": "Heuristics only; review untracked release inputs and Git metadata separately."}


def audit_licenses(root):
    try:
        manifest = json.loads((root / "docs/release-audit.json").read_text(encoding="utf-8"))
        issues = manifest["issues"]
        if not issues or not all(isinstance(i, dict) and i.get("id") and i.get("status") for i in issues):
            raise ValueError("invalid_manifest")
        unresolved = [i["id"] for i in issues if i["status"] != "resolved"]
        license_file = root / "LICENSE"
        if not license_file.is_file() or not license_file.read_text(encoding="utf-8").strip():
            unresolved.append("project_license_missing")
        return {"status": "blocked" if unresolved else "passed", "unresolved": unresolved}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "blocked", "unresolved": ["license_review_manifest_missing_or_invalid"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--privacy-only", action="store_true", help="Only scan data exposure; does not approve publication")
    parser.add_argument("--denylist", type=Path, help="Local UTF-8 file of exact private strings, one per line; keep outside Git")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.repo.resolve()
    try:
        denylist = args.denylist.read_text(encoding="utf-8-sig").splitlines() if args.denylist else ()
        result = {"privacy": audit_privacy(root, denylist)}
        if not args.privacy_only:
            result["licensing"] = audit_licenses(root)
        result["status"] = "passed" if all(v["status"] == "passed" for v in result.values()) else "blocked"
    except (OSError, RuntimeError, UnicodeError, ValueError):
        result = {"status": "blocked", "reason": "audit_incomplete"}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
