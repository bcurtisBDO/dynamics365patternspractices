"""Normalize Excel cell content for Azure DevOps HTML fields.

Catalog sources mix:
- already-HTML fragments (e.g. Microsoft references with <div><a>...)
- markdown-like plain text (bullets with '*', numbered lists, blank-line paragraphs)

ADO HTML fields accept HTML; plain markdown-like text renders poorly. This module
detects HTML and converts markdown-like content to safe HTML while leaving real
HTML intact.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

_HTML_TAG_NAMES = r"(?:div|p|br|ul|ol|li|a|span|strong|em|b|i|h[1-6]|table|tr|td|th|pre|code|blockquote)"
# A real tag closes right after its name (<b>, <br/>, </div>) or carries an attribute
# (<a href=...>), so angle-bracket placeholders such as "<a customer>" stay plain text.
_HTML_TAG_RE = re.compile(
    rf"<(?:/{_HTML_TAG_NAMES}\s*>|{_HTML_TAG_NAMES}(?:\s*/?>|\s+[a-z_:][-\w:.]*\s*=))",
    re.IGNORECASE,
)
_HTML_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|nbsp|#\d+|#x[0-9a-f]+);", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\"'=])(https?://[^\s<>\"']+)")
_BULLET_RE = re.compile(r"^(\s*)([-*•]|\*)\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")


def looks_like_html(value: Any) -> bool:
    text = "" if value is None else str(value)
    if not text.strip():
        return False
    return bool(_HTML_TAG_RE.search(text[:4000]))


def _escape_keep_newlines(text: str) -> str:
    return html.escape(text, quote=False)


def _inline_markdown(text: str) -> str:
    """Escape then restore simple markdown links / bare URLs as anchors."""
    placeholders: list[str] = []

    def stash(fragment: str) -> str:
        placeholders.append(fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    def replace_md_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        href = html.escape(match.group(2), quote=True)
        return stash(f'<a href="{href}">{label}</a>')

    working = _MD_LINK_RE.sub(replace_md_link, text)

    def replace_bare(match: re.Match[str]) -> str:
        url = match.group(1).rstrip(".,);]")
        trailing = match.group(1)[len(url) :]
        href = html.escape(url, quote=True)
        return stash(f'<a href="{href}">{html.escape(url, quote=False)}</a>') + trailing

    working = _BARE_URL_RE.sub(replace_bare, working)
    escaped = _escape_keep_newlines(working)
    for index, fragment in enumerate(placeholders):
        escaped = escaped.replace(f"\x00{index}\x00", fragment)
    return escaped


def _list_indent(prefix: str) -> int:
    return len(prefix.replace("\t", "    ")) // 2


def markdown_to_html(value: Any) -> str:
    """Convert markdown-like plain text (catalog workbook style) to ADO HTML."""
    text = "" if value is None else str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip("\n")
    if not text.strip():
        return ""

    lines = text.split("\n")
    parts: list[str] = []
    paragraph: list[str] = []
    # Open lists, outermost first: (tag, indent). Every open list has an open <li>.
    stack: list[tuple[str, int]] = []
    list_html: list[str] = []
    blank_lines = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        body = "<br/>".join(_inline_markdown(line) for line in paragraph)
        parts.append(f"<p>{body}</p>")
        paragraph = []

    def close_lists(depth: int = 0) -> None:
        nonlocal list_html
        while len(stack) > depth:
            tag, _ = stack.pop()
            list_html.append(f"</li></{tag}>")
        if not stack and list_html:
            parts.append("".join(list_html))
            list_html = []

    def add_item(tag: str, indent: int, number: int | None, content: str) -> None:
        while stack and indent < stack[-1][1]:
            close_lists(len(stack) - 1)
        if stack and indent == stack[-1][1] and tag != stack[-1][0]:
            close_lists(len(stack) - 1)
        if stack and indent == stack[-1][1]:
            list_html.append(f"</li><li>{content}")
            return
        start = f' start="{number}"' if tag == "ol" and number is not None and number != 1 else ""
        stack.append((tag, indent))
        list_html.append(f"<{tag}{start}><li>{content}")

    for raw in lines:
        if not raw.strip():
            blank_lines += 1
            flush_paragraph()
            continue

        bullet = _BULLET_RE.match(raw)
        numbered = _NUMBERED_RE.match(raw)
        if bullet or numbered:
            if blank_lines > 1:
                close_lists()
            blank_lines = 0
            flush_paragraph()
            if bullet:
                add_item("ul", _list_indent(bullet.group(1)), None, _inline_markdown(bullet.group(3).strip()))
            else:
                add_item(
                    "ol",
                    _list_indent(numbered.group(1)),
                    int(numbered.group(2)),
                    _inline_markdown(numbered.group(3).strip()),
                )
            continue

        blank_lines = 0
        close_lists()
        paragraph.append(raw.rstrip())

    close_lists()
    flush_paragraph()

    if not parts:
        return f"<div>{_inline_markdown(text)}</div>"
    if len(parts) == 1:
        return parts[0]
    return "<div>" + "".join(parts) + "</div>"


def normalize_rich_text(
    value: Any,
    *,
    field_type: str = "html",
    convert_markdown_to_html: bool = False,
) -> str:
    """Normalize Excel content for ADO large-text / HTML fields.

    Behavior:
    - Existing HTML is always preserved.
    - Markdown-like text is preserved by default (caller should also set
      /multilineFieldsFormat/{field}=Markdown on create).
    - Optional convert_markdown_to_html converts markdown → HTML instead.
    """
    if value is None:
        return ""
    text = str(value)
    if not text.strip():
        return ""

    ft = (field_type or "").strip().lower()
    is_html_field = "html" in ft or ft in {"", "richtext", "rich text"}

    if looks_like_html(text):
        # Preserve authoring HTML from Excel (references, pasted content).
        return text.strip()

    if not is_html_field:
        return text.strip()

    if convert_markdown_to_html:
        return markdown_to_html(text)

    # Default: keep markdown/plain text as authored.
    return text.strip()


def json_cell_value(value: Any) -> Any:
    """Typed Excel cells (dates, times, decimals) as values ``json.dumps`` accepts.

    Excel stores a date as a datetime at midnight, so those become ``YYYY-MM-DD``.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None and value.time() == time(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value.is_finite() and value == value.to_integral_value() else float(value)
    return value


def normalize_field_value(
    value: Any,
    field_type: str,
    *,
    convert_markdown_to_html: bool = False,
) -> Any:
    """Coerce a catalog/template value for ADO, applying rich-text rules for HTML."""
    if value is None:
        return None
    value = json_cell_value(value)
    ft = (field_type or "").strip().lower()
    if "html" in ft:
        return normalize_rich_text(
            value,
            field_type=field_type,
            convert_markdown_to_html=convert_markdown_to_html,
        )
    if "boolean" in ft or ft == "bool":
        return str(value).strip().lower() in {"true", "yes", "y", "1", "x"}
    return value if not isinstance(value, str) else value.strip()
