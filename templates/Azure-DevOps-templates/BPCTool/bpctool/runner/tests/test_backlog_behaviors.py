"""Backlog level rename/create and WIT→behavior assignment (reference Script 4 Parts 1–2)."""

from __future__ import annotations

from typing import Any

import pytest

from app.writers.backlog import (
    _assign_wits_to_backlogs,
    _configure_backlog_levels,
    _set_default_wits,
)


class BehaviorFakeClient:
    def __init__(self, behaviors: list[dict[str, Any]], wits: list[dict[str, Any]]) -> None:
        self.behaviors = {str(b["referenceName"]): dict(b) for b in behaviors}
        self.wits = list(wits)
        self.wit_behaviors: dict[str, list[dict[str, Any]]] = {}
        self.puts: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.gets: list[str] = []
        self.wit_list_reads = 0
        self.reject_update = False

    def list_process_wits(self, _process_id: str) -> list[dict[str, Any]]:
        self.wit_list_reads += 1
        return list(self.wits)

    def get(self, path: str, **_kwargs: Any) -> Any:
        self.gets.append(path)
        if path.endswith("/behaviors") and "workitemtypesbehaviors" not in path:
            return {"value": list(self.behaviors.values())}
        if "/workitemtypesbehaviors/" in path and path.endswith("/behaviors"):
            # .../workitemtypesbehaviors/{witRef}/behaviors
            wit_ref = path.split("/workitemtypesbehaviors/")[1].split("/behaviors")[0]
            return {"value": [dict(cb) for cb in self.wit_behaviors.get(wit_ref, [])]}
        return {"value": []}

    def patch(self, path: str, **kwargs: Any) -> Any:
        body = kwargs.get("json_body") or {}
        self.patches.append({"path": path, "body": body})
        if self.reject_update:
            from app.ado_client import AdoError

            raise AdoError("ADO 405 Method Not Allowed", status=405)
        wit_ref = path.split("/workitemtypesbehaviors/")[1].split("/behaviors")[0]
        for cb in self.wit_behaviors.get(wit_ref, []):
            if cb["behavior"]["id"] == body["behavior"]["id"]:
                cb["isDefault"] = bool(body.get("isDefault"))
        return body

    def put(self, path: str, **kwargs: Any) -> Any:
        body = kwargs.get("json_body") or {}
        self.puts.append({"path": path, "body": body})
        ref = path.rsplit("/", 1)[-1]
        if ref in self.behaviors:
            self.behaviors[ref]["name"] = body.get("name", self.behaviors[ref]["name"])
            if "color" in body:
                self.behaviors[ref]["color"] = body["color"]
        return self.behaviors.get(ref, body)

    def post(self, path: str, **kwargs: Any) -> Any:
        body = kwargs.get("json_body") or {}
        self.posts.append({"path": path, "body": body})
        if path.endswith("/behaviors") and "workitemtypesbehaviors" not in path:
            ref = f"Custom.{body['name'].replace(' ', '')}"
            created = {
                "name": body["name"],
                "referenceName": ref,
                "color": body.get("color"),
            }
            self.behaviors[ref] = created
            return created
        if "/workitemtypesbehaviors/" in path:
            wit_ref = path.split("/workitemtypesbehaviors/")[1].split("/behaviors")[0]
            entry = {
                "behavior": {"id": body["behavior"]["id"]},
                "isDefault": bool(body.get("isDefault")),
            }
            self.wit_behaviors.setdefault(wit_ref, []).append(entry)
            return entry
        return body

    def delete(self, path: str, **_kwargs: Any) -> Any:
        self.deletes.append(path)
        if "/workitemtypesbehaviors/" in path and "/behaviors/" in path:
            parts = path.split("/workitemtypesbehaviors/")[1]
            wit_ref, rest = parts.split("/behaviors/", 1)
            behavior_ref = rest
            current = self.wit_behaviors.get(wit_ref, [])
            self.wit_behaviors[wit_ref] = [
                cb
                for cb in current
                if str((cb.get("behavior") or {}).get("id") or "") != behavior_ref
            ]
            return None
        # DELETE /_apis/work/processes/{id}/behaviors/{ref}
        if "/behaviors/" in path:
            ref = path.rsplit("/", 1)[-1]
            self.behaviors.pop(ref, None)
        return None


