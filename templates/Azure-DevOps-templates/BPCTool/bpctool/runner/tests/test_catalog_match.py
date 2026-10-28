"""Rows already in Azure DevOps under another key are reused, not created again."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.ado_client import AdoError
from app.catalog_import import WorkItemDraft
from app.catalog_match import (
    MATCHED_CSV,
    apply_to_id_map,
    find_existing_by_microsoft_id,
    write_matched_csv,
)
from app.catalog_runtime import ID_MAP_FIELDS, import_drafts_parent_aware, load_id_map
from app.failure_explain import LOOKUP_FAILED_PREFIX, enrich_failures
from app.mapping import TemplateMapping
from app.writers.catalog import _write_change_report


def _draft(key: str, microsoft_id: str, *, parent: str | None = None, wit: str = "Process") -> WorkItemDraft:
    return WorkItemDraft(
        key=key,
        parent_key=parent,
        work_item_type=wit,
        title=f"Title {key}",
        fields={"System.Title": f"Title {key}", "MSBPC.microsoftid": microsoft_id, "MSBPC.processsequenceid": key},
        source_file="bpc.xlsx",
        source_row=3,
    )


class Reader:
    def __init__(self, items: dict[int, dict]) -> None:
        self.items = items
        self.queries: list[list[str]] = []

    def get_project_id(self, _name: str) -> str:
        return "guid"

    def has_work_item_with_field(self, _project: str, field_ref: str) -> bool:
        return any(fields.get(field_ref) for fields in self.items.values())

    def find_work_items_by_field_values(self, _project: str, field_ref: str, values: list[str]) -> list[int]:
        self.queries.append(list(values))
        wanted = {v.casefold() for v in values}
        return [i for i, f in self.items.items() if str(f.get(field_ref) or "").casefold() in wanted]

    def get_work_items_batch(self, _project: str, ids: list[int], **_kw) -> list[dict]:
        return [{"id": i, "fields": self.items[i]} for i in ids if i in self.items]


def _item(microsoft_id: str, wit: str = "Process", sequence: str = "") -> dict:
    return {"System.WorkItemType": wit, "System.Title": "In ADO", "MSBPC.microsoftid": microsoft_id,
            "MSBPC.processsequenceid": sequence}


def _find(reader, drafts, id_map, **kwargs):
    return find_existing_by_microsoft_id(reader, drafts, id_map, project="P", emit=lambda *_: None, **kwargs)


def _write_map(path: Path, rows: list[tuple[str, int]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ID_MAP_FIELDS)
        writer.writeheader()
        for key, wid in rows:
            writer.writerow({"Key": key, "WorkItemId": wid, "WorkItemType": "Process", "Title": key,
                             "ParentKey": "", "ParentId": "7"})
    return path


def test_renumbered_row_reuses_the_item_of_a_key_gone_from_the_workbook(tmp_path) -> None:
    reader = Reader({1: _item("M1"), 2: _item("M2")})
    drafts = [_draft("10.2", "M2"), _draft("10.9", "m1", parent="10.2")]
    id_map = {"10.2": 2, "10.1": 1}
    matches = _find(reader, drafts, id_map)
    assert [(m.draft.key, m.work_item_id, m.previous_keys) for m in matches.reused] == [("10.9", 1, ["10.1"])]
    assert not matches.released and not matches.several and matches.checked == 1

    path = _write_map(tmp_path / "ado-id-map.csv", [("10.2", 2), ("10.1", 1)])
    updated = apply_to_id_map(matches, id_map, path)
    assert updated == {"10.2": 2, "10.9": 1}
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [(r["Key"], r["WorkItemId"], r["ParentKey"], r["ParentId"]) for r in rows] == [
        ("10.2", "2", "", "7"),
        ("10.9", "1", "10.2", ""),
    ]
    assert load_id_map(path) == updated


def test_apply_keeps_keys_of_a_map_saved_by_excel_with_bom(tmp_path) -> None:
    path = _write_map(tmp_path / "ado-id-map.csv", [("10.2", 2), ("10.1", 1)])
    path.write_bytes(path.read_text(encoding="utf-8").encode("utf-8-sig"))
    id_map = load_id_map(path)
    assert id_map == {"10.2": 2, "10.1": 1}
    matches = _find(Reader({1: _item("M1"), 2: _item("M2")}), [_draft("10.2", "M2"), _draft("10.9", "M1")], id_map)
    assert apply_to_id_map(matches, id_map, path) == {"10.2": 2, "10.9": 1}
    assert load_id_map(path) == {"10.2": 2, "10.9": 1}


def test_key_given_a_new_microsoft_id_is_released_and_created(tmp_path) -> None:
    # The release moved M-old to key B and gave key A a new Microsoft ID.
    reader = Reader({1: _item("M-old")})
    drafts = [_draft("A", "M-new"), _draft("B", "M-old")]
    id_map = {"A": 1}
    matches = _find(reader, drafts, id_map)
    assert [(m.draft.key, m.work_item_id) for m in matches.reused] == [("B", 1)]
    assert [(d.key, wid) for d, wid in matches.released] == [("A", 1)]
    updated = apply_to_id_map(matches, id_map, _write_map(tmp_path / "ado-id-map.csv", [("A", 1)]))
    assert updated == {"B": 1}

    write_matched_csv(matches, tmp_path / MATCHED_CSV)
    actions = [r["Action"] for r in csv.DictReader((tmp_path / MATCHED_CSV).open(encoding="utf-8-sig"))]
    assert actions[0].startswith("Reused the existing work item") and "created as a new item" in actions[1]


def test_released_row_is_looked_up_again(tmp_path) -> None:
    # B takes A's item; A's new Microsoft ID is on C's item; C's new one is on nothing.
    reader = Reader({1: _item("M-a"), 2: _item("M-c")})
    drafts = [_draft("A", "M-c"), _draft("B", "M-a"), _draft("C", "M-new")]
    id_map = {"A": 1, "C": 2}
    matches = _find(reader, drafts, id_map)
    assert sorted((m.draft.key, m.work_item_id) for m in matches.reused) == [("A", 2), ("B", 1)]
    assert [(d.key, wid) for d, wid in matches.released] == [("C", 2)]
    path = _write_map(tmp_path / "ado-id-map.csv", [("A", 1), ("C", 2)])
    assert apply_to_id_map(matches, id_map, path) == {"A": 2, "B": 1}
    assert load_id_map(path) == {"A": 2, "B": 1}


def test_item_still_claimed_by_its_row_is_not_reused() -> None:
    reader = Reader({1: _item("M1")})
    # Same Microsoft ID on two rows: the mapped row keeps its item; the other is created as before.
    drafts = [_draft("A", "M1"), _draft("B", "M1")]
    assert not _find(reader, drafts, {"A": 1}).reused
    # A Deprecated / Deleted row still claims its item.
    assert not _find(reader, [_draft("B", "M1")], {"R": 1}, retired=[_draft("R", "M1")]).reused
    # A capped workbook can't tell whether a key outside it still exists.
    assert not _find(reader, [_draft("B", "M1")], {"OUTSIDE": 1}, complete_workbook=False).reused
    assert _find(reader, [_draft("B", "M1")], {"OUTSIDE": 1}).reused


def test_type_must_match_and_sequence_id_breaks_ties() -> None:
    reader = Reader({1: _item("M1", wit="Scenario"), 2: _item("M2", sequence="9"), 3: _item("M2", sequence="5")})
    assert not _find(reader, [_draft("A", "M1")], {}).reused
    matches = _find(reader, [_draft("5", "M2")], {})
    assert [(m.draft.key, m.work_item_id) for m in matches.reused] == [("5", 3)]


def test_project_without_microsoft_ids_skips_the_lookup() -> None:
    reader = Reader({})
    assert not _find(reader, [_draft("A", "M1")], {}).reused
    assert reader.queries == []
    assert _find(Reader({1: _item("M1")}), [_draft("A", "M1")], {"A": 1}).checked == 0


def test_several_matches_are_not_created_and_block_their_children(tmp_path) -> None:
    reader = Reader({1: _item("M1"), 2: _item("M1")})
    parent, child = _draft("A", "M1"), _draft("A.1", "M9", parent="A")
    matches = _find(reader, [parent, child], {})
    assert [(s.draft.key, s.work_item_ids) for s in matches.several] == [("A", [1, 2])]

    class NoCreate:
        def create_work_item(self, *_a, **_kw):
            raise AssertionError("must not create")

    _, failures, stats = import_drafts_parent_aware(
        NoCreate(), [parent, child], project="P", id_map={}, id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1, emit=lambda *_: None, hold=matches.held_keys,
    )
    assert stats["failed"] == 2 and stats["created"] == 0
    enrich_failures(failures, {"A": parent, "A.1": child})
    by_key = {f["key"]: f for f in failures}
    assert by_key["A"]["category"] == "already_exists" and "#1, #2" in by_key["A"]["problem"]
    assert by_key["A.1"]["category"] == "parent_failed" and by_key["A.1"]["blockedBy"] == "A"


def test_failed_microsoft_id_check_holds_unmapped_rows(tmp_path, monkeypatch) -> None:
    from app.writers import catalog as catalog_writer

    mapped, renumbered, child = _draft("1", "M1"), _draft("2", "M2"), _draft("2.1", "", parent="2")
    no_id, with_id = _draft("N", ""), _draft("3", "M3")
    _write_map(tmp_path / "ado-id-map.csv", [("1", 11)])
    monkeypatch.setattr(catalog_writer, "build_mapping_from_ir", lambda _ir: TemplateMapping())
    monkeypatch.setattr(
        catalog_writer, "load_catalog_drafts", lambda *_a, **_kw: [mapped, renumbered, child, no_id, with_id]
    )

    class Client:
        def __init__(self) -> None:
            self.created: list[str] = []

        def get(self, *_a, **_kw):
            raise AdoError("ADO 503 Service Unavailable: busy", status=503)

        def has_work_item_with_field(self, *_a):
            raise AdoError("ADO 503 Service Unavailable: busy", status=503)

        def create_work_item(self, _project, _wit, fields, **_kw):
            self.created.append(fields["System.Title"])
            return {"id": 50 + len(self.created)}

    client = Client()
    events: list[str] = []
    ir = {
        "targets": {"project_name": "P"},
        "source": {"catalog_source_dir": str(tmp_path)},
        "counts": {"catalog_rows": 5},
    }
    stats = catalog_writer.apply_catalog(
        client, ir, dry_run=False, emit=lambda _s, msg: events.append(msg), output_dir=tmp_path,
        include_deprecated_deleted=True, parallel_workers=1,
    )
    assert client.created == ["Title N"]
    assert stats["created"] == 1 and stats["skipped"] == 1
    failures = json.loads((tmp_path / "import-failures.json").read_text(encoding="utf-8"))["failures"]
    by_key = {f["key"]: f for f in failures}
    assert set(by_key) == {"2", "2.1", "3"}
    for key in ("2", "3"):
        assert by_key[key]["error"].startswith(LOOKUP_FAILED_PREFIX)
        assert by_key[key]["category"] == "lookup_failed"
    assert by_key["2.1"]["category"] == "parent_failed" and by_key["2.1"]["blockedBy"] == "2"
    assert any("WARNING" in e and "2 new row(s)" in e for e in events)


def test_rehearsal_reports_reused_and_released_rows(tmp_path) -> None:
    reader = Reader({1: _item("M-old"), 2: _item("M2"), 3: _item("M3")})
    drafts = [_draft("A", "M-new"), _draft("B", "M-old"), _draft("C", "M2"), _draft("D", "M3"), _draft("E", "M5")]
    id_map_rows = [
        {"Key": key, "WorkItemId": str(wid), "WorkItemType": "Process", "Title": key}
        for key, wid in (("A", 1), ("C", 2), ("GONE", 3))
    ]
    summary = _write_change_report(
        reader, drafts, id_map_rows, TemplateMapping(), project="P", output_dir=tmp_path, workers=2,
        emit=lambda *_: None, run_id="r", complete_workbook=True, project_lookup={"exists": True},
    )
    kinds = summary["byKind"]
    assert kinds["matched_other_key"] == 2  # B → #1 (was A), D → #3 (was GONE)
    assert kinds["key_reassigned"] == 1  # A is created as a new item
    assert kinds["new_row"] == 1 and kinds["not_in_workbook"] == 0  # E; GONE's item went to D
    assert summary["wouldCreate"] == 2 and summary["compared"] == 3
