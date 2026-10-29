"""Team area assignment aggregation (reference Script 3 semantics)."""

from __future__ import annotations

from typing import Any

import pytest

from app.ado_client import AdoError
from app.writers.process import StepSetupError
from app.writers.teams import (
    _assign_team_areas,
    _format_area_path,
    apply_teams,
    strip_project_prefix,
    workbook_teams,
)


class FakeClient:
    def __init__(self) -> None:
        self.patches: list[dict[str, Any]] = []
        self.project_id = "proj-1"
        self.teams = {
            "value": [
                {"name": "Business process catalog"},
                {"name": "Accounting"},
            ]
        }

    def get_project_id(self, _project: str) -> str:
        return self.project_id

    def get(self, path: str, **kwargs: Any) -> Any:
        if path.endswith("/teams"):
            return self.teams
        # Area nodes already exist.
        if "classificationnodes" in path:
            return {"name": "ok"}
        return {}

    def post(self, path: str, **kwargs: Any) -> Any:
        return {"ok": True}

    def patch(self, path: str, **kwargs: Any) -> Any:
        self.patches.append({"path": path, "project": kwargs.get("project"), "body": kwargs.get("json_body")})
        return {"ok": True}


def test_format_area_path() -> None:
    assert _format_area_path("MSBPCProj", "Functional areas\\Finance") == (
        "MSBPCProj\\Functional areas\\Finance"
    )


def test_assign_aggregates_all_paths_one_patch() -> None:
    client = FakeClient()
    _assign_team_areas(
        client,
        "MSBPCProj",
        "Accounting",
        "Functional areas\\Finance",
        {
            "Functional areas\\Finance",
            "Functional areas\\Finance\\Accounting",
        },
    )
    assert len(client.patches) == 1
    body = client.patches[0]["body"]
    assert body["defaultValue"] == "MSBPCProj\\Functional areas\\Finance"
    values = {row["value"] for row in body["values"]}
    assert values == {
        "MSBPCProj\\Functional areas\\Finance",
        "MSBPCProj\\Functional areas\\Finance\\Accounting",
    }
    assert all(row["includeChildren"] is False for row in body["values"])


def test_apply_teams_assigns_each_mapped_team() -> None:
    client = FakeClient()
    events: list[str] = []
    ir = {
        "targets": {"project_name": "MSBPCProj"},
        "counts": {"teams": 2, "area_paths": 3},
        "project": {
            "teams": [
                {"name": "Business process catalog"},
                {"name": "Accounting"},
            ],
            "area_paths": [
                {
                    "path": "Business process catalog",
                    "team": "Business process catalog",
                },
                {
                    "path": "Business process catalog\\Acquire to dispose",
                    "team": "Business process catalog",
                },
                {
                    "path": "Functional areas\\Finance\\Accounting",
                    "team": "Accounting",
                },
            ],
        },
    }
    stats = apply_teams(client, ir, dry_run=False, emit=lambda _s, m: events.append(m))
    assert stats["assignments"] == 2
    assert len(client.patches) == 2
    bpc = next(p for p in client.patches if p["project"] == "MSBPCProj/Business process catalog")
    assert len(bpc["body"]["values"]) == 2
    assert any("Assigned team 'Business process catalog'" in m for m in events)


class EmptyAreasClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_areas: list[tuple[str, str]] = []

    def get(self, path: str, **kwargs: Any) -> Any:
        if "classificationnodes" in path:
            if path not in {f"{parent}/{name}" for parent, name in self.created_areas}:
                raise AdoError("not found", status=404)
            return {"name": "ok"}
        return super().get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        if "classificationnodes" in path:
            self.created_areas.append((path, kwargs["json_body"]["name"]))
        return {}


def test_apply_teams_nests_areas_under_root_area_path() -> None:
    client = EmptyAreasClient()
    ir = {
        "targets": {"project_name": "MSBPCProj"},
        "counts": {"teams": 1, "area_paths": 1},
        "project": {
            "teams": [{"name": "Accounting"}],
            "area_paths": [{"path": "Finance", "team": "Accounting"}],
        },
    }
    apply_teams(client, ir, dry_run=False, emit=lambda *_: None, root_area_path="MSBPCProj\\Imported")
    assert client.created_areas == [
        ("/_apis/wit/classificationnodes/Areas", "Imported"),
        ("/_apis/wit/classificationnodes/Areas/Imported", "Finance"),
    ]
    body = client.patches[0]["body"]
    assert body["defaultValue"] == "MSBPCProj\\Imported\\Finance"