TEMPLATE_BACKLOGS = [
    {
        "name": "Highest",
        "type": "Portfolio backlog",
        "color": "FF69B4",
        "default_wit": "Functional area",
        "rename_from": "(new)",
    },
    {
        "name": "Kanban",
        "type": "Portfolio backlog",
        "color": "007ACC",
        "default_wit": "Test Case",
        "rename_from": "(new)",
    },
    {
        "name": "Levels",
        "type": "Portfolio backlog",
        "color": "",
        "default_wit": "Process",
        "rename_from": "Epic",
    },
    {
        "name": "Lowest",
        "type": "Portfolio backlog",
        "color": "",
        "default_wit": "Scenario",
        "rename_from": "Feature",
    },
    {
        "name": "Deliverables",
        "type": "Requirements backlog",
        "color": "",
        "default_wit": "Configuration",
        "rename_from": "(rename)",
    },
    {
        "name": "Tasks",
        "type": "Iteration backlog",
        "color": "",
        "default_wit": "Task",
        "rename_from": "(rename)",
    },
]


def test_configure_backlog_levels_renames_and_creates() -> None:
    client = BehaviorFakeClient(
        behaviors=[
            {
                "name": "Epics",
                "referenceName": "Microsoft.VSTS.Agile.EpicBacklogBehavior",
            },
            {
                "name": "Features",
                "referenceName": "Microsoft.VSTS.Agile.FeatureBacklogBehavior",
            },
            {
                "name": "Stories",
                "referenceName": "System.RequirementBacklogBehavior",
            },
            {
                "name": "Tasks",
                "referenceName": "System.TaskBacklogBehavior",
            },
        ],
        wits=[],
    )
    stats: dict[str, Any] = {"behaviors": 0, "warnings": 0}
    events: list[str] = []
    refs = _configure_backlog_levels(
        client,
        {"process": {"backlogs": TEMPLATE_BACKLOGS}},
        "proc-1",
        stats,
        lambda _s, m: events.append(m),
    )

    assert refs["Levels"] == "Microsoft.VSTS.Agile.EpicBacklogBehavior"
    assert refs["Lowest"] == "Microsoft.VSTS.Agile.FeatureBacklogBehavior"
    assert refs["Deliverables"] == "System.RequirementBacklogBehavior"
    assert refs["Tasks"] == "System.TaskBacklogBehavior"
    assert refs["Highest"].startswith("Custom.")
    assert refs["Kanban"].startswith("Custom.")
    assert stats["behaviors"] == 6

    put_names = {p["body"]["name"] for p in client.puts}
    assert put_names == {"Levels", "Lowest", "Deliverables"}
    # Tasks already named correctly — no PUT required for that row.
    assert "Tasks" not in put_names
    assert any("Renamed behavior 'Epics' → 'Levels'" in m for m in events)
    assert any("Created backlog behavior 'Highest'" in m for m in events)


def test_configure_heals_duplicate_levels_left_beside_epics() -> None:
    """Prior buggy apply created custom Levels while leaving system Epics."""
    client = BehaviorFakeClient(
        behaviors=[
            {
                "name": "Levels",
                "referenceName": "Custom.Levels",
            },
            {
                "name": "Epics",
                "referenceName": "Microsoft.VSTS.Agile.EpicBacklogBehavior",
            },
            {
                "name": "Lowest",
                "referenceName": "Custom.Lowest",
            },
            {
                "name": "Features",
                "referenceName": "Microsoft.VSTS.Agile.FeatureBacklogBehavior",
            },
            {
                "name": "Stories",
                "referenceName": "System.RequirementBacklogBehavior",
            },
            {
                "name": "Tasks",
                "referenceName": "System.TaskBacklogBehavior",
            },
        ],
        wits=[],
    )
    stats: dict[str, Any] = {"behaviors": 0, "warnings": 0}
    events: list[str] = []
    refs = _configure_backlog_levels(
        client,
        {"process": {"backlogs": TEMPLATE_BACKLOGS}},
        "proc-1",
        stats,
        lambda _s, m: events.append(m),
    )

    assert refs["Levels"] == "Microsoft.VSTS.Agile.EpicBacklogBehavior"
    assert refs["Lowest"] == "Microsoft.VSTS.Agile.FeatureBacklogBehavior"
    assert "Custom.Levels" not in client.behaviors
    assert "Custom.Lowest" not in client.behaviors
    assert client.behaviors["Microsoft.VSTS.Agile.EpicBacklogBehavior"]["name"] == "Levels"
    assert client.behaviors["Microsoft.VSTS.Agile.FeatureBacklogBehavior"]["name"] == "Lowest"
    assert any("Removing duplicate custom backlog 'Levels'" in m for m in events)
    assert any("Renamed behavior 'Epics' → 'Levels'" in m for m in events)
    # No leftover Epics display name.
    assert all(b.get("name") != "Epics" for b in client.behaviors.values())


