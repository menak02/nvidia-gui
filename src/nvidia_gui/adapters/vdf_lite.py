"""A tiny, dependency-free Valve Data Format (VDF) parser/writer.

Steam uses VDF for ``libraryfolders.vdf``, ``appmanifest_*.acf``, and
``localconfig.vdf``. We don't need the full grammar — just enough to read those
files robustly and to rewrite the ``LaunchOptions`` value inside
``localconfig.vdf`` for a given appid.

The parser returns nested dicts (lists are dicts too in VDF-land: repeated keys
collapse to the last one, which is fine for our use). The writer emits a
canonical, minimal form suitable for re-stamping LaunchOptions.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["loads", "parse", "dumps", "VdfError"]


class VdfError(ValueError):
    """Raised on unrecoverable VDF syntax errors."""


_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<comment>//[^\n]*)
      | (?P<str>"(?:\\.|[^"\\])*")
      | (?P<open>\{)
      | (?P<close>\})
      | (?P<bare>[^\s{}"]]+)        # unquoted tokens (rare in real files)
    )
    """,
    re.VERBOSE,
)


def loads(text: str) -> dict[str, Any]:
    """Parse VDF text into nested dicts."""
    parser = _Parser(text)
    return parser.parse_root()


def parse(text: str) -> dict[str, Any]:
    """Alias of :func:`loads`."""
    return loads(text)


def dumps(obj: dict[str, Any], indent: int = 0) -> str:
    """Serialize a nestable dict back to VDF text (canonical 4-space indent)."""
    return _dump(obj, indent)


class _Parser:
    __slots__ = ("_s", "_pos")

    def __init__(self, text: str) -> None:
        self._s = text
        self._pos = 0

    def parse_root(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        self._parse_block(out, top=True)
        return out

    def _parse_block(self, target: dict[str, Any], top: bool = False) -> None:
        while True:
            tok = self._next_token()
            if tok is None:
                if not top:
                    raise VdfError("unexpected EOF inside block")
                return  # end of input at top level
            if tok == "}":
                if top:
                    raise VdfError("unexpected '}' at top level")
                return
            if tok == "{":
                raise VdfError("unexpected '{' (missing key)")
            key = tok
            nxt = self._next_token()
            if nxt is None:
                raise VdfError(f"EOF after key {key!r}")
            if nxt == "{":
                child: dict[str, Any] = {}
                self._parse_block(child, top=False)
                target[key] = child
            elif nxt == "}" or nxt == "{":
                raise VdfError(f"unexpected token {nxt!r} after key {key!r}")
            else:
                target[key] = nxt

    def _next_token(self) -> str | None:
        while True:
            m = _TOKEN_RE.match(self._s, self._pos)
            if not m:
                # no more tokens; ensure only whitespace remains
                rest = self._s[self._pos:]
                if rest.strip() == "":
                    return None
                raise VdfError(f"unparsable remainder at {self._pos}: {rest[:20]!r}")
            self._pos = m.end()
            if m.group("comment"):
                continue
            if m.group("str"):
                return _unescape(m.group("str")[1:-1])
            if m.group("open"):
                return "{"
            if m.group("close"):
                return "}"
            if m.group("bare"):
                return m.group("bare")
        # unreachable


def _unescape(s: str) -> str:
    # handle \\ \" and any other \x; unknown escapes -> raw char
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _escape(s: str) -> str:
    # Mirror _unescape (\\ \" \n \t \r) so dumps output is canonical and a value
    # containing control characters round-trips instead of embedding raw bytes.
    return (
        s.replace("\\", "\\\\").replace('"', '\\"')
         .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def _dump(obj: dict[str, Any], indent: int) -> str:
    pad = "\t" * indent
    lines: list[str] = []
    for key, val in obj.items():
        key_q = f'"{_escape(str(key))}"'
        if isinstance(val, dict):
            lines.append(f"{pad}{key_q}")
            lines.append(f"{pad}{{")
            lines.append(_dump(val, indent + 1))
            lines.append(f"{pad}}}")
        else:
            lines.append(f'{pad}{key_q}\t\t"{_escape(str(val))}"')
    return "\n".join(lines)
