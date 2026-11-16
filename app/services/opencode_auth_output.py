"""Extract only the public device-login fields from native CLI output."""
from __future__ import annotations

import re


_ANSI = re.compile(r"\x1b\][^\x1b\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_URL = re.compile(r"https://(?:auth\.openai\.com|chatgpt\.com)/[^\s<>\"\x00-\x1f]+")
_LABELED_CODE = re.compile(
    r"\b(?:enter\s+(?:the\s+)?code|(?:device|user|verification|authorization)\s+code)"
    r"\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9-]{4,30}[A-Za-z0-9])(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_BARE_CODE = re.compile(r"(?:[A-Za-z0-9]{4}-[A-Za-z0-9]{4}|[0-9]{9})")


def parse_device_login_line(line: str) -> dict[str, str]:
    """The upstream `Enter code:` value is opaque, not necessarily XXXX-XXXX.

    Keep numeric codes as strings (including leading zeroes). Do not return raw
    output or mistake arbitrary token-looking log lines for authorization codes.
    """
    text = _ANSI.sub("", line)
    result = {}
    url = _URL.search(text)
    if url:
        result["url"] = url[0].rstrip(".,;）)│")
    code = _LABELED_CODE.search(text)
    if code:
        result["user_code"] = code[1]
    else:
        bare = text.strip(" \t\r\n│┃┆┊║>")
        if _BARE_CODE.fullmatch(bare):
            result["user_code"] = bare
    return result
