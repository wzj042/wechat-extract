"""Log redaction filters (stdlib-only subset).

Extracted from WeChatDataAnalysis `request_logging.py`. The original module
also contains a Starlette/FastAPI error-logging middleware
(`log_server_errors_middleware`); that part is intentionally dropped in this
standalone package because it has no web server and no `starlette` dependency.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_plus


_CONTENT_LOG_KEYS = {
    "contentbase64",
    "contentb64",
    "database64",
    "datab64",
    "filebase64",
    "fileb64",
    "protectedcontentbase64",
}
_SECRET_LOG_KEYS = {
    "apikey",
    "aeskey",
    "auth",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "databasekey",
    "dbkey",
    "encryptionkey",
    "key",
    "keyhex",
    "imageaeskey",
    "imagexorkey",
    "mcptoken",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "secretkey",
    "setcookie",
    "token",
    "xorkey",
}
_URL_QUERY_LOG_KEYS = {
    "remoteurl",
    "sourceurl",
    "url",
}


def _normalized_log_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _redacted_content_marker(value: Any) -> str:
    try:
        length = len(value)
    except Exception:
        return "<redacted>"
    return f"<redacted length={length}>"


def _is_sensitive_log_key(key: Any) -> bool:
    normalized = _normalized_log_key(key)
    return bool(
        normalized in _SECRET_LOG_KEYS
        or normalized.endswith("password")
        or normalized.endswith("secret")
        or normalized.endswith("token")
    )


def redact_sensitive_log_data(value: Any, *, _depth: int = 0) -> Any:
    """Return a log-safe copy of structured request or error data."""
    if _depth >= 20:
        return "<redacted max-depth>"
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = _normalized_log_key(key)
            if normalized in _CONTENT_LOG_KEYS or normalized.endswith(("base64", "b64")):
                redacted[key] = _redacted_content_marker(item)
            elif _is_sensitive_log_key(key):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_sensitive_log_data(item, _depth=_depth + 1)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_log_data(item, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_log_data(item, _depth=_depth + 1) for item in value)
    return value


def redact_sensitive_query_text(value: Any) -> str:
    """Redact sensitive query values without decoding or rewriting unrelated values."""
    text = str(value or "")
    if "?" not in text:
        return text

    prefix, query = text.split("?", 1)
    redacted_parts: list[str] = []
    for part in query.split("&"):
        raw_key, separator, raw_value = part.partition("=")
        try:
            decoded_key = unquote_plus(raw_key)
        except Exception:
            decoded_key = raw_key
        normalized_key = _normalized_log_key(decoded_key)
        if separator and (
            _is_sensitive_log_key(decoded_key) or normalized_key in _URL_QUERY_LOG_KEYS
            or normalized_key in {'q', 'query', 'keyword', 'keywords', 'question', 'prompt', 'content', 'text', 'condition'}
        ):
            redacted_parts.append(f"{raw_key}=<redacted>")
        else:
            redacted_parts.append(part)
    return f"{prefix}?{'&'.join(redacted_parts)}"


class SensitiveQueryLogFilter(logging.Filter):
    """Redact the path argument used by Uvicorn's access-log record."""

    _wda_sensitive_query_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            updated = list(args)
            updated[2] = redact_sensitive_query_text(updated[2])
            record.args = tuple(updated)
        return True


class SensitiveHttpClientLogFilter(logging.Filter):
    """Drop dependency request logs that contain complete remote URLs.

    SNS media diagnostics are emitted separately with host, size suffix, byte
    counts, and hashed identities. The raw httpx/httpcore request records add no
    actionable information and can expose credential-bound CDN URLs.
    """

    _wda_sensitive_http_client_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        logger_name = str(record.name or "").lower()
        diagnostics = sys.modules.get('wechat_extract.ai.diagnostics')
        if diagnostics and diagnostics.context.get().get('trace_id') and logger_name != diagnostics.logger.name and logger_name != 'uvicorn.access':
            # AI 调用链内旧模块/依赖的自由文本不能越过统一白名单。
            fields = diagnostics.safe_fields(diagnostics.context.get())
            if record.exc_info and record.exc_info[1]:
                fields.update(diagnostics.exception_fields(record.exc_info[1]))
            record.msg = '[ai.dependency] 依赖诊断 ' + json.dumps(fields, ensure_ascii=False, separators=(',', ':'))
            record.args, record.exc_info, record.exc_text, record.stack_info = (), None, None, None
        return not (
            logger_name.split('.')[0] in {'anthropic', 'openai', 'langchain', 'langchain_core', 'langchain_openai', 'langchain_anthropic', 'huggingface_hub'}
            or
            logger_name == "httpx"
            or logger_name.startswith("httpx.")
            or logger_name == "httpcore"
            or logger_name.startswith("httpcore.")
        )
