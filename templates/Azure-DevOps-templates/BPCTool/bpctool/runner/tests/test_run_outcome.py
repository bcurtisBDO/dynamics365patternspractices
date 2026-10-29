"""Run outcome headline/attention and the always-written summary report."""

from __future__ import annotations

import html
import json
from urllib.parse import quote

import pytest
from fastapi import HTTPException

from app.run_outcome import build_outcome
from app.writers.report import write_run_summary


def _ir() -> dict:
    return {
        "targets": {"process_name": "Proc", "project_name": "Proj"},
        "source": {"fingerprint": "sha256:abc"},
        "counts": {"catalog_rows": 10},
    }


def _catalog_with_failures() -> dict:
    return {
        "drafts": 10, "created": 7, "skipped": 0, "failed": 3, "missing_parent": 2,
        "failure_summary": {
            "total": 3, "rootCauses": 1, "blockedByParent": 2,
            "groups": [{"category": "area_path", "label": "Area path does not exist in the project",
                        "fix": "Add it.", "count": 1, "blockedChildren": 2,
                        "examples": [{"key": "k", "sourceFile": "bpc.xlsx", "sourceRow": 5,
                                      "problem": "Area path 'X' does not exist in the project."}]}],
        },
        "prevalidate": {"needsReview": [], "automaticFixes": [], "needsReviewTotal": 0},
    }


def test_outcome_explains_catalog_failures() -> None:
    outcome = build_outcome({"catalog": _catalog_with_failures()}, dry_run=False, output_dir=None)
    assert outcome["status"] == "attention"
    assert outcome["headline"] == "Finished with problems: 7 of 10 catalog items imported"
    assert "1 row needs fixing; it blocked 2 more" in outcome["subline"]
    item = outcome["attention"][0]
    assert item["severity"] == "error" and item["file"] == "import-failures.csv"
    assert "2 child rows" in item["detail"]
    assert item["examples"] == ["bpc.xlsx row 5 (k) — Area path 'X' does not exist in the project."]


def test_unknown_outcome_creates_are_listed_once_with_the_rerun_fix() -> None:
    from app.failure_explain import CATEGORY_TEXT

    network_group = {"category": "network", "label": CATEGORY_TEXT["network"]["label"],
                     "fix": CATEGORY_TEXT["network"]["fix"], "count": 2, "blockedChildren": 0, "examples": []}
    catalog = {"drafts": 5, "created": 3, "failed": 2, "outcome_unknown": 2,
               "failure_summary": {"total": 2, "rootCauses": 2, "blockedByParent": 0, "groups": [network_group]}}
    outcome = build_outcome({"catalog": catalog}, dry_run=False, output_dir=None)
    titles = [item["title"] for item in outcome["attention"]]
    assert titles == ["2 rows: Network error — outcome unknown"]

    # Without a failure group for them (e.g. an older summary), the item gives the same fix.
    catalog["failure_summary"] = {}
    outcome = build_outcome({"catalog": catalog}, dry_run=False, output_dir=None)
    item = next(i for i in outcome["attention"] if "unknown outcome" in i["title"])
    assert item["fix"] == CATEGORY_TEXT["network"]["fix"]
    assert "duplicates" not in item["fix"]
    assert "matched by their Microsoft ID" in item["fix"]


def test_failed_setup_step_keeps_its_counts_and_shows_as_failed() -> None:
    from app.run_outcome import describe_step

    status, text = describe_step(
        "layout",
        {"wits_processed": 4, "controls": 30, "hidden_fields": 2, "warnings": 1, "error": "Layout step: 1 change failed"},
        dry_run=False,
    )
    assert status == "error"
    assert text.startswith("4 forms laid out with 30 controls (2 hidden fields).")
    assert text.endswith("Then the step failed — see the error at the top.")


def test_outcome_headline_says_when_catalog_problems_are_from_earlier_run() -> None:
    outcome = build_outcome(
        {"teams": {"teams": 2}, "catalog": _catalog_with_failures()},
        dry_run=False,
        output_dir=None,
        earlier_steps={"catalog": "2026-10-14T09:30:00+00:00"},
    )
    assert outcome["headline"] == "Finished — problems from an earlier Catalog Import are still open"
    assert "1 row from the Catalog Import on 2026-10-14 09:30 still needs fixing" in outcome["subline"]


