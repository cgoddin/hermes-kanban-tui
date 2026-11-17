"""Terminal-safe text rendering shared by the viewer and formatter.

Redaction is deliberately heuristic, not a claim that arbitrary prose can be
classified perfectly. No caller should select secret-bearing columns at all.
"""

import re
import unicodedata


_STRINGS = re.compile(r"(?:\x1b\]|\x9d|\x1b[P^_X]|[\x90\x98\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)", re.S)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ESC = re.compile(r"\x1b[ -/]*[@-~]")
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", re.S)
_ENV = re.compile(r"\b[A-Z_][A-Z0-9_]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\n]*)")
_SHELL_ENV = re.compile(r"\b(?:export|env)\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\n]*)")
_SECRET = re.compile(
    r"(?i)\b(?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential)[\w.-]*|authorization|cookie)\b"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\n,;]+)"
)
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_FLAG = re.compile(
    r"(?i)--(?:password|passwd|secret|token|api[_-]?key|access[_-]?token|credential)"
    r"(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_AUTH = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+(?::[^\s/@]*)?@")
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|AKIA[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_PROCESS = re.compile(r"(?im)\b(?:cmdline|command_line|process[_ ](?:command|cmdline|environment)|environ)\s*[:=][^\n]*")


def sanitize(text) -> str:
    """Remove terminal/control spoofing and redact likely credentials/env values.

    Newlines are retained for prose; tabs and CR become spaces. Escape strings
    (including unterminated OSC/DCS) are removed together with their payload.
    All other C0/C1, surrogate and format characters (including bidi) vanish.
    This function never reads environment variables or process information.
    """
    value = "" if text is None else str(text)
    value = _STRINGS.sub("", value)
    value = _CSI.sub("", value)
    value = _ESC.sub("", value)
    value = value.replace("\t", " ").replace("\r", " ")
    value = "".join(
        char for char in value
        if char == "\n" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    value = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    value = _PROCESS.sub("[REDACTED PROCESS DATA]", value)
    value = _SHELL_ENV.sub(lambda m: m.group(0).split("=", 1)[0].rstrip() + "=[REDACTED]", value)
    value = _ENV.sub(lambda m: m.group(0).split("=", 1)[0].rstrip() + "=[REDACTED]", value)
    value = _SECRET.sub(lambda m: re.split(r"\s*[:=]", m.group(0), maxsplit=1)[0] + "=[REDACTED]", value)
    value = _SECRET_FLAG.sub("[REDACTED FLAG]", value)
    value = _BEARER.sub("[REDACTED AUTH]", value)
    value = _URL_AUTH.sub(r"\1[REDACTED]@", value)
    return _TOKEN.sub("[REDACTED TOKEN]", value)
