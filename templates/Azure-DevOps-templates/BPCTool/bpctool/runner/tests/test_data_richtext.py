from __future__ import annotations

from app.catalog_import import _looks_like_html_marker
from app.richtext import looks_like_html, markdown_to_html, normalize_rich_text


def test_angle_bracket_placeholders_are_plain_text() -> None:
    for text in (
        "<Customer> reviews the invoice",
        "<Vendor name> sends <document>",
        "<a customer> approves",
        "Step 1 <Approver>",
    ):
        assert not looks_like_html(text), text
        assert not _looks_like_html_marker(text), text
    raw = "<Customer> reviews the invoice\n* Check totals"
    assert normalize_rich_text(raw, field_type="html", convert_markdown_to_html=True) == (
        "<div><p>&lt;Customer&gt; reviews the invoice</p><ul><li>Check totals</li></ul></div>"
    )


def test_real_html_tags_are_detected() -> None:
    for text in (
        "<b>Bold</b> text",
        "Line one<br/>line two",
        "Line one<br />line two",
        '<a href="https://example.com">x</a>',
        "<div>block</div>",
        "<p class='x'>para</p>",
        "Text then </li>",
        "<DIV>upper</DIV>",
    ):
        assert looks_like_html(text), text
        assert _looks_like_html_marker(text), text


def test_numbered_list_keeps_number_across_single_blank_line() -> None:
    assert markdown_to_html("1. a\n\n2. b") == "<ol><li>a</li><li>b</li></ol>"


def test_two_blank_lines_end_the_list() -> None:
    assert markdown_to_html("1. a\n\n\n2. b") == '<div><ol><li>a</li></ol><ol start="2"><li>b</li></ol></div>'


def test_numbered_list_starting_after_one_keeps_its_start() -> None:
    assert markdown_to_html("3. c\n4. d") == '<ol start="3"><li>c</li><li>d</li></ol>'


def test_indented_items_nest_inside_the_parent_item() -> None:
    html = markdown_to_html("* Parent\n  * Child 1\n  * Child 2\n* Next\n    1. Deep")
    assert html == (
        "<ul><li>Parent<ul><li>Child 1</li><li>Child 2</li></ul></li>"
        "<li>Next<ol><li>Deep</li></ol></li></ul>"
    )


def test_list_kind_change_and_paragraphs_still_split_blocks() -> None:
    html = markdown_to_html("Intro\n* a\n1. b\nOutro")
    assert html == "<div><p>Intro</p><ul><li>a</li></ul><ol><li>b</li></ol><p>Outro</p></div>"


def test_list_content_is_escaped() -> None:
    html = markdown_to_html('* <script>alert(1)</script>\n  2. "x" & y')
    assert "<script>" not in html
    assert html == (
        '<ul><li>&lt;script&gt;alert(1)&lt;/script&gt;<ol start="2"><li>"x" &amp; y</li></ol></li></ul>'
    )