def test_summary_headline_for_catalog_rerun_vs_other_steps(tmp_path) -> None:
    def run(step_results: dict, run_id: str) -> dict:
        return write_run_summary(
            _ir(), step_results=step_results, output_dir=tmp_path,
            org_url="https://dev.azure.com/o", dry_run=False, run_id=run_id,
        )

    run({"catalog": _catalog_with_failures()}, "run1")
    # Catalog Import ran again and still failed: this run's problems, not earlier ones.
    rerun = run({"catalog": _catalog_with_failures()}, "run2")
    assert rerun["headline"] == "Finished with problems: 7 of 10 catalog items imported"
    # Only Teams ran: the open catalog failures come from the earlier run.
    teams_only = run({"teams": {"teams": 2}}, "run3")
    assert teams_only["headline"] == "Finished — problems from an earlier Catalog Import are still open"


def test_outcome_success_and_rehearsal() -> None:
    clean = {"drafts": 4, "created": 4, "skipped": 0, "failed": 0}
    assert build_outcome({"catalog": clean}, dry_run=False, output_dir=None)["status"] == "success"
    rehearsal = build_outcome({"catalog": clean}, dry_run=True, output_dir=None)
    assert rehearsal["status"] == "rehearsal"
    assert rehearsal["steps"][0]["status"] == "rehearsed"


def test_summary_keeps_earlier_steps_and_lists_files(tmp_path) -> None:
    write_run_summary(
        _ir(), step_results={"process": {"wits_created": 3}}, output_dir=tmp_path,
        org_url="https://dev.azure.com/o", dry_run=False, run_id="run1",
    )
    outcome = write_run_summary(
        _ir(), step_results={"catalog": _catalog_with_failures()}, output_dir=tmp_path,
        org_url="https://dev.azure.com/o", dry_run=False, run_id="run2",
    )
    data = json.loads((tmp_path / "bpc-ado-setup-summary.json").read_text())
    assert data["runId"] == "run2"
    assert data["stepResults"]["process"]["runId"] == "run1"
    assert data["stepResults"]["catalog"]["runId"] == "run2"
    process_step = next(step for step in outcome["steps"] if step["id"] == "process")
    assert process_step["earlierRun"]
    html = (tmp_path / "bpc-ado-setup-summary.html").read_text()
    assert "Finished with problems" in html and "What needs attention" in html
    assert {f["name"] for f in outcome["files"]} >= {"bpc-ado-setup-summary.html"}


def test_project_name_substitution_is_shown_in_outcome_and_html(tmp_path) -> None:
    from app.run_outcome import project_name_notice

    assert project_name_notice("Contoso", template_rows=0, catalog_values=0) == ""
    ir = _ir()
    ir["project"] = {"project_placeholders_replaced": 4}
    catalog = {
        "drafts": 2,
        "created": 2,
        "prevalidate": {
            "automaticFixes": [{"kind": "project_placeholder_replaced", "label": "x", "count": 30}]
        },
    }
    outcome = write_run_summary(
        ir, step_results={"catalog": catalog}, output_dir=tmp_path, org_url="o", dry_run=True, run_id="r"
    )
    assert outcome["notices"] == [
        "Project name applied: 'Your Project Name Here' → '"
        + ir["targets"]["project_name"]
        + "' in 4 template area/iteration rows and 30 catalog Area/Iteration Path values."
    ]
    html_text = (tmp_path / "rehearsal-summary.html").read_text(encoding="utf-8")
    assert 'class="notice"' in html_text and "Your Project Name Here" in html_text


def test_outcome_reports_states_set_after_create_and_ones_that_failed() -> None:
    catalog = {
        "drafts": 3, "created": 3, "skipped": 0, "failed": 0,
        "state_set_after_create": 2, "state_not_set": 1,
        "state_not_set_examples": [{"key": "k", "workItemId": 9, "state": "New", "sourceFile": "bpc.xlsx",
                                    "sourceRow": 4, "error": "TF401320"}],
    }
    outcome = build_outcome({"catalog": catalog}, dry_run=False, output_dir=None)
    step = outcome["steps"][0]
    assert "2 items created in the type's first state, then moved to its State" in step["summary"]
    item = outcome["attention"][0]
    assert item["severity"] == "warning" and item["count"] == 1
    assert item["title"].startswith("1 item created but not moved to its State")
    assert item["examples"] == ["bpc.xlsx row 4 (k, work item 9) — State 'New': TF401320"]