def test_apply_teams_merges_team_names_that_differ_only_in_case() -> None:
    client = FakeClient()
    events: list[str] = []
    ir = {
        "targets": {"project_name": "MSBPCProj"},
        "counts": {"teams": 1, "area_paths": 2},
        "project": {
            "teams": [{"name": "Accounting"}],
            "area_paths": [
                {"path": "Functional areas\\Finance", "team": "Accounting"},
                {"path": "Functional areas\\Finance\\Payables", "team": "accounting"},
            ],
        },
    }
    stats = apply_teams(client, ir, dry_run=False, emit=lambda _s, m: events.append(m))
    assert stats["assignments"] == 1
    assert len(client.patches) == 1
    assert client.patches[0]["project"] == "MSBPCProj/Accounting"
    assert {row["value"] for row in client.patches[0]["body"]["values"]} == {
        "MSBPCProj\\Functional areas\\Finance",
        "MSBPCProj\\Functional areas\\Finance\\Payables",
    }
    assert not any("no Area paths row" in m for m in events)


def test_apply_teams_sends_include_sub_areas_from_the_teams_sheet() -> None:
    client = FakeClient()
    ir = {
        "targets": {"project_name": "MSBPCProj"},
        "counts": {"teams": 2, "area_paths": 2},
        "project": {
            "teams": [
                {"name": "Accounting", "include_sub_areas": "Yes"},
                {"name": "Business process catalog", "include_sub_areas": "No"},
            ],
            "area_paths": [
                {"path": "Functional areas\\Finance", "team": "Accounting"},
                {"path": "Business process catalog", "team": "Business process catalog"},
            ],
        },
    }
    apply_teams(client, ir, dry_run=False, emit=lambda *_: None)
    include = {
        patch["project"]: {row["includeChildren"] for row in patch["body"]["values"]}
        for patch in client.patches
    }
    assert include == {
        "MSBPCProj/Accounting": {True},
        "MSBPCProj/Business process catalog": {False},
    }


class AreaTreeClient(FakeClient):
    """Answers the whole-tree read; per-node existence GETs would be a regression."""

    def __init__(self) -> None:
        super().__init__()
        self.gets: list[str] = []
        self.created_areas: list[tuple[str, str]] = []

    def get(self, path: str, **kwargs: Any) -> Any:
        self.gets.append(path)
        if path.startswith("/_apis/wit/classificationnodes/Areas?$depth="):
            return {
                "name": "MSBPCProj",
                "children": [{"name": "Functional areas", "children": [{"name": "Finance"}]}],
            }
        return super().get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        if "classificationnodes" in path:
            self.created_areas.append((path, kwargs["json_body"]["name"]))
        return {}


def test_apply_teams_reads_the_area_tree_once_and_creates_only_missing_nodes() -> None:
    client = AreaTreeClient()
    ir = {
        "targets": {"project_name": "MSBPCProj"},
        "counts": {"teams": 1, "area_paths": 2},
        "project": {
            "teams": [{"name": "Accounting"}],
            "area_paths": [
                {"path": "functional areas\\Finance", "team": "Accounting"},
                {"path": "Functional areas\\Finance\\Payables", "team": "Accounting"},
            ],
        },
    }
    stats = apply_teams(client, ir, dry_run=False, emit=lambda *_: None)
    assert client.created_areas == [
        ("/_apis/wit/classificationnodes/Areas/Functional%20areas/Finance", "Payables")
    ]
    assert stats["areas"] == 1
    assert [path for path in client.gets if "classificationnodes" in path] == [
        "/_apis/wit/classificationnodes/Areas?$depth=14"
    ]


def test_apply_teams_dry_run_does_not_patch() -> None:
    client = FakeClient()
    ir = {
        "targets": {"project_name": "MSBPCProj"},
        "counts": {"teams": 1, "area_paths": 1},
        "project": {
            "teams": [{"name": "Accounting"}],
            "area_paths": [{"path": "Functional areas", "team": "Accounting"}],
        },
    }
    stats = apply_teams(client, ir, dry_run=True, emit=lambda *_: None)
    assert stats.get("dry_run") is True
    assert stats["assignments"] == 1
    assert client.patches == []


class FailingTeamsClient(AreaTreeClient):
    def __init__(self, *, reject_area: str = "", team_errors: dict[str, AdoError] | None = None) -> None:
        super().__init__()
        self.reject_area = reject_area
        self.team_errors = team_errors or {}
        self.teams = {"value": []}

    def post(self, path: str, **kwargs: Any) -> Any:
        name = kwargs["json_body"]["name"]
        if path.endswith("/teams") and name in self.team_errors:
            raise self.team_errors[name]
        if "classificationnodes" in path and name == self.reject_area:
            raise AdoError("ADO 403 Forbidden: no permission", status=403)
        return super().post(path, **kwargs)


