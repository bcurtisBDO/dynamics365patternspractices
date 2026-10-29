"""Every generated CSV that lists workbook rows ends with the row's MS BPC catalog columns."""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook

from app import catalog_runtime
from app.ado_client import AdoError
from app.catalog_import import (
    CATALOG_ID_HEADERS,
    SkippedDraft,
    _drafts_from_workbook,
    catalog_id_values,
    catalog_ids_by_key,
    write_skipped_csv,
)
from app.catalog_match import ExistingMatches, Reuse, apply_to_id_map, write_matched_csv
from app.catalog_runtime import backfill_parent_links, import_drafts_parent_aware
from app.catalog_validate import PrevalidateIssue, PrevalidateResult, write_prevalidate_csv
from app.failure_explain import write_failures_csv
from app.mapping import TemplateMapping
from app.writers.catalog import _write_preview_csv

HEADERS = [
    "Work item type",
    "Title 1",
    "Title 2",
    "Process sequence ID",
    "Microsoft ID MS BPC",
    "Catalog status MS BPC",
    "Mavim ID MS BPC",
    "Mavim reference MS BPC",
    "Partner ID MS BPC",
]
ROOT_IDS = {
    "Catalog status MS BPC": "40 - Published",
    "Microsoft ID MS BPC": "MS-10",
    "Mavim ID MS BPC": "MV-10",
    "Mavim reference MS BPC": "REF-10",
    "Partner ID MS BPC": "P-10",
}
CHILD_IDS = {
    "Catalog status MS BPC": "30 - Draft",
    "Microsoft ID MS BPC": "MS-11",
    "Mavim ID MS BPC": "MV-11",
    "Mavim reference MS BPC": "",
    "Partner ID MS BPC": "",
}


