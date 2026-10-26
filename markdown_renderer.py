"""Markdown / code / plain-text rendering for RTL Viewer.

The renderer converts clipboard text into a single HTML string that is
consumed by :class:`PySide6.QtWidgets.QTextBrowser`. Because Qt's rich-text
engine understands only a subset of HTML/CSS and knows nothing about CSS
classes, every visual property is emitted as an *inline* style and syntax
highlighting is done with Pygments' ``noclasses`` mode (inline colours).

Direction handling -- the core feature of this app:

* Every text block (paragraph, heading, list item, quote, table cell) gets
  ``dir="rtl"`` + ``text-align:right`` when its content starts with a strong
  RTL (Persian/Arabic/Hebrew) character, otherwise ``dir="ltr"``.
* Code blocks are *always* ``dir="ltr"`` + ``text-align:left`` -- they are
  never right-aligned, no matter what surrounds them.

The module has no Qt dependency, which keeps it easy to unit-test.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional, Sequence

from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments.formatter import Formatter
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.style import Style
from pygments.token import (
    Comment,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    String,
    Token,
)
from pygments import highlight as pygments_highlight

# --------------------------------------------------------------------------- #
# palette (GitHub Dark)
# --------------------------------------------------------------------------- #
FG = "#e6edf3"
MUTED = "#8b949e"
LINK = "#58a6ff"
PANEL = "#161b22"
CANVAS = "#0d1117"
BORDER = "#30363d"
CODE_BG = "#161b22"
CODE_INLINE_BG = "#343941"
TABLE_HEADER_BG = "#21262d"
TABLE_ZEBRA = (CANVAS, PANEL)  # visible alternation, like GitHub Dark

#: Base font size in pixels (before the user zoom factor is applied).
BASE_FONT_PX = 17.0
CODE_FONT_PX = 14.0

#: Text longer than this is rendered through the fast plain-text path.
MAX_RENDER_CHARS = 1_000_000

_RTL_RE = re.compile(
    "[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF"
    "\uFE70-\uFEFF\u0590-\u05FF\u200F]"
)

# Block-level token types that receive direction + typography styles.
_BLOCK_OPEN_TYPES = frozenset(
    {
        "paragraph_open",
        "heading_open",
        "blockquote_open",
        "bullet_list_open",
        "ordered_list_open",
        "list_item_open",
    }
)

# Block types that terminate the search for the first inline token.
_BLOCK_END_TYPES = frozenset(
    {"paragraph_close", "heading_close", "blockquote_close", "list_item_close",
     "fence", "code_block", "html_block", "hr", "table_close"}
)


class ContentKind(Enum):
    """What kind of payload the clipboard holds."""

    MARKDOWN = auto()
    CODE = auto()
    PLAIN = auto()


@dataclass(frozen=True)
class FontFamilies:
    """Resolved font family names injected by the Qt layer."""

    code: str = "Consolas"


def is_rtl(text: str) -> bool:
    """Return True when *text* contains any strong RTL character."""

    return bool(_RTL_RE.search(text))


# --------------------------------------------------------------------------- #
# RTL arrow mirroring
# --------------------------------------------------------------------------- #
# The Unicode bidi algorithm mirrors brackets but never arrows, so a literal
# "→" inside Persian text ends up pointing at the *source* word. Inside an
# RTL block *every* arrow glyph is swapped (→/← and ⇒/⇐) uniformly —
# mirroring only the arrows between Persian words leaves English runs and
# inline code pointing the other way, which reads as inconsistent. Fenced
# code blocks and fully-LTR blocks keep their arrows untouched.
_ARROW_TABLE = str.maketrans("→←⇒⇐", "←→⇐⇒")
_ARROW_RE = re.compile("[→←⇒⇐]")


def mirror_arrows(text: str, rtl_context: bool) -> str:
    """Swap →/← (and ⇒/⇐) in *text* when it lives in a right-to-left block."""

    if rtl_context and _ARROW_RE.search(text):
        return text.translate(_ARROW_TABLE)
    return text


# --------------------------------------------------------------------------- #
# content-type detection
# --------------------------------------------------------------------------- #
_MD_PATTERNS: Sequence[tuple] = (
    (re.compile(r"^```|~~~", re.M), 3, 3),          # fenced code
    (re.compile(r"^#{1,6}\s+\S", re.M), 2, 6),      # ATX headings
    (re.compile(r"\*\*[^*\n]+?\*\*"), 3, 1),        # **bold** — strong signal,
     # capped at one hit so ``2**10 % 3**2`` arithmetic cannot pile up
    (re.compile(r"__[^_\n]+?__"), 1, 2),            # __bold__ — low weight:
     # dunder identifiers like ``__main__`` in code look identical
    (re.compile(r"`[^`\n]+?`"), 3, 3),              # `inline code`
    (re.compile(r"^>\s+\S", re.M), 2, 4),           # blockquote
    (re.compile(r"\[[^\]\n]+\]\([^)\n]+\)"), 2, 4), # links
    (re.compile(r"!\[[^\]\n]*\]\([^)\n]+\)"), 2, 2),  # images
    (re.compile(r"^\s*\|?[-: |]+\|[-: |]+$", re.M), 4, 4),  # table separator
    (re.compile(r"^\s*[-*+]\s+\S", re.M), 1, 3),    # bullet list
    (re.compile(r"^\s*\d+\.\s+\S", re.M), 1, 3),    # ordered list
    (re.compile(r"^(={3,}|-{3,})$", re.M | re.IGNORECASE), 1, 1),  # setext
)

_CODE_PATTERNS: Sequence[tuple] = (
    (
        re.compile(
            r"^\s*(def |class |import |from \S+ import|public |private |"
            r"protected |function |const |let |var |return\b|if\s*\(|"
            r"for\s*\(|while\s*\(|switch\s*\(|case |elif |print\(|"
            r"console\.|func |fn |package |#!|using |struct |impl |#include)",
            re.M,
        ),
        2,
        8,
    ),
    (
        re.compile(
            r"^\s*(if|else|elif|for|while|try|except|finally|with|switch|"
            r"case|return|break|continue|class|def|import|from)\b",
            re.M,
        ),
        1,
        3,
    ),
    (re.compile(r"[;{}]\s*$", re.M), 1, 6),
    (re.compile(r"^ {4,}\S", re.M), 1, 4),
    (re.compile(r":\s*$", re.M), 1, 3),             # python/yaml style colon
    (re.compile(r"^\s*[\w.\[\]\"']+\s*=\s", re.M), 1, 3),  # assignments
    (re.compile(r"->|=>|::|!==|===|==|!=|<=|>=|&&|\|\||\+=|\*\*"), 1, 5),
)

_XML_RE = re.compile(r"^\s*(<\?xml|<!DOCTYPE\s+\w+|<[a-zA-Z][^>]*>)", re.M)


def _normalize_newlines(text: str) -> str:
    """Windows clipboard text arrives with ``\\r\\n``; unify to ``\\n``.

    The detection regexes anchor with ``$``/``^`` and markdown-it expects
    LF, so a stray carriage return silently breaks table detection.
    """

    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_kind(text: str) -> ContentKind:
    """Classify *text* as markdown / code / plain text heuristically."""

    sample = _normalize_newlines(text)[:200_000]
    if _MD_PATTERNS[0][0].search(sample):
        return ContentKind.MARKDOWN

    md_score = sum(
        min(len(p.findall(sample)), cap) * weight
        for p, weight, cap in _MD_PATTERNS[1:]
    )

    if not is_rtl(sample):
        if _XML_RE.match(sample) or _looks_like_json(sample):
            return ContentKind.CODE
        code_score = sum(
            min(len(p.findall(sample)), cap) * weight
            for p, weight, cap in _CODE_PATTERNS
        )
        if code_score >= 4:
            # Text that looks like source code needs stronger markdown
            # evidence: ``**`` pairs are valid arithmetic (``2**10``) and
            # dunders (``__main__``) mimic underscore bold.
            return ContentKind.MARKDOWN if md_score >= 6 else ContentKind.CODE
    if md_score >= 3:
        return ContentKind.MARKDOWN
    return ContentKind.PLAIN


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped[:1] in "{[":
        return False
    try:
        return isinstance(json.loads(stripped), (dict, list))
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# pygments: GitHub-Dark style
# --------------------------------------------------------------------------- #
class GitHubDarkStyle(Style):
    """Pygments colour scheme matching GitHub's dark code blocks."""

    background_color = CODE_BG
    default_style = ""

    styles = {
        Token: f"noinherit {FG}",
        Comment: f"italic {MUTED}",
        Comment.Preproc: "noinherit #8b949e bold",
        Keyword: "noinherit #ff7b72",
        Keyword.Constant: "noinherit #79c0ff",
        Keyword.Namespace: "noinherit #ff7b72",
        Operator: "noinherit #ff7b72",
        Operator.Word: "noinherit #ff7b72",
        Name.Builtin: "noinherit #79c0ff",
        Name.Function: "noinherit #d2a8ff",
        Name.Class: "noinherit #ffa657",
        Name.Namespace: "noinherit #79c0ff",
        Name.Exception: "noinherit #ffa657",
        Name.Tag: "noinherit #7ee787",
        Name.Attribute: "noinherit #79c0ff",
        Name.Decorator: "noinherit #d2a8ff",
        String: "noinherit #a5d6ff",
        String.Interpol: "noinherit #a5d6ff",
        String.Escape: "noinherit #79c0ff",
        Number: "noinherit #79c0ff",
        Generic.Deleted: "noinherit #ffa198",
        Generic.Inserted: "noinherit #7ee787",
        Generic.Emph: "italic",
        Generic.Strong: "bold",
        Generic.Subheading: "noinherit #1f6feb bold",
        Generic.Error: "noinherit #f85149",
    }