def _teams_ir() -> dict[str, Any]:
    return {
        "targets": {"project_name": "MSBPCProj"},
        "counts": {"teams": 2, "area_paths": 3},
        "project": {
            "teams": [{"name": "Accounting"}, {"name": "Payroll"}],
            "area_paths": [
                {"path": "Functional areas\\Finance\\Payables", "team": "Accounting"},
                {"path": "HR\\Payroll", "team": "Payroll"},
                {"path": "HR\\Payroll\\Benefits", "team": "Payroll"},
            ],
        },
    }


def test_area_that_cannot_be_created_fails_the_step_after_the_rest() -> None:
    client = FailingTeamsClient(reject_area="HR")
    events: list[str] = []
    with pytest.raises(StepSetupError) as caught:
        apply_teams(client, _teams_ir(), dry_run=False, emit=lambda _s, m: events.append(m))

    message = str(caught.value)
    assert "Teams step: 1 change(s)" in message and "area 'HR' couldn't be created" in message
    # Nodes below the failed one aren't tried; other areas and teams still go ahead.
    assert [name for _, name in client.created_areas] == ["Payables"]
    # Payroll's only areas are missing, so it isn't pointed at them.
    assert [p["project"] for p in client.patches] == ["MSBPCProj/Accounting"]
    assert caught.value.stats["teams"] == 2 and caught.value.stats["warnings"] == 0


def test_team_that_cannot_be_created_fails_the_step_and_skips_its_areas() -> None:
    client = FailingTeamsClient(team_errors={"Payroll": AdoError("ADO 403 Forbidden", status=403)})
    events: list[str] = []
    with pytest.raises(StepSetupError, match="team 'Payroll' couldn't be created"):
        apply_teams(client, _teams_ir(), dry_run=False, emit=lambda _s, m: events.append(m))
    assert [p["project"] for p in client.patches] == ["MSBPCProj/Accounting"]
    assert not any("no Area paths row" in m for m in events)


def test_team_that_already_exists_still_gets_its_areas() -> None:
    client = FailingTeamsClient(team_errors={"Payroll": AdoError("ADO 409 Conflict", status=409)})
    stats = apply_teams(client, _teams_ir(), dry_run=False, emit=lambda *_: None)
    assert stats["assignments"] == 2 and stats["warnings"] == 0
    assert sorted(p["project"] for p in client.patches) == ["MSBPCProj/Accounting", "MSBPCProj/Payroll"]


def test_workbook_teams_adds_area_only_teams_once_ignoring_case() -> None:
    ir = {
        "project": {
            "teams": [{"name": "Accounting", "bug_behavior": "off"}, {"name": "accounting"}],
            "area_paths": [{"path": "HR", "team": "Payroll"}, {"path": "HR\\X", "team": "PAYROLL"}, {"path": "Y"}],
        }
    }
    assert workbook_teams(ir) == [{"name": "Accounting", "bug_behavior": "off"}, {"name": "Payroll"}]


def test_level_one_area_named_like_the_project_is_kept() -> None:
    client = EmptyAreasClient()
    ir = {
        "targets": {"project_name": "Finance"},
        "counts": {"teams": 1, "area_paths": 1},
        "project": {"teams": [{"name": "AP"}], "area_paths": [{"path": "Finance\\AP", "team": "AP"}]},
    }
    apply_teams(client, ir, dry_run=False, emit=lambda *_: None)
    assert client.created_areas == [
        ("/_apis/wit/classificationnodes/Areas", "Finance"),
        ("/_apis/wit/classificationnodes/Areas/Finance", "AP"),
    ]
    assert client.patches[0]["body"]["defaultValue"] == "Finance\\Finance\\AP"


def test_root_area_path_drops_only_one_leading_project_segment() -> None:
    assert strip_project_prefix("Finance\\Finance\\Imported", "finance") == ["Finance", "Imported"]
    assert strip_project_prefix("Imported", "Finance") == ["Imported"]
    assert strip_project_prefix("Finance", "Finance") == []


class UnreadableTreeClient(EmptyAreasClient):
    """The whole-tree read fails, and one per-node check is refused."""

    def get(self, path: str, **kwargs: Any) -> Any:
        if "?$depth=" in path:
            raise AdoError("ADO 500 Internal Server Error", status=500)
        if path.endswith("/Areas/HR"):
            raise AdoError("ADO 403 Forbidden: no permission", status=403)
        return super().get(path, **kwargs)


def test_area_check_that_errors_fails_the_step_after_the_rest() -> None:
    client = UnreadableTreeClient()
    with pytest.raises(StepSetupError) as caught:
        apply_teams(client, _teams_ir(), dry_run=False, emit=lambda *_: None)
    message = str(caught.value)
    assert "Teams step: 1 change(s)" in message and "area 'HR' couldn't be checked" in message and "403" in message
    assert [name for _, name in client.created_areas] == ["Functional areas", "Finance", "Payables"]
    assert [p["project"] for p in client.patches] == ["MSBPCProj/Accounting"]
