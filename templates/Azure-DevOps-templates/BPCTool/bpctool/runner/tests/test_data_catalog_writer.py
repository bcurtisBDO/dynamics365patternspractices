from __future__ import annotations

import csv

from app.catalog_import import WorkItemDraft
from app.catalog_validate import MAX_STRING_LENGTH, prevalidate_catalog_drafts
from app.mapping import TemplateMapping
from app.writers import catalog as catalog_writer


class AreaTreeClient:
    def get(self, path: str, **_kw) -> dict:
        assert "classificationnodes/Areas" in path
        return {
            "name": "Finance",
            "children": [
                {"name": "Finance", "children": [{"name": "AP"}]},
                {"name": "Shared"},
            ],
        }


def _draft(key: str, **fields) -> WorkItemDraft:
    return WorkItemDraft(
        key=key,
        parent_key=None,
        work_item_type="Scenario",
        title=str(fields.get("System.Title") or key),
        fields=dict(fields),
        source_file="bpc.xlsx",
        source_row=4,
    )


def test_live_area_under_same_named_area_keeps_both_segments() -> None:
    areas, states = catalog_writer._live_project_context(AreaTreeClient(), "Finance", set(), lambda *_: None)
    assert sorted(areas) == ["Finance\\Finance", "Finance\\Finance\\AP", "Finance\\Shared"]
    assert states is None
    ir = {"targets": {"project_name": "Finance"}, "project": {"area_paths": []}}
    nested = _draft("a", **{"System.AreaPath": "Finance\\Finance\\AP"})
    shared = _draft("b", **{"System.AreaPath": "Finance\\Shared"})
    missing = _draft("c", **{"System.AreaPath": "Finance\\AP"})
    result = prevalidate_catalog_drafts(
        [nested, shared, missing], ir, TemplateMapping(), project="Finance", extra_area_paths=areas
    )
    assert nested.fields["System.AreaPath"] == "Finance\\Finance\\AP"
    assert shared.fields["System.AreaPath"] == "Finance\\Shared"
    assert "System.AreaPath" not in missing.fields
    assert [(i.key, i.kind) for i in result.issues] == [("c", "invalid_area_path")]


def test_preview_csv_shows_rows_after_data_checks(tmp_path, monkeypatch) -> None:
    long_title = "=" + "x" * 400
    drafts = [_draft("k1", **{"System.Title": long_title, "System.Description": "Ünïcode"})]
    monkeypatch.setattr(catalog_writer, "load_catalog_drafts", lambda *_a, **_kw: drafts)
    ir = {
        "targets": {"project_name": "P"},
        "source": {"catalog_source_dir": str(tmp_path)},
        "counts": {"catalog_rows": 1},
    }
    catalog_writer.apply_catalog(None, ir, dry_run=True, emit=lambda *_: None, output_dir=tmp_path)
    raw = (tmp_path / "import-preview.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with (tmp_path / "import-preview.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    title = rows[0]["Title"]
    assert title.startswith("'=")
    assert len(title) == MAX_STRING_LENGTH + 1 and title.endswith("…")
    assert rows[0]["DescriptionSample"] == "Ünïcode"
