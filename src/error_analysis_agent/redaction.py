"""Redact secrets and personal data out of free-form text.

Applied to everything that leaves Sentry on its way to the Codex prompt or to
Slack. The rules are deliberately keyed on secret *names* and on well-known
token shapes instead of "any long random string", so stack traces, commit SHAs
and file paths survive intact.
"""

from __future__ import annotations

import re
from typing import Pattern

PLACEHOLDER = "[REDACTED]"

# Field names whose value is dropped wherever it appears as `name=value`,
# `name: value` or `"name": "value"`.
SECRET_KEY_NAMES: tuple[str, ...] = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "secret",
    "secret_key",
    "private_key",
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "credential",
    "credentials",
    "session_id",
    "jsessionid",
    "token",
    "auth",
    "signature",
    "ssn",
    "card_number",
)

_KEY_ALTERNATION = "|".join(re.escape(name) for name in SECRET_KEY_NAMES)

_RULES: tuple[tuple[Pattern[str], str], ...] = (
    # PEM blocks first: they span lines and would be chewed up by later rules.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        PLACEHOLDER,
    ),
    # Credentials embedded in a URL: scheme://user:pass@host
    (re.compile(r"(?P<scheme>\b[a-zA-Z][\w+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\g<scheme>" + PLACEHOLDER + "@"),
    (re.compile(r"\b(?:Bearer|Basic|Digest)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), PLACEHOLDER),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), PLACEHOLDER),
    # Well-known provider tokens
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), PLACEHOLDER),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{8,}"), PLACEHOLDER),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), PLACEHOLDER),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), PLACEHOLDER),
    # name=value / "name": "value" / name: value
    (
        re.compile(
            rf"(?P<key>[\"']?\b(?:{_KEY_ALTERNATION})\b[\"']?\s*[:=]\s*)"
            r"(?P<quote>[\"'])(?!\[REDACTED\])(?P<value>(?:(?!(?P=quote)).)*)(?P=quote)",
            re.IGNORECASE,
        ),
        r"\g<key>\g<quote>" + PLACEHOLDER + r"\g<quote>",
    ),
    (
        re.compile(
            rf"(?P<key>\b(?:{_KEY_ALTERNATION})\b\s*[:=]\s*)"
            r"(?!\[REDACTED\])(?P<value>[^\s,;&}\)\]]+)",
            re.IGNORECASE,
        ),
        r"\g<key>" + PLACEHOLDER,
    ),
    # Personal data
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), PLACEHOLDER),
    (re.compile(r"\b\d{6}-[1-4]\d{6}\b"), PLACEHOLDER),  # 주민등록번호
    (re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b"), PLACEHOLDER),  # card-like
    (re.compile(r"(?<![\w.-])01[016-9][ .-]?\d{3,4}[ .-]?\d{4}(?![\w.-])"), PLACEHOLDER),
    (re.compile(r"(?<![\w.-])\+\d{1,3}[ .-]?\d{1,2}[ .-]?\d{3,4}[ .-]?\d{4}(?![\w.-])"), PLACEHOLDER),
    # IPv4, keeping loopback/any so "connection refused to 127.0.0.1" stays readable
    (
        re.compile(
            r"(?<![\w.])(?!127\.0\.0\.1|0\.0\.0\.0)"
            r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
            r"(?![\w.])"
        ),
        PLACEHOLDER,
    ),
)


def redact(text: str | None) -> str:
    """Return ``text`` with secrets and personal data replaced."""
    if not text:
        return ""
    result = str(text)
    for pattern, replacement in _RULES:
        result = pattern.sub(replacement, result)
    return result


def is_secret_key(name: str) -> bool:
    """True when a header/tag/field name must never have its value forwarded."""
    lowered = name.strip().lower()
    return any(secret in lowered for secret in SECRET_KEY_NAMES)