def test_rename_from_and_markers_ignore_case() -> None:
    client = BehaviorFakeClient(
        behaviors=[
            # Display name changed by hand; found through the known Agile reference.
            {"name": "Big rocks", "referenceName": "Microsoft.VSTS.Agile.EpicBacklogBehavior"},
            {"name": "Features", "referenceName": "Microsoft.VSTS.Agile.FeatureBacklogBehavior"},
        ],
        wits=[],
    )
    stats: dict[str, Any] = {"behaviors": 0, "warnings": 0}
    backlogs = [
        {"name": "Levels", "type": "Portfolio backlog", "rename_from": "epic"},
        {"name": "Lowest", "type": "Portfolio backlog", "rename_from": "FEATURE"},
        {"name": "Kanban", "type": "Portfolio backlog", "rename_from": "(New)"},
    ]
    refs = _configure_backlog_levels(client, {"process": {"backlogs": backlogs}}, "proc-1", stats, lambda *_: None)

    assert refs["Levels"] == "Microsoft.VSTS.Agile.EpicBacklogBehavior"
    assert refs["Lowest"] == "Microsoft.VSTS.Agile.FeatureBacklogBehavior"
    assert refs["Kanban"] == "Custom.Kanban"
    assert stats == {"behaviors": 3, "warnings": 0}


def test_rename_source_that_is_missing_creates_the_portfolio_level() -> None:
    client = BehaviorFakeClient(behaviors=[], wits=[])
    stats: dict[str, Any] = {"behaviors": 0, "warnings": 0}
    events: list[str] = []
    failures: list[str] = []
    backlogs = [{"name": "Levels", "type": "Portfolio backlog", "color": "112233", "rename_from": "Epic"}]
    refs = _configure_backlog_levels(
        client, {"process": {"backlogs": backlogs}}, "proc-1", stats, lambda _s, m: events.append(m), failures=failures
    )

    assert refs == {"Levels": "Custom.Levels"}
    [post] = client.posts
    assert post["body"] == {"name": "Levels", "inherits": "System.PortfolioBacklogBehavior", "color": "112233"}
    assert stats == {"behaviors": 1, "warnings": 1} and failures == []
    assert any("creating 'Levels' as a new portfolio backlog level instead" in m for m in events)


def test_missing_rename_source_for_a_non_portfolio_level_fails_the_step() -> None:
    from app.writers.backlog import apply_backlog
    from app.writers.process import StepSetupError

    client = BehaviorFakeClient(behaviors=[], wits=[])
    ir = {
        "targets": {"project_name": "Proj", "process_name": "Demo"},
        "counts": {},
        "process": {
            "work_item_types": [],
            "backlogs": [{"name": "Deliverables", "type": "Requirements backlog", "rename_from": "Stories"}],
        },
        "project": {"iteration_paths": [], "teams": []},
    }
    with pytest.raises(StepSetupError) as caught:
        apply_backlog(client, ir, dry_run=False, emit=lambda *_: None, process_id="proc-1")
    message = str(caught.value)
    assert "Backlog step: 1 change(s)" in message
    assert "backlog level 'Deliverables' wasn't created: its Rename from 'Stories'" in message
    assert client.posts == []


