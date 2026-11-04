"""Exercise publication checks against disposable Git repositories."""

import importlib.util
import json
from pathlib import Path
import subprocess


spec = importlib.util.spec_from_file_location("check_release", Path(__file__).resolve().parents[1] / "tools/check_release.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def repository(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Synthetic")
    git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    (tmp_path / "README.md").write_text("Synthetic repository\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def test_deleted_secret_is_detected_in_history_without_printing_value(tmp_path):
    root = repository(tmp_path)
    secret = "ghp_" + "SyntheticCredential" * 3
    (root / "temporary.txt").write_text(secret, encoding="utf-8")
    git(root, "add", "temporary.txt")
    git(root, "commit", "-m", "fixture")
    git(root, "rm", "temporary.txt")
    git(root, "commit", "-m", "remove fixture")
    result = checker.audit_privacy(root)
    assert result["status"] == "blocked"
    assert {"scope": "history", "path": "temporary.txt", "rule": "service_token"} in result["findings"]
    assert secret not in json.dumps(result)


def test_ignored_private_data_is_excluded_but_force_added_data_is_blocked(tmp_path):
    root = repository(tmp_path)
    (root / ".gitignore").write_text("data/\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data/private.db").write_bytes(b"synthetic SQLite fixture")
    assert checker.audit_privacy(root)["status"] == "passed"
    git(root, "add", "-f", "data/private.db")
    assert any(i["rule"] == "private_data_path" for i in checker.audit_privacy(root)["findings"])


def test_uncommitted_change_is_scanned(tmp_path):
    root = repository(tmp_path)
    (root / "README.md").write_text("sk-" + "SYNTHETIC" * 4, encoding="utf-8")
    assert any(i["scope"] == "working_tree" and i["rule"] == "service_token"
               for i in checker.audit_privacy(root)["findings"])


def test_staged_secret_is_detected_even_when_working_copy_is_clean(tmp_path):
    root = repository(tmp_path)
    (root / "README.md").write_text("sk-" + "SYNTHETIC" * 4, encoding="utf-8")
    git(root, "add", "README.md")
    (root / "README.md").write_text("Safe working copy\n", encoding="utf-8")
    assert any(i["scope"] == "index" and i["rule"] == "service_token"
               for i in checker.audit_privacy(root)["findings"])


def test_unknown_or_missing_license_review_fails_closed(tmp_path):
    assert checker.audit_licenses(tmp_path)["status"] == "blocked"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/release-audit.json").write_text(
        json.dumps({"issues": [{"id": "upstream", "status": "unreviewed"}]}), encoding="utf-8")
    (tmp_path / "LICENSE").write_text("Synthetic license fixture", encoding="utf-8")
    assert checker.audit_licenses(tmp_path)["unresolved"] == ["upstream"]


def test_local_denylist_finds_old_nickname_without_echoing_it(tmp_path):
    root = repository(tmp_path)
    nickname = "SyntheticPrivate" + "Nickname"
    (root / "README.md").write_text(nickname, encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "synthetic name")
    (root / "README.md").write_text("Generic name", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "sanitized name")
    result = checker.audit_privacy(root, [nickname])
    assert any(i["scope"] == "history" and i["rule"] == "local_denylist_match" for i in result["findings"])
    assert nickname not in json.dumps(result)


def test_private_historical_path_with_same_blob_as_readme_is_detected(tmp_path):
    root = repository(tmp_path)
    (root / "account.db").write_bytes((root / "README.md").read_bytes())
    git(root, "add", "account.db")
    git(root, "commit", "-m", "duplicate blob fixture")
    git(root, "rm", "account.db")
    git(root, "commit", "-m", "remove private path")
    assert {"scope": "history", "path": "account.db", "rule": "private_data_path"} in checker.audit_privacy(root)["findings"]