# --------------------------------------------------------------------------- #
# block styles
# --------------------------------------------------------------------------- #
def _px(em: float, scale: float) -> int:
    return max(1, round(em * scale))


@dataclass(frozen=True)
class _BlockSpec:
    size_em: float
    bold: bool = False
    color: str = FG
    top: int = 0
    bottom: int = 10
    sides: int = 0


_SPECS = {
    "p": _BlockSpec(1.0),
    "blockquote": _BlockSpec(1.0, color=MUTED, bottom=10, sides=12),
    "h1": _BlockSpec(1.75, bold=True, top=26, bottom=14),
    "h2": _BlockSpec(1.5, bold=True, top=22, bottom=12),
    "h3": _BlockSpec(1.28, bold=True, top=20, bottom=10),
    "h4": _BlockSpec(1.07, bold=True, top=16, bottom=8),
    "h5": _BlockSpec(0.93, bold=True, top=16, bottom=8),
    "h6": _BlockSpec(0.87, bold=True, color=MUTED, top=16, bottom=8),
    "ul": _BlockSpec(1.0),
    "ol": _BlockSpec(1.0),
    "li": _BlockSpec(1.0, bottom=3),
    "table": _BlockSpec(1.0),
    "td": _BlockSpec(1.0, bottom=0),
    "th": _BlockSpec(1.0, bold=True, bottom=0),
    "hr": _BlockSpec(1.0),
}