def test_assign_wits_skips_disabled_and_maps_enabled() -> None:
    client = BehaviorFakeClient(
        behaviors=[],
        wits=[
            {"name": "Process", "referenceName": "MSBPC.Process"},
            {"name": "Epic", "referenceName": "MSBPC.Epic"},
            {"name": "Configuration", "referenceName": "MSBPC.Configuration"},
        ],
    )
    # Seed Epic still on old Epics behavior so assign would rewire if not skipped.
    client.wit_behaviors["MSBPC.Epic"] = [
        {"behavior": {"id": "Microsoft.VSTS.Agile.EpicBacklogBehavior"}, "isDefault": True}
    ]
    client.wit_behaviors["MSBPC.Process"] = [
        {"behavior": {"id": "Microsoft.VSTS.Agile.EpicBacklogBehavior"}, "isDefault": False}
    ]

    stats: dict[str, Any] = {"wit_assignments": 0, "warnings": 0}
    events: list[str] = []
    backlog_refs = {
        "Levels": "Microsoft.VSTS.Agile.EpicBacklogBehavior",
        "Deliverables": "System.RequirementBacklogBehavior",
    }
    ir = {
        "process": {
            "work_item_types": [
                {
                    "name": "Epic",
                    "disabled": True,
                    "backlog_name": "Levels",
                },
                {
                    "name": "Process",
                    "disabled": False,
                    "backlog_name": "Levels",
                },
                {
                    "name": "Configuration",
                    "disabled": False,
                    "backlog_name": "Deliverables",
                },
                {
                    "name": "Bug",
                    "disabled": False,
                    "backlog_name": "No associated backlog",
                },
            ]
        }
    }
    _assign_wits_to_backlogs(
        client, ir, "proc-1", backlog_refs, stats, lambda _s, m: events.append(m)
    )

    assert any("Skipping 'Epic' — disabled WIT" in m for m in events)
    assert stats["wit_assignments"] == 1  # Configuration only (Process already on Levels)
    assert any(p for p in client.posts if "MSBPC.Configuration" in p["path"])
    assert client.wit_behaviors["MSBPC.Configuration"][0]["behavior"]["id"] == (
        "System.RequirementBacklogBehavior"
    )
    # Disabled Epic must not be reassigned.
    assert not any("MSBPC.Epic" in p["path"] for p in client.posts)


def test_set_default_wits_marks_is_default() -> None:
    client = BehaviorFakeClient(
        behaviors=[],
        wits=[
            {"name": "Process", "referenceName": "MSBPC.Process"},
            {"name": "Configuration", "referenceName": "MSBPC.Configuration"},
        ],
    )
    client.wit_behaviors["MSBPC.Process"] = [
        {
            "behavior": {"id": "Microsoft.VSTS.Agile.EpicBacklogBehavior"},
            "isDefault": False,
        }
    ]
    stats: dict[str, Any] = {"default_wits": 0, "warnings": 0}
    events: list[str] = []
    _set_default_wits(
        client,
        {
            "process": {
                "backlogs": [
                    {
                        "name": "Levels",
                        "default_wit": "Process",
                        "rename_from": "Epic",
                    }
                ]
            }
        },
        "proc-1",
        {"Levels": "Microsoft.VSTS.Agile.EpicBacklogBehavior"},
        stats,
        lambda _s, m: events.append(m),
    )
    assert stats["default_wits"] == 1
    assert client.wit_behaviors["MSBPC.Process"][0]["isDefault"] is True
    assert any("Set 'Process' as default for 'Levels'" in m for m in events)


LEVELS = "Microsoft.VSTS.Agile.EpicBacklogBehavior"


def _default_ir(default_wit: str = "Process") -> dict[str, Any]:
    return {"process": {"backlogs": [{"name": "Levels", "default_wit": default_wit, "rename_from": "Epic"}]}}