def _drafts(tmp_path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Catalog"
    ws.append(HEADERS)
    ws.append(["End to end", "Order to cash", None, "10", "MS-10", "40 - Published", "MV-10", "REF-10", "P-10"])
    ws.append(["Process area", None, "Ship goods", "11", "MS-11", "30 - Draft", "MV-11", None, None])
    path = tmp_path / "Business Process Catalog TEST.xlsx"
    wb.save(path)
    return _drafts_from_workbook(path, TemplateMapping())


def _read(path: Path, encoding: str = "utf-8-sig") -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def _ids(row: dict[str, str]) -> dict[str, str]:
    return {header: row[header] for header in CATALOG_ID_HEADERS}


def _ends_with_catalog_columns(header: list[str], *, in_place: tuple[str, ...] = ()) -> bool:
    """The catalog columns not already in the file come last; each field appears once."""
    added = [h for h in CATALOG_ID_HEADERS if h not in in_place]
    plain = {h.removesuffix(" MS BPC").casefold() for h in CATALOG_ID_HEADERS}
    return (
        header[-len(added):] == added
        and all(header.count(h) == 1 for h in CATALOG_ID_HEADERS)
        and not plain & {h.casefold() for h in header}
    )


def test_drafts_keep_the_workbook_values(tmp_path: Path) -> None:
    root, child = _drafts(tmp_path)
    assert CATALOG_ID_HEADERS == [
        "Catalog status MS BPC", "Microsoft ID MS BPC", "Mavim ID MS BPC", "Mavim reference MS BPC", "Partner ID MS BPC",
    ]
    assert catalog_id_values(root) == ROOT_IDS
    assert catalog_id_values(child) == CHILD_IDS
    assert catalog_id_values(None) == dict.fromkeys(CATALOG_ID_HEADERS, "")


def test_report_csvs_end_with_the_catalog_columns(tmp_path: Path) -> None:
    root, child = _drafts(tmp_path)
    ids = catalog_ids_by_key([root, child])

    _write_preview_csv([root, child], tmp_path / "import-preview.csv")
    header, rows = _read(tmp_path / "import-preview.csv")
    assert _ends_with_catalog_columns(header)
    assert [_ids(r) for r in rows] == [ROOT_IDS, CHILD_IDS]

    write_skipped_csv([SkippedDraft(child, "Parent row 10 is skipped")], tmp_path / "skipped.csv")
    header, rows = _read(tmp_path / "skipped.csv")
    assert header[3] == "Catalog status MS BPC"
    assert _ends_with_catalog_columns(header, in_place=("Catalog status MS BPC",))
    assert [_ids(r) for r in rows] == [CHILD_IDS]

    issue = PrevalidateIssue(child.key, child.work_item_type, "System.State", "x", "invalid_state", "report")
    write_prevalidate_csv(PrevalidateResult(issues=[issue]), tmp_path / "prevalidate.csv", catalog_ids=ids)
    header, rows = _read(tmp_path / "prevalidate.csv")
    assert _ends_with_catalog_columns(header) and [_ids(r) for r in rows] == [CHILD_IDS]

    failures = [
        {"key": root.key, "category": "other", "error": "boom"},
        {"key": "not-in-workbook", "category": "other", "error": "boom"},
    ]
    write_failures_csv(failures, tmp_path / "failures.csv", catalog_ids=ids)
    header, rows = _read(tmp_path / "failures.csv")
    assert _ends_with_catalog_columns(header)
    assert [_ids(r) for r in rows] == [ROOT_IDS, dict.fromkeys(CATALOG_ID_HEADERS, "")]

    write_matched_csv(ExistingMatches(reused=[Reuse(child, 42, ["old"])]), tmp_path / "matched.csv")
    header, rows = _read(tmp_path / "matched.csv")
    assert header[5] == "Microsoft ID MS BPC"
    assert _ends_with_catalog_columns(header, in_place=("Microsoft ID MS BPC",))
    assert [_ids(r) for r in rows] == [CHILD_IDS]


class _Client:
    def __init__(self) -> None:
        self.next_id = 100

    def create_work_item(self, project, wit, fields, **kwargs):
        if "System.State" in fields:
            raise AdoError(
                "ADO 400 Bad Request: The field 'System.State' contains the value 'New' "
                "that is not in the list of supported values",
                status=400,
            )
        self.next_id += 1
        return {"id": self.next_id}

    def update_work_items_batch(self, updates):
        return [None for _ in updates]

    def get_work_items_batch(self, project, ids, *, expand_relations=False):
        return [{"id": i, "relations": []} for i in ids]

    def add_parent_link(self, project, child_id, parent_id, **kwargs):
        return {}


def test_id_map_state_log_and_backfill_report_carry_the_catalog_columns(tmp_path: Path) -> None:
    root, child = _drafts(tmp_path)
    child.fields["System.State"] = "New"
    id_map_path = tmp_path / "ado-id-map.csv"
    client = _Client()
    id_map, failures, _ = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        [root, child],
        project="Demo",
        id_map={},
        id_map_path=id_map_path,
        parallel_workers=1,
        emit=lambda *_: None,
    )
    assert failures == []
    header, rows = _read(id_map_path, encoding="utf-8")
    assert _ends_with_catalog_columns(header)
    assert {r["Key"]: _ids(r) for r in rows} == {"10": ROOT_IDS, "11": CHILD_IDS}

    header, rows = _read(tmp_path / catalog_runtime.PENDING_STATES_CSV, encoding="utf-8")
    assert _ends_with_catalog_columns(header)
    assert [(r["Status"], _ids(r)) for r in rows] == [("pending", CHILD_IDS), ("done", CHILD_IDS)]

    # Unlinked child: backfill links it, reports it and records the link in the map.
    backfill_parent_links(
        client,  # type: ignore[arg-type]
        [root, child],
        id_map,
        project="Demo",
        output_dir=tmp_path,
        emit=lambda *_: None,
        id_map_path=id_map_path,
    )
    header, rows = _read(tmp_path / "backfill-links-report.csv")
    assert _ends_with_catalog_columns(header) and [_ids(r) for r in rows] == [CHILD_IDS]
    _, rows = _read(id_map_path, encoding="utf-8")
    assert rows[-1]["Key"] == "11" and _ids(rows[-1]) == CHILD_IDS


def test_id_map_rewrite_keeps_the_columns_and_an_older_map_still_appends(tmp_path: Path) -> None:
    root, child = _drafts(tmp_path)
    old = tmp_path / "ado-id-map.csv"
    old.write_text("Key,WorkItemId,WorkItemType,Title,ParentKey,ParentId\nold,7,Process area,Ship goods,,\n")

    handle, writer = catalog_runtime.open_id_map_for_append(old)
    with handle:
        catalog_runtime.append_id_map_row(writer, handle, root, 8, None)
    header, rows = _read(old, encoding="utf-8")
    assert "Microsoft ID MS BPC" not in header and [r["Key"] for r in rows] == ["old", "10"]

    apply_to_id_map(ExistingMatches(reused=[Reuse(child, 7, ["old"])]), {"old": 7, "10": 8}, old)
    header, rows = _read(old, encoding="utf-8")
    assert _ends_with_catalog_columns(header)
    assert {r["Key"]: _ids(r) for r in rows} == {"10": dict.fromkeys(CATALOG_ID_HEADERS, ""), "11": CHILD_IDS}
