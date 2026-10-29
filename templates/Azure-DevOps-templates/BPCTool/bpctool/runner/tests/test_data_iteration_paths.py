from __future__ import annotations

from app.catalog_import import WorkItemDraft
from app.catalog_validate import prevalidate_catalog_drafts, prevalidate_summary
from app.mapping import TemplateMapping


def _ir() -> dict:
    return {
        "targets": {"project_name": "NewProj"},
        "process": {"picklists": {}},
        "project": {
            "area_paths": [],
            "iteration_paths": [
                {"path": "Release 1"},
                {"path": "Release 1\\Sprint 1"},
            ],
        },
    }


def _draft(key: str, iteration: str) -> WorkItemDraft:
    return WorkItemDraft(
        key=key,
        parent_key=None,
        work_item_type="Scenario",
        title=key,
        fields={"System.IterationPath": iteration},
        source_file="bpc.xlsx",
        source_row=9,
    )


def test_unknown_multi_segment_iteration_goes_to_project_root_and_is_reported() -> None:
    other = _draft("a", "OtherProj\\Unknown")
    partly_known = _draft("b", "Release 1\\Sprint 9")
    result = prevalidate_catalog_drafts([other, partly_known], _ir(), TemplateMapping())
    assert other.fields["System.IterationPath"] == "NewProj"
    assert partly_known.fields["System.IterationPath"] == "NewProj"
    assert result.counts == {"invalid_iteration_path": 2}
    assert [(i.key, i.action, i.new_value) for i in result.issues] == [
        ("a", "sanitize", "NewProj"),
        ("b", "sanitize", "NewProj"),
    ]
    review = prevalidate_summary(result)["needsReview"]
    assert [g["kind"] for g in review] == ["invalid_iteration_path"]


def test_unknown_multi_segment_iteration_only_reported_without_sanitize() -> None:
    draft = _draft("a", "OtherProj\\Unknown")
    result = prevalidate_catalog_drafts([draft], _ir(), TemplateMapping(), sanitize=False)
    assert draft.fields["System.IterationPath"] == "OtherProj\\Unknown"
    assert [(i.kind, i.action) for i in result.issues] == [("invalid_iteration_path", "report")]


def test_existing_iteration_rewrites_are_unchanged() -> None:
    known = _draft("a", "Release 1\\Sprint 1")
    renamed = _draft("b", "OldProj\\Release 1\\Sprint 1")
    in_project = _draft("c", "NewProj\\Anything")
    result = prevalidate_catalog_drafts([known, renamed, in_project], _ir(), TemplateMapping())
    assert known.fields["System.IterationPath"] == "NewProj\\Release 1\\Sprint 1"
    assert renamed.fields["System.IterationPath"] == "NewProj\\Release 1\\Sprint 1"
    assert in_project.fields["System.IterationPath"] == "NewProj\\Anything"
    assert result.counts == {"area_path_project_renamed": 2}