def test_default_wit_is_updated_in_place_without_leaving_its_backlog_level() -> None:
    client = BehaviorFakeClient(behaviors=[], wits=[{"name": "Process", "referenceName": "MSBPC.Process"}])
    client.wit_behaviors["MSBPC.Process"] = [{"behavior": {"id": LEVELS}, "isDefault": False}]
    stats: dict[str, Any] = {"default_wits": 0, "warnings": 0}
    _set_default_wits(client, _default_ir(), "proc-1", {"Levels": LEVELS}, stats, lambda *_: None)

    assert client.deletes == [] and client.posts == []
    [patch] = client.patches
    assert patch["path"].endswith("/workitemtypesbehaviors/MSBPC.Process/behaviors")
    assert patch["body"] == {"behavior": {"id": LEVELS}, "isDefault": True}
    assert client.wit_behaviors["MSBPC.Process"] == [{"behavior": {"id": LEVELS}, "isDefault": True}]
    assert stats["default_wits"] == 1


def test_default_wit_falls_back_to_remove_and_re_add_when_update_is_refused() -> None:
    client = BehaviorFakeClient(behaviors=[], wits=[{"name": "Process", "referenceName": "MSBPC.Process"}])
    client.wit_behaviors["MSBPC.Process"] = [{"behavior": {"id": LEVELS}, "isDefault": False}]
    client.reject_update = True
    stats: dict[str, Any] = {"default_wits": 0, "warnings": 0}
    _set_default_wits(client, _default_ir(), "proc-1", {"Levels": LEVELS}, stats, lambda *_: None)

    assert len(client.deletes) == 1
    assert client.wit_behaviors["MSBPC.Process"] == [{"behavior": {"id": LEVELS}, "isDefault": True}]
    assert stats["default_wits"] == 1 and stats["warnings"] == 0


def test_backlog_wits_resolve_to_the_process_copy_when_the_system_entry_is_listed_last() -> None:
    local = {
        "name": "Bug",
        "referenceName": "MSBPC.Bug",
        "customization": "inherited",
        "inherits": "Microsoft.VSTS.WorkItemTypes.Bug",
    }
    system = {"name": "Bug", "referenceName": "Microsoft.VSTS.WorkItemTypes.Bug", "customization": "system"}
    client = BehaviorFakeClient(behaviors=[], wits=[local, system])
    stats: dict[str, Any] = {"wit_assignments": 0, "default_wits": 0, "warnings": 0}
    ir = {
        "process": {
            "work_item_types": [
                {"name": "Bug", "reference_name": "Microsoft.VSTS.WorkItemTypes.Bug", "backlog_name": "Levels"}
            ],
            "backlogs": [{"name": "Levels", "default_wit": "Bug"}],
        }
    }
    _assign_wits_to_backlogs(client, ir, "proc-1", {"Levels": LEVELS}, stats, lambda *_: None)
    _set_default_wits(client, ir, "proc-1", {"Levels": LEVELS}, stats, lambda *_: None)

    assert [p["path"].split("/workitemtypesbehaviors/")[1] for p in client.posts] == ["MSBPC.Bug/behaviors"]
    assert client.wit_behaviors["MSBPC.Bug"] == [{"behavior": {"id": LEVELS}, "isDefault": True}]
    assert "Microsoft.VSTS.WorkItemTypes.Bug" not in client.wit_behaviors


def test_apply_backlog_reads_the_wit_list_and_each_wit_behaviors_once() -> None:
    from app.writers.backlog import apply_backlog

    client = BehaviorFakeClient(
        behaviors=[{"name": "Levels", "referenceName": LEVELS}],
        wits=[{"name": "Process", "referenceName": "MSBPC.Process"}],
    )
    client.wit_behaviors["MSBPC.Process"] = [{"behavior": {"id": LEVELS}, "isDefault": True}]
    ir = {
        "targets": {"project_name": "Proj", "process_name": "Demo"},
        "counts": {},
        "process": {
            "work_item_types": [{"name": "Process", "backlog_name": "Levels"}],
            "backlogs": [{"name": "Levels", "default_wit": "Process"}],
        },
        "project": {"iteration_paths": [], "teams": []},
    }
    apply_backlog(client, ir, dry_run=False, emit=lambda *_: None, process_id="proc-1")

    assert client.wit_list_reads == 1
    assert sum("workitemtypesbehaviors/MSBPC.Process" in path for path in client.gets) == 1
    assert client.posts == [] and client.patches == [] and client.deletes == []
