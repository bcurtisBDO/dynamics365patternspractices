"""Plain-language failure explanations, root-cause grouping, and CSV output."""

from __future__ import annotations

import json

import pytest

from app.catalog_import import WorkItemDraft
from app.failure_explain import (
    LOOKUP_FAILED_PREFIX,
    enrich_failures,
    explain_failure,
    summarize_failures,
    write_failures_csv,
)


def _ado(message: str, *, custom: bool = False) -> str:
    body = {"customProperties": {"ErrorMessage": message}} if custom else {"message": message}
    return "ADO 400 Bad Request: " + json.dumps(body)


def _draft(key: str, **fields) -> WorkItemDraft:
    return WorkItemDraft(
        key=key, parent_key=None, work_item_type="System process", title=key,
        fields=fields, source_file="bpc.xlsx", source_row=3,
    )


def test_area_path_error_names_the_path() -> None:
    draft = _draft("a", **{"System.AreaPath": "Proj\\Missing"})
    result = explain_failure(
        _ado("TF401347: Invalid tree name given for work item -1, field 'System.AreaPath'."), draft
    )
    assert result["category"] == "area_path"
    assert "Proj\\Missing" in result["problem"]
    assert result["value"] == "Proj\\Missing"


def test_iteration_path_error_is_not_reported_as_area_path() -> None:
    draft = _draft("a", **{"System.AreaPath": "Proj", "System.IterationPath": "OldProj"})
    result = explain_failure(
        _ado("TF401347: Invalid tree name given for work item -1, field 'System.IterationPath'."), draft
    )
    assert result["category"] == "iteration_path"
    assert result["fieldLabel"] == "Iteration path"
    assert result["value"] == "OldProj"


def test_too_long_uses_field_label() -> None:
    draft = _draft("a", **{"MSBPC.menupath": "x" * 300})
    result = explain_failure(
        _ado("TF401324: Value for field 'MSBPC.menupath' is too long."),
        draft,
        label_for=lambda ref: {"MSBPC.menupath": "Menu path"}.get(ref, ref),
    )
    assert result["category"] == "text_too_long"
    assert result["problem"] == "Menu path is longer than 255 characters (300 characters)."


def test_invalid_state() -> None:
    draft = _draft("a", **{"System.State": "Deprecated"})
    message = "The field 'State' contains the value 'Deprecated' that is not in the list of supported values"
    result = explain_failure(_ado(message, custom=True), draft)
    assert result["category"] == "invalid_state"
    assert "Deprecated" in result["problem"] and "System process" in result["problem"]


def test_held_rows_and_unconfirmed_creates_say_rerun() -> None:
    draft = _draft("a", **{"MSBPC.microsoftid": "M1"})
    held = explain_failure(LOOKUP_FAILED_PREFIX + "ADO 503 Service Unavailable: busy", draft)
    assert held["category"] == "lookup_failed"
    assert held["value"] == "M1"
    assert held["fix"].startswith("Re-run Catalog Import")
    unconfirmed = explain_failure(
        "Network error talking to Azure DevOps (outcome unknown, not retried): reset; "
        "the work item didn't show up in Azure DevOps, so it wasn't created again",
        draft,
    )
    assert unconfirmed["category"] == "network"
    assert unconfirmed["fix"].startswith("Re-run Catalog Import")


def test_server_error_on_create_is_an_unconfirmed_create(monkeypatch) -> None:
    from app.ado_client import AdoClient, AdoError

    client = AdoClient("https://dev.azure.com/contoso", "x" * 52)
    body = json.dumps({"message": "TF246017: database error"}).encode()
    monkeypatch.setattr(client, "_send", lambda *_a: (500, "Internal Server Error", {}, body))
    with pytest.raises(AdoError) as caught:
        client.create_work_item("Demo", "Scenario", {"System.Title": "x"})
    error = f"{caught.value}; the work item didn't show up in Azure DevOps, so it wasn't created again"
    result = explain_failure(error, _draft("a"))
    assert result["category"] == "network"
    assert result["fix"].startswith("Re-run Catalog Import")


def test_auth_and_unknown_errors() -> None:
    assert explain_failure("ADO 401 Unauthorized: nope", None)["category"] == "permission"
    other = explain_failure(_ado("Something odd happened"), None)
    assert other["category"] == "other" and other["problem"] == "Something odd happened"


def test_cascade_points_to_root_and_counts_children(tmp_path) -> None:
    failures = [
        {"key": "root", "parentKey": None, "error": _ado("TF401324: Value for field 'X' is too long."),
         "sourceFile": "bpc.xlsx", "sourceRow": 2, "title": "Root", "workItemType": "Process"},
        {"key": "child", "parentKey": "root", "error": "Parent not imported: root",
         "sourceFile": "bpc.xlsx", "sourceRow": 3, "title": "Child", "workItemType": "Scenario"},
        {"key": "grandchild", "parentKey": "child", "error": "Parent not imported: child",
         "sourceFile": "bpc.xlsx", "sourceRow": 4, "title": "Grandchild", "workItemType": "Test Case"},
    ]
    enrich_failures(failures, {})
    assert failures[2]["blockedBy"] == "root"
    assert failures[0]["blockedChildren"] == 2
    assert "root failed" in failures[2]["problem"]
    summary = summarize_failures(failures)
    assert (summary["rootCauses"], summary["blockedByParent"]) == (1, 2)
    assert summary["groups"][0]["blockedChildren"] == 2

    path = tmp_path / "failures.csv"
    write_failures_csv(failures, path)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("Status,Problem,How to fix")
    assert lines[1].startswith("Fix this row,")
    assert lines[2].startswith("Blocked by parent,") and lines[3].startswith("Blocked by parent,")