def _merge_style(token, extra: str) -> None:
    """Append *extra* to the token's existing inline style attribute."""

    current = token.attrGet("style") or ""
    token.attrSet("style", (current + ";" if current else "") + extra)


def _first_inline(tokens: List, start: int) -> Optional:
    """Find the first inline token belonging to the block opened at *start*."""

    for tok in tokens[start + 1 : start + 60]:
        if tok.type == "inline":
            return tok
        if tok.type in _BLOCK_END_TYPES or tok.type in _BLOCK_OPEN_TYPES and tok.level < tokens[start].level:
            return None
    return None


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
class Renderer:
    """Turns clipboard text into Qt-friendly HTML.

    Parameters
    ----------
    fonts:
        Resolved font family names (``code`` is used for code blocks, the
        Qt application font handles the prose family).
    """

    def __init__(self, fonts: Optional[FontFamilies] = None) -> None:
        self.fonts = fonts or FontFamilies()
        self._md = MarkdownIt("gfm-like", {"highlight": self._highlight})
        self._md.use(tasklists_plugin)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def render(self, text: str, font_scale: float = 1.0) -> str:
        """Render *text* to HTML appropriate for :class:`QTextBrowser`."""

        text = _normalize_newlines(text)
        if not text.strip():
            return ""
        if len(text) > MAX_RENDER_CHARS:
            return self._render_plain(text, font_scale)

        kind = detect_kind(text)
        if kind is ContentKind.MARKDOWN:
            return self._render_markdown(text, font_scale)
        if kind is ContentKind.CODE:
            return self._render_code(text, font_scale)
        return self._render_plain(text, font_scale)

    # ------------------------------------------------------------------ #
    # markdown
    # ------------------------------------------------------------------ #
    def _render_markdown(self, text: str, scale: float) -> str:
        tokens = self._md.parse(text)
        self._annotate(tokens, scale)
        body = self._md.renderer.render(tokens, self._md.options, {})
        base = _px(BASE_FONT_PX, scale)
        return (
            f'<div style="color:{FG}; font-size:{base}px;">'
            f"{body}</div>"
        )

    def _annotate(self, tokens: List, scale: float) -> None:
        """Inject inline styles, direction attributes and table zebraing.

        Note on alignment: Qt's rich-text engine mis-renders the combination
        of ``dir="rtl"`` with a CSS ``text-align`` on the same element (the
        text ends up flush left). Directional alignment is therefore emitted
        through the HTML ``align`` attribute, which Qt honours correctly.
        """

        in_header = False
        row_odd = False
        for i, tok in enumerate(tokens):
            ttype = tok.type
            if ttype in _BLOCK_OPEN_TYPES:
                self._style_block(tokens, i, scale)
            elif ttype in ("td_open", "th_open"):
                direction = self._direction_at(tokens, i)
                bg = TABLE_HEADER_BG if in_header else TABLE_ZEBRA[row_odd]
                spec = _SPECS["th" if ttype == "th_open" else "td"]
                style = self._block_css(spec, scale) + (
                    f";background-color:{bg};padding:5px 10px"
                )
                align = self._column_align(tok, direction)
                tok.attrSet("style", style)
                tok.attrSet("dir", direction)
                tok.attrSet("align", align)
            elif ttype == "table_open":
                direction = self._direction_at(tokens, i)
                base = _px(BASE_FONT_PX, scale)
                # Cell grid + outline (Qt honours CSS border-color on tables)
                # plus visible zebra rows make tables clearly readable.
                _merge_style(
                    tok,
                    f"color:{FG};font-size:{base}px;"
                    f"margin:12px 0px 14px 0px;border-color:{BORDER}",
                )
                tok.attrSet("dir", direction)
                tok.attrSet("align", "right" if direction == "rtl" else "left")
                tok.attrSet("cellpadding", "4")
                tok.attrSet("cellspacing", "0")
                tok.attrSet("border", "1")
            elif ttype == "thead_open":
                in_header = True
            elif ttype == "thead_close":
                in_header = False
            elif ttype == "tbody_open":
                row_odd = False
            elif ttype == "tr_open":
                row_odd = not row_odd
            elif ttype == "hr":
                _merge_style(
                    tok, f"color:{BORDER};background-color:{BORDER};border:none"
                )
            elif ttype == "code_block":
                # Indented code blocks: route them through the fenced-code
                # highlighter so they get the exact same styling.
                tok.type = "fence"
                tok.tag = "code"
                tok.info = ""
                tok.markup = "```"
            elif ttype == "inline":
                self._annotate_inline(tok.children, scale, is_rtl(tok.content))

    def _annotate_inline(
        self, children: Optional[List], scale: float, rtl_context: bool = False
    ) -> None:
        if not children:
            return
        code_px = _px(CODE_FONT_PX, scale)
        for child in children:
            ctype = child.type
            if ctype in ("text", "code_inline"):
                # Uniform mirroring inside RTL blocks — see mirror_arrows.
                child.content = mirror_arrows(child.content, rtl_context)
            if ctype == "code_inline":
                _merge_style(
                    child,
                    f"font-family:'{self.fonts.code}';font-size:{code_px}px;"
                    f"color:#f0f6fc;background-color:{CODE_INLINE_BG}",
                )
            elif ctype == "link_open":
                _merge_style(child, f"color:{LINK}")
            elif ctype in ("del_open", "del_close"):
                child.tag = "s"  # Qt understands <s>, not <del>
            elif ctype == "html_inline" and "<input" in child.content:
                checked = "checked" in child.content
                glyph = "\u2611" if checked else "\u2610"  # ☑ / ☐
                child.content = (
                    f'<span style="color:{LINK};font-size:'
                    f'{_px(BASE_FONT_PX, scale)}px">{glyph}</span>&nbsp;'
                )

    def _style_block(self, tokens: List, i: int, scale: float) -> None:
        tok = tokens[i]
        direction = self._direction_at(tokens, i)
        spec = _SPECS.get(tok.tag, _SPECS["p"])
        _merge_style(tok, self._block_css(spec, scale))
        tok.attrSet("dir", direction)
        tok.attrSet("align", "right" if direction == "rtl" else "left")

    @staticmethod
    def _column_align(tok, direction: str) -> str:
        """Turn markdown-it's CSS column alignment into an ``align`` attribute."""

        style = tok.attrGet("style") or ""
        match = re.search(r"text-align:\s*(left|right|center)", style)
        return match.group(1) if match else (
            "right" if direction == "rtl" else "left"
        )

    @staticmethod
    def _direction_at(tokens: List, i: int) -> str:
        inline = _first_inline(tokens, i)
        if inline is None:
            return "ltr"
        return "rtl" if is_rtl(inline.content) else "ltr"

    @staticmethod
    def _block_css(spec: _BlockSpec, scale: float) -> str:
        size = _px(BASE_FONT_PX * spec.size_em, scale)
        css = (
            f"color:{spec.color};font-size:{size}px;"
            f"margin:{spec.top}px {spec.sides}px {spec.bottom}px {spec.sides}px"
        )
        if spec.bold:
            css += ";font-weight:bold"
        return css

    # ------------------------------------------------------------------ #
    # code
    # ------------------------------------------------------------------ #
    def _pre_style(self, scale: float) -> str:
        code_px = _px(CODE_FONT_PX, scale)
        return (
            f"font-family:'{self.fonts.code}';font-size:{code_px}px;"
            f"color:{FG};background-color:{CODE_BG};"
            f"margin:10px 4px;white-space:pre"
        )

    def _highlight(self, code: str, lang: str, attrs: str) -> str:
        """markdown-it fence highlighter returning a fully styled ``<pre>``."""

        return f'<pre dir="ltr" style="{self._pre_style(1.0)}">' \
            f"{self._pygmentize(code, lang)}</pre>"

    def _pygmentize(self, code: str, lang: str) -> str:
        try:
            if lang:
                lexer = get_lexer_by_name(lang.strip(), stripall=False)
            else:
                raise ValueError("no language")
        except Exception:
            try:
                lexer = guess_lexer(code[:100_000])
            except Exception:
                lexer = TextLexer()
        try:
            formatter: Formatter = HtmlFormatter(  # type: ignore[assignment]
                style=GitHubDarkStyle, noclasses=True, nowrap=True
            )
            return pygments_highlight(code, lexer, formatter)
        except Exception:
            return html.escape(code, quote=False)

    def _render_code(self, text: str, scale: float) -> str:
        stripped = text.rstrip("\n")
        return (
            f'<pre dir="ltr" style="{self._pre_style(scale)}">'
            f"{self._pygmentize(stripped, '')}</pre>"
        )

    # ------------------------------------------------------------------ #
    # plain text
    # ------------------------------------------------------------------ #
    def _render_plain(self, text: str, scale: float) -> str:
        """Render plain text as RTL paragraphs, one per line.

        Each line becomes its own ``<p dir="rtl" align="right">`` (the
        combination Qt actually renders right-aligned), with ``&nbsp;``
        keeping empty lines one row tall.
        """

        base = _px(BASE_FONT_PX, scale)
        paragraphs = []
        for line in _escape_plain_lines(text):
            line = mirror_arrows(line, is_rtl(line))
            paragraphs.append(
                f'<p dir="rtl" align="right" style="color:{FG};'
                f'font-size:{base}px;margin:0px">{line or "&nbsp;"}</p>'
            )
        return "".join(paragraphs)


def _escape_plain_lines(text: str) -> List[str]:
    """Escape HTML per line, preserving leading/multiple spaces."""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fixed: List[str] = []
    for line in lines:
        line = html.escape(line, quote=False)
        line = re.sub(r"^( +)", lambda m: "&nbsp;" * len(m.group()), line)
        line = re.sub(r" {2,}", lambda m: "&nbsp;" * (len(m.group()) - 1) + " ", line)
        fixed.append(line)
    return fixed