def test_ado_links_use_the_canonical_org_and_encode_names() -> None:
    from app.run_outcome import ado_links

    links = ado_links("https://contoso.visualstudio.com/", project_name="BPC Demo", process_name="BPC & Co")
    assert links == [
        {"label": "Project", "url": "https://dev.azure.com/contoso/BPC%20Demo"},
        {"label": "Boards", "url": "https://dev.azure.com/contoso/BPC%20Demo/_boards/board"},
        {"label": "Work items", "url": "https://dev.azure.com/contoso/BPC%20Demo/_workitems"},
        {"label": "Process settings", "url": "https://dev.azure.com/contoso/_settings/process?process-name=BPC%20%26%20Co"},
    ]
    assert ado_links("not a url", project_name="P", process_name="Q") == []


def test_report_links_into_azure_devops(tmp_path) -> None:
    outcome = write_run_summary(
        _ir(), step_results={"teams": {"teams": 2}}, output_dir=tmp_path,
        org_url="https://dev.azure.com/o", dry_run=False, run_id="r",
    )
    assert [link["label"] for link in outcome["adoLinks"]] == ["Project", "Boards", "Work items", "Process settings"]
    page = (tmp_path / "bpc-ado-setup-summary.html").read_text(encoding="utf-8")
    assert '<a href="https://dev.azure.com/o/Proj/_workitems" target="_blank" rel="noreferrer">Work items</a>' in page
    assert "A rehearsal creates nothing" not in page

    write_run_summary(
        _ir(), step_results={}, output_dir=tmp_path, org_url="https://dev.azure.com/o", dry_run=True, run_id="r"
    )
    rehearsal = (tmp_path / "rehearsal-summary.html").read_text(encoding="utf-8")
    assert "https://dev.azure.com/o/_settings/process?process-name=Proc" in rehearsal
    assert "A rehearsal creates nothing" in rehearsal


def test_rehearsal_writes_separate_summary(tmp_path) -> None:
    write_run_summary(
        _ir(), step_results={}, output_dir=tmp_path, org_url="o", dry_run=True, run_id="r"
    )
    assert (tmp_path / "rehearsal-summary.html").is_file()
    assert not (tmp_path / "bpc-ado-setup-summary.html").exists()


def test_outputs_endpoint_serves_known_files_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BPCTOOL_OUTPUT_DIR", str(tmp_path))
    from app.main import output_file

    folder = tmp_path / "org_Proj"
    folder.mkdir()
    (folder / "import-failures.csv").write_text("Status\n", encoding="utf-8")
    (folder / "secret.txt").write_text("x", encoding="utf-8")
    response = output_file("org_Proj", "import-failures.csv")
    assert str(response.path) == str((folder / "import-failures.csv").resolve())
    assert "attachment" in response.headers["content-disposition"]
    for folder_name, name in [
        ("org_Proj", "secret.txt"),
        ("../etc", "import-failures.csv"),
        ("..", "import-failures.csv"),
        ("org_Proj", "import-prevalidate.csv"),
    ]:
        with pytest.raises(HTTPException):
            output_file(folder_name, name)


def test_outputs_endpoint_serves_from_user_selected_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BPCTOOL_OUTPUT_DIR", str(tmp_path / "default"))
    from app.main import output_file

    chosen = tmp_path / "chosen"
    folder = chosen / "org_Proj"
    folder.mkdir(parents=True)
    (folder / "import-failures.csv").write_text("Status\n", encoding="utf-8")
    (folder / "bpc-ado-setup-summary.html").write_text(
        '<a href="import-failures.csv">x</a> <a href="https://example.com/">y</a>', encoding="utf-8"
    )
    response = output_file("org_Proj", "import-failures.csv", root=str(chosen))
    assert str(response.path) == str((folder / "import-failures.csv").resolve())
    page = output_file("org_Proj", "bpc-ado-setup-summary.html", root=str(chosen)).body.decode()
    assert f'href="import-failures.csv?root={html.escape(quote(str(chosen)))}"' in page
    assert 'href="https://example.com/"' in page
    with pytest.raises(HTTPException):
        output_file("org_Proj", "bpc-ado-setup-summary.html", root=None)
    with pytest.raises(HTTPException):
        output_file("org_Proj", "bpc-ado-setup-summary.html", root="relative")


def test_rehearsal_run_end_to_end_writes_summary(template_path, tmp_path) -> None:
    from app.apply_engine import run_apply
    from app.models import DryRunRequest

    request = DryRunRequest(
        org_url="https://dev.azure.com/contoso",
        process_name="Business Process Catalog",
        project_name="Contoso-BPC",
        excel_file=str(template_path),
        output_root=str(tmp_path),
    )
    result = run_apply(request, pat="", dry_run=True, step_filter=["process", "report"])
    assert result.ok, result.error
    assert result.output_dir == str(tmp_path / "contoso_Contoso-BPC")
    assert (tmp_path / "contoso_Contoso-BPC" / "rehearsal-summary.html").is_file()
    assert result.outcome and result.outcome["status"] == "rehearsal"


