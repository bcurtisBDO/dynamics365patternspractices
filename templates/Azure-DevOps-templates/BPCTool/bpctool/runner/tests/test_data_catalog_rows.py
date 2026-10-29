from __future__ import annotations

import pytest
from openpyxl import Workbook

from app import excel_ir
from app.catalog_import import _drafts_from_workbook, _external_key, _first_value
from app.excel_ir import build_ir, parse_catalog_sources
from app.mapping import TemplateMapping


def _catalog(path, header: list[str], rows: list[list]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Catalog"
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_catalog_statistics_match_headers_case_insensitively(tmp_path) -> None:
    _catalog(
        tmp_path / "Business Process Catalog TEST.xlsx",
        ["WORK ITEM TYPE", "title  1"],
        [["Process", "Order to cash"], ["Scenario", "Ship goods"]],
    )
    catalog = parse_catalog_sources(tmp_path)
    assert catalog["work_item_types"] == {"Process": 1, "Scenario": 1}
    assert catalog["files"][0]["sample_titles"] == ["Order to cash", "Ship goods"]


def test_whitespace_only_rows_are_not_drafts_and_counts_agree(tmp_path) -> None:
    path = tmp_path / "Business Process Catalog TEST.xlsx"
    _catalog(
        path,
        ["Work item type", "Title 1", "Process sequence ID"],
        [["Process", "Order to cash", "10"], ["  ", "\t", " "], [None, "", "   "], ["Scenario", "Ship", "11"]],
    )
    drafts = _drafts_from_workbook(path, TemplateMapping())
    assert [d.title for d in drafts] == ["Order to cash", "Ship"]
    assert parse_catalog_sources(tmp_path)["total_rows"] == len(drafts)


def test_blank_looking_cell_falls_back_to_next_column() -> None:
    lookup = {"process sequence id": "   ", "microsoft id": "MS-42"}
    assert _first_value(lookup, "process sequence id", "microsoft id") == "MS-42"
    assert _external_key(lookup, {"_source_file": "bpc.xlsx", "_source_row": 3}) == "MS-42"
    assert _first_value({"title": " \t "}, "title") is None


@pytest.mark.parametrize(
    "content",
    [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512, b"not a zip at all"],
    ids=["encrypted-ole", "corrupt"],
)
def test_unreadable_template_is_a_plain_value_error(tmp_path, content) -> None:
    path = tmp_path / "ADO template guideline TEST.xlsx"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="ADO template guideline TEST.xlsx") as info:
        build_ir(str(path))
    assert "encrypted" in str(info.value)


def test_unreadable_catalog_workbook_is_a_plain_value_error(tmp_path) -> None:
    (tmp_path / "Deliverables TEST.xlsx").write_bytes(b"PK\x03\x04 truncated")
    with pytest.raises(ValueError, match="Deliverables TEST.xlsx"):
        parse_catalog_sources(tmp_path)


def test_locked_workbook_is_a_plain_value_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "Objectives TEST.xlsx"
    _catalog(path, ["Work item type"], [["Objective"]])

    def locked(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(excel_ir, "load_workbook", locked)
    with pytest.raises(ValueError, match="Objectives TEST.xlsx.*permission denied"):
        excel_ir._sheet_rows(path, "Catalog")
    with pytest.raises(ValueError, match="permission denied"):
        parse_catalog_sources(tmp_path)
