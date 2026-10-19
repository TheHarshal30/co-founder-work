"""A small C lexer shared by the similarity, readability and pattern analyzers.

It is not a full C front end (no preprocessing, no parsing); it only needs to
split source into tokens reliably enough to (a) normalise code for plagiarism
fingerprints and (b) measure surface readability facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

C_KEYWORDS = frozenset(
    """auto break case char const continue default do double else enum extern float
    for goto if inline int long register restrict return short signed sizeof static
    struct switch typedef union unsigned void volatile while _Bool _Complex _Imaginary
    _Alignas _Alignof _Atomic _Noreturn _Static_assert _Thread_local bool""".split()
)

# Library names that say nothing about who wrote the code.
COMMON_LIBRARY = frozenset(
    """printf scanf fprintf fscanf sprintf snprintf sscanf puts putchar getchar fgets
    gets fputs fgetc getc putc malloc calloc realloc free memset memcpy memmove strlen
    strcpy strncpy strcmp strncmp strcat qsort abs labs llabs main stdin stdout stderr
    NULL EOF size_t INT_MAX INT_MIN LLONG_MAX LLONG_MIN LONG_MAX LONG_MIN exit""".split()
)

_TOKEN_RE = re.compile(
    r"""
    (?P<nl>\n)
  | (?P<ws>[ \t\r\f\v]+)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:\\.|[^"\\\n])*")
  | (?P<chr>'(?:\\.|[^'\\\n])*')
  | (?P<num>(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)[uUlLfF]*)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<op>->|\+\+|--|<<=|>>=|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<|>>|\.\.\.|\S)
    """,
    re.S | re.X,
)


@dataclass(frozen=True)
class Token:
    kind: str  # pp | comment | str | chr | num | id | op
    text: str
    line: int


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    line = 1
    pos = 0
    at_line_start = True
    n = len(source)
    while pos < n:
        if at_line_start and source[pos] == "#":
            # preprocessor directive, including backslash continuations
            end = pos
            while True:
                nl = source.find("\n", end)
                if nl == -1:
                    end = n
                    break
                if nl > 0 and source[nl - 1] == "\\":
                    end = nl + 1
                    continue
                end = nl
                break
            text = source[pos:end]
            tokens.append(Token("pp", text, line))
            line += text.count("\n")
            pos = end
            continue
        m = _TOKEN_RE.match(source, pos)
        if not m:  # pragma: no cover - the op branch matches any non-space char
            pos += 1
            continue
        kind = m.lastgroup or "op"
        text = m.group(0)
        if kind == "nl":
            line += 1
            at_line_start = True
        elif kind == "ws":
            pass
        else:
            tokens.append(Token(kind, text, line))
            line += text.count("\n")
            at_line_start = False
        pos = m.end()
    return tokens


def strip_comments(source: str) -> str:
    """Source with comments blanked out (line numbers preserved)."""
    out = []
    last = 0
    for m in _TOKEN_RE.finditer(source):
        if m.lastgroup == "comment":
            out.append(source[last:m.start()])
            out.append("\n" * m.group(0).count("\n") or " ")
            last = m.end()
    out.append(source[last:])
    return "".join(out)


def normalized(tokens: List[Token]) -> List[Token]:
    """Tokens with names and literals erased, the classic MOSS/JPlag normalisation:
    renaming variables or changing constants does not change the result."""
    out = []
    for t in tokens:
        if t.kind in ("pp", "comment"):
            continue
        if t.kind == "id":
            text = t.text if t.text in C_KEYWORDS else "I"
        elif t.kind == "num":
            text = "N"
        elif t.kind == "str":
            text = "S"
        elif t.kind == "chr":
            text = "C"
        else:
            text = t.text
        out.append(Token(t.kind, text, t.line))
    return out


def readability_facts(source: str) -> Dict[str, object]:
    """Deterministic surface measurements, shown as evidence and given to the LLM
    so its judgement is anchored to facts rather than impressions."""
    tokens = tokenize(source)
    lines = source.splitlines()
    code_lines = {t.line for t in tokens if t.kind not in ("comment",)}
    comment_tokens = [t for t in tokens if t.kind == "comment"]
    comment_lines = set()
    for t in comment_tokens:
        comment_lines.update(range(t.line, t.line + t.text.count("\n") + 1))
    names = sorted({t.text for t in tokens if t.kind == "id" and t.text not in C_KEYWORDS
                    and t.text not in COMMON_LIBRARY})
    short = [x for x in names if len(x) == 1]
    longest_line = max((len(l.expandtabs(4)) for l in lines), default=0)
    indent_styles = {("tab" if l.startswith("\t") else "space") for l in lines if l[:1] in (" ", "\t")}
    return {
        "lines": len(lines),
        "code_lines": len(code_lines),
        "comment_lines": len(comment_lines),
        "comment_ratio": round(len(comment_lines) / max(1, len(code_lines)), 2),
        "identifiers": names,
        "single_letter_identifiers": short,
        "avg_identifier_length": round(sum(map(len, names)) / max(1, len(names)), 2),
        "longest_line": longest_line,
        "lines_over_100_chars": sum(1 for l in lines if len(l.expandtabs(4)) > 100),
        "mixed_tabs_and_spaces": len(indent_styles) > 1,
    }