def test_skip_existing_run_tells_backlog_to_leave_the_process_alone(
    template_path, tmp_path, monkeypatch
) -> None:
    from app import apply_engine
    from app.models import DryRunRequest

    seen: dict[str, bool] = {}

    def fake_backlog(_client, _ir, *, process_changes=True, **_kwargs):
        seen["process_changes"] = process_changes
        return {"warnings": 0}

    monkeypatch.setattr(apply_engine, "apply_backlog", fake_backlog)
    request = DryRunRequest(
        org_url="https://dev.azure.com/contoso",
        process_name="Business Process Catalog",
        project_name="Contoso-BPC",
        excel_file=str(template_path),
        output_root=str(tmp_path),
        update_policy="skip-existing",
        known_process_exists=True,
    )
    apply_engine.run_apply(request, pat="", dry_run=True, step_filter=["backlog"])
    assert seen == {"process_changes": False}

    seen.clear()
    fresh = request.model_copy(update={"known_process_exists": False})
    apply_engine.run_apply(fresh, pat="", dry_run=True, step_filter=["backlog"])
    assert seen == {"process_changes": True}


def test_live_run_checks_ado_for_existing_targets_without_inventory(
    template_path, tmp_path, monkeypatch
) -> None:
    from app import apply_engine
    from app.models import DryRunRequest

    class ExistingTargets:
        def __init__(self, *_args) -> None:
            pass

        def get_process_id(self, _name: str) -> str:
            return "proc-guid"

        def get_project_id(self, _name: str) -> None:
            return None

    monkeypatch.setattr(apply_engine, "AdoClient", ExistingTargets)
    request = DryRunRequest(
        org_url="https://dev.azure.com/contoso",
        process_name="Business Process Catalog",
        project_name="Contoso-BPC",
        excel_file=str(template_path),
        output_root=str(tmp_path),
        update_policy="create-only",
    )
    result = apply_engine.run_apply(request, pat="x", dry_run=False, step_filter=["process"])
    assert not result.ok
    assert 'Process "Business Process Catalog" already exists and policy is create-only' in result.error
    assert "the browser hadn't loaded its list of existing processes" in result.error
    assert "Click Fetch existing processes in the Connection section so the confirm box appears" in result.error

    stale = request.model_copy(update={"known_process_exists": False})
    result = apply_engine.run_apply(stale, pat="x", dry_run=False, step_filter=["process"])
    assert "wasn't in the browser's list of existing processes (the list was out of date)" in result.error

    preview = apply_engine.build_dry_run_plan(
        apply_engine.build_ir(str(template_path), process_name="P1", project_name="Proj1"),
        update_policy="create-only",
        confirm_process_update=False,
        confirm_project_update=False,
        known_process_exists=None,
        known_project_exists=False,
        step_filter=["process", "project"],
    )
    assert any("Not known whether the process already exists" in w for w in preview.warnings)


def test_live_run_writes_summary_when_ado_check_fails(template_path, tmp_path, monkeypatch) -> None:
    from app import apply_engine
    from app.ado_client import AdoError
    from app.models import DryRunRequest

    class ExpiredPat:
        def __init__(self, *_args) -> None:
            pass

        def get_process_id(self, _name: str) -> str:
            raise AdoError("Azure DevOps returned a sign-in page (HTTP 203)", status=401)

    monkeypatch.setattr(apply_engine, "AdoClient", ExpiredPat)
    request = DryRunRequest(
        org_url="https://dev.azure.com/contoso",
        process_name="Business Process Catalog",
        project_name="Contoso-BPC",
        excel_file=str(template_path),
        output_root=str(tmp_path),
    )
    events: list[dict] = []
    result = apply_engine.run_apply(request, pat="x", dry_run=False, step_filter=["process"], emit=events.append)
    assert not result.ok
    assert result.error.startswith("Could not check the process and project in Azure DevOps before the run")
    assert "sign-in page" in result.error
    assert result.outcome and result.outcome["status"] == "failed"
    assert (tmp_path / "contoso_Contoso-BPC" / "bpc-ado-setup-summary.html").is_file()
    assert any(event["type"] == "job-error" for event in events)


class _ReadyTargets:
    """Live client stand-in: process and project exist; records the project wait."""

    waited: list[str] = []
    cancel_wait = False

    def __init__(self, *_args) -> None:
        pass

    def get_process_id(self, _name: str) -> str:
        return "proc-guid"

    def get_project_id(self, _name: str) -> str:
        return "proj-guid"

    def get_ready_project_id(self, _name: str, *, should_cancel=None, on_wait=None) -> str:
        from app.ado_client import RunCancelled

        if self.cancel_wait:
            raise RunCancelled("Run cancelled by user while waiting for Azure DevOps to create project 'Contoso-BPC'.")
        on_wait("createPending")
        self.waited.append("ready")
        return "proj-guid"

    def require_multivalue_extension(self) -> None:
        return None


def _live_request(template_path, tmp_path):
    from app.models import DryRunRequest

    return DryRunRequest(
        org_url="https://dev.azure.com/contoso",
        process_name="Business Process Catalog",
        project_name="Contoso-BPC",
        excel_file=str(template_path),
        output_root=str(tmp_path),
        update_policy="always-update",
    )


@pytest.mark.parametrize("step", ["layout", "teams"])
def test_step_setup_error_keeps_the_step_counts(template_path, tmp_path, monkeypatch, step) -> None:
    from app import apply_engine
    from app.writers.process import StepSetupError

    stats = {"layout": {"wits_processed": 3, "controls": 12, "hidden_fields": 1, "warnings": 0},
             "teams": {"teams": 2, "areas": 5, "assignments": 4, "warnings": 0}}[step]

    def failing_step(*_args, **_kwargs):
        raise StepSetupError(f"{step} step: 1 change(s) that later steps depend on failed", stats=dict(stats))

    monkeypatch.setattr(apply_engine, "AdoClient", _ReadyTargets)
    monkeypatch.setattr(apply_engine, f"apply_{step}", failing_step)
    result = apply_engine.run_apply(_live_request(template_path, tmp_path), pat="x", dry_run=False, step_filter=[step])
    assert not result.ok
    kept = result.step_results[step]
    assert {key: kept[key] for key in stats} == stats
    assert "later steps depend on failed" in kept["error"]
    card = next(s for s in result.outcome["steps"] if s["id"] == step)
    assert card["status"] == "error"
    assert ("3 forms laid out" if step == "layout" else "2 teams, 5 new area paths") in card["summary"]


def test_live_run_waits_for_a_project_still_being_created(template_path, tmp_path, monkeypatch) -> None:
    from app import apply_engine

    monkeypatch.setattr(apply_engine, "AdoClient", _ReadyTargets)
    monkeypatch.setattr(_ReadyTargets, "waited", [])
    monkeypatch.setattr(apply_engine, "apply_teams", lambda *_a, **_k: {"teams": 1, "warnings": 0})
    events: list[dict] = []
    result = apply_engine.run_apply(
        _live_request(template_path, tmp_path), pat="x", dry_run=False, step_filter=["teams"], emit=events.append
    )
    assert result.ok, result.error
    assert _ReadyTargets.waited == ["ready"]
    assert any("still being set up" in str(event.get("message")) for event in events)


def test_cancel_while_waiting_for_the_project_cancels_the_run(template_path, tmp_path, monkeypatch) -> None:
    from app import apply_engine

    monkeypatch.setattr(apply_engine, "AdoClient", _ReadyTargets)
    monkeypatch.setattr(_ReadyTargets, "cancel_wait", True)
    result = apply_engine.run_apply(
        _live_request(template_path, tmp_path), pat="x", dry_run=False, step_filter=["teams"],
        should_cancel=lambda: True,
    )
    assert result.cancelled and not result.ok
    assert result.outcome and result.outcome["status"] == "cancelled"


def test_cancel_after_the_last_step_still_counts_as_finished(template_path, tmp_path) -> None:
    from app.apply_engine import run_apply
    from app.models import DryRunRequest

    request = DryRunRequest(
        org_url="https://dev.azure.com/contoso",
        process_name="Business Process Catalog",
        project_name="Contoso-BPC",
        excel_file=str(template_path),
        output_root=str(tmp_path),
    )
    steps_done: list[str] = []

    def cancel_once_report_finished() -> bool:
        return "report" in steps_done

    result = run_apply(
        request,
        pat="",
        dry_run=True,
        step_filter=["report"],
        should_cancel=cancel_once_report_finished,
        emit=lambda event: event["type"] == "step-completed" and steps_done.append(event["stepId"]),
    )
    assert result.ok and not result.cancelled, result.error
