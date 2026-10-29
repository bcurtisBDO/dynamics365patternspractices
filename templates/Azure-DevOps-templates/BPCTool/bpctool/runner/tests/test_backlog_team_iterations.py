"""Team iteration assignment (reference Script 4 Part 4)."""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import unquote

import pytest

from app.ado_client import AdoError
from app.writers.backlog import (
    _collect_iteration_ids,
    _index_iteration_tree,
    _lookup_iteration,
    _team_settings_payload,
    apply_backlog,
)
from app.writers.process import StepSetupError


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.tree = {
            "name": "MSBPCProj",
            "identifier": "root-guid",
            "children": [
                {
                    "name": "Phase 1 - Initiate",
                    "identifier": "phase1-guid",
                    "children": [
                        {"name": "Sprint 1", "identifier": "s1-guid", "children": []},
                    ],
                }
            ],
        }
        self.team_iters: dict[str, list[str]] = {}

    def get_process_id(self, _name: str) -> str:
        return "proc-1"

    def list_process_wits(self, _process_id: str) -> list[dict[str, Any]]:
        return []

    def get(self, path: str, **kwargs: Any) -> Any:
        project = kwargs.get("project") or ""
        if "behaviors" in path:
            return {"value": []}
        lowered = path.lower()
        if "classificationnodes/iterations" in lowered and (
            "$depth" in lowered or "%24depth" in lowered
        ):
            return self.tree
        if path == "/_apis/work/teamsettings":
            return {
                "bugsBehavior": "off",
                # Unset backlog iteration (matches ADO empty Backlog iteration UI).
                "backlogIteration": {"id": "00000000-0000-0000-0000-000000000000"},
                "defaultIterationMacro": "",
            }
        if path == "/_apis/work/teamsettings/iterations":
            return {"value": [{"id": x} for x in self.team_iters.get(project, [])]}
        if path == "/_apis/work/teamsettings/teamfieldvalues":
            return {
                "defaultValue": "MSBPCProj\\Business process catalog",
                "values": [
                    {
                        "value": "MSBPCProj\\Business process catalog",
                        "includeChildren": False,
                    }
                ],
            }
        return {}

    def post(self, path: str, **kwargs: Any) -> Any:
        self.posts.append({"path": path, "project": kwargs.get("project"), "body": kwargs.get("json_body")})
        if "classificationnodes" in path.lower():
            return {"ok": True}
        if path == "/_apis/work/teamsettings/iterations":
            project = kwargs.get("project") or ""
            self.team_iters.setdefault(project, []).append(kwargs["json_body"]["id"])
            return {"ok": True}
        return {"ok": True}

    def put(self, path: str, **kwargs: Any) -> Any:
        return {"ok": True}

    def delete(self, path: str, **kwargs: Any) -> Any:
        return None

    def patch(self, path: str, **kwargs: Any) -> Any:
        self.patches.append({"path": path, "project": kwargs.get("project"), "body": kwargs.get("json_body")})
        return {"ok": True}


class BacklogFakeClient(FakeClient):
    def get(self, path: str, **kwargs: Any) -> Any:
        lowered = path.lower()
        if "classificationnodes/iterations" in lowered and "$depth" not in lowered and "%24depth" not in lowered:
            from app.ado_client import AdoError

            raise AdoError("missing", status=404)
        return super().get(path, **kwargs)


def test_collect_iteration_ids_keys_casefolded_paths() -> None:
    result: dict[str, str] = {}
    _collect_iteration_ids(
        {"name": "Phase 1", "identifier": "p1", "children": [{"name": "Sprint 1", "identifier": "s1"}]},
        "",
        result,
    )
    assert result == {"phase 1": "p1", "phase 1\\sprint 1": "s1"}


def test_index_uses_tree_root_identifier_not_project_name_lookup() -> None:
    tree = {
        "name": "DifferentRootName",
        "identifier": "root-guid",
        "children": [{"name": "Phase 1", "identifier": "p1", "children": []}],
    }
    root_id, _map, children = _index_iteration_tree(tree, "MSBPCProj")
    assert root_id == "root-guid"
    assert {c["id"] for c in children} == {"p1"}
    assert "root-guid" not in {c["id"] for c in children}


def test_team_settings_payload_sets_backlog_when_unset() -> None:
    payload = _team_settings_payload(
        {
            "bug_behavior": "asRequirements",
            "backlog_iteration": "@currentIteration",
        },
        {
            "bugsBehavior": "off",
            "backlogIteration": {"id": "00000000-0000-0000-0000-000000000000"},
            "defaultIterationMacro": "",
        },
        "root-guid",
        {},
    )
    assert payload["backlogIteration"] == "root-guid"
    assert payload["defaultIterationMacro"] == "@CurrentIteration"
    assert payload["bugsBehavior"] == "asRequirements"


def test_team_settings_payload_empty_without_root() -> None:
    payload = _team_settings_payload(
        {"bug_behavior": "asRequirements", "backlog_iteration": "@currentIteration"},
        {"bugsBehavior": "off", "defaultIterationMacro": ""},
        None,
        {},
    )
    # Must not set macro alone — that produces the empty Backlog iteration UI state.
    assert payload == {}


def _backlog_ir(team_names: list[str]) -> dict[str, Any]:
    return {
        "targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate"},
        "counts": {"backlogs": 0, "iteration_paths": 2, "teams": len(team_names)},
        "process": {"backlogs": [], "work_item_types": []},
        "project": {
            "iteration_paths": [
                {"path": "Phase 1 - Initiate", "level": "1"},
                {"path": "Phase 1 - Initiate\\Sprint 1", "level": "2"},
            ],
            "teams": [
                {
                    "name": name,
                    "bug_behavior": "asRequirements",
                    "include_sub_areas": "Yes",
                    "backlog_iteration": "@currentIteration",
                }
                for name in team_names
            ],
        },
    }


def test_apply_backlog_parallel_teams_match_serial_counts() -> None:
    names = [f"Team {n}" for n in range(9)]
    serial_client = BacklogFakeClient()
    serial = apply_backlog(serial_client, _backlog_ir(names), dry_run=False, emit=lambda *_: None)
    parallel_client = BacklogFakeClient()
    parallel = apply_backlog(
        parallel_client, _backlog_ir(names), dry_run=False, emit=lambda *_: None, workers=4
    )

    assert parallel == serial
    assert parallel["team_settings"] == 9
    assert parallel["team_iterations"] == 9 * 2
    assert sorted(parallel_client.team_iters) == sorted(f"MSBPCProj/{n}" for n in names)


def test_cancel_stops_queued_teams_when_parallel() -> None:
    class Cancelled(Exception):
        pass

    cancelled = threading.Event()

    def emit(_step: str, message: str) -> None:
        if "Updated team settings" in message:
            cancelled.set()
        if cancelled.is_set():
            raise Cancelled()

    client = BacklogFakeClient()
    with pytest.raises(Cancelled):
        apply_backlog(
            client, _backlog_ir([f"Team {n}" for n in range(12)]), dry_run=False, emit=emit, workers=4
        )
    patched = {p["project"] for p in client.patches if p["path"] == "/_apis/work/teamsettings"}
    # Only teams already in flight when the cancel landed finish their settings PATCH.
    assert 1 <= len(patched) <= 4


def test_skip_existing_process_leaves_backlog_levels_alone() -> None:
    class RecordingClient(BacklogFakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.process_calls: list[str] = []

        def get(self, path: str, **kwargs: Any) -> Any:
            if "/_apis/work/processes" in path:
                self.process_calls.append(path)
            return super().get(path, **kwargs)

        def put(self, path: str, **kwargs: Any) -> Any:
            self.process_calls.append(path)
            return super().put(path, **kwargs)

        def get_process_id(self, _name: str) -> str:
            self.process_calls.append("get_process_id")
            return "proc-1"

    client = RecordingClient()
    events: list[str] = []
    stats = apply_backlog(
        client,
        _backlog_ir(["Accounting"]),
        dry_run=False,
        emit=lambda _s, m: events.append(m),
        process_changes=False,
    )
    assert client.process_calls == []
    assert not any("/_apis/work/processes" in p["path"] for p in client.posts + client.patches)
    assert stats["behaviors"] == 0
    assert stats["team_settings"] == 1
    assert any("left as they are" in m for m in events)


def test_apply_backlog_assigns_iterations_to_each_team() -> None:
    client = BacklogFakeClient()
    events: list[str] = []
    ir = {
        "targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate"},
        "counts": {"backlogs": 0, "iteration_paths": 2, "teams": 2},
        "process": {"backlogs": [], "work_item_types": []},
        "project": {
            "iteration_paths": [
                {"path": "Phase 1 - Initiate", "level": "1"},
                {"path": "Phase 1 - Initiate\\Sprint 1", "level": "2"},
            ],
            "teams": [
                {
                    "name": "Accounts payable",
                    "bug_behavior": "asRequirements",
                    "include_sub_areas": "Yes",
                    "backlog_iteration": "@currentIteration",
                },
                {
                    "name": "Accounting",
                    "bug_behavior": "asRequirements",
                    "include_sub_areas": "Yes",
                    "backlog_iteration": "@currentIteration",
                },
            ],
        },
    }
    stats = apply_backlog(client, ir, dry_run=False, emit=lambda _s, m: events.append(m))
    assert stats["team_settings"] == 2
    assert stats["team_iterations"] >= 2
    settings_patches = [p for p in client.patches if p["path"] == "/_apis/work/teamsettings"]
    assert settings_patches
    assert settings_patches[0]["body"]["backlogIteration"] == "root-guid"
    iter_posts = [p for p in client.posts if p["path"] == "/_apis/work/teamsettings/iterations"]
    # Root GUID must never be POSTed as a team iteration (TF400494).
    assert all(p["body"]["id"] != "root-guid" for p in iter_posts)
    assert any("backlogIteration=root-guid" in m for m in events)


class SpecificBacklogClient(BacklogFakeClient):
    """Team whose Backlog iteration is already a specific (non-root) iteration."""

    def get(self, path: str, **kwargs: Any) -> Any:
        if path == "/_apis/work/teamsettings":
            return {
                "bugsBehavior": "asRequirements",
                "backlogIteration": {"id": "phase1-guid"},
                "defaultIterationMacro": "",
            }
        return super().get(path, **kwargs)


def _team_ir(backlog_iteration: str) -> dict[str, Any]:
    ir = _backlog_ir(["Accounting"])
    ir["project"]["teams"][0].update(backlog_iteration=backlog_iteration, include_sub_areas="No")
    return ir


def test_team_with_a_specific_backlog_iteration_still_gets_its_iterations() -> None:
    client = SpecificBacklogClient()
    events: list[str] = []
    stats = apply_backlog(
        client,
        _team_ir("MSBPCProj\\Phase 1 - Initiate"),
        dry_run=False,
        emit=lambda _s, m: events.append(m),
    )
    assert not [p for p in client.patches if p["path"] == "/_apis/work/teamsettings"]
    assert stats["team_iterations"] == 2 and stats["warnings"] == 0
    assert not any("backlog iteration not set" in m for m in events)


class FailingSettingsPatch:
    def patch(self, path: str, **kwargs: Any) -> Any:
        if path == "/_apis/work/teamsettings":
            raise AdoError("ADO 400 Bad Request: nope", status=400)
        return super().patch(path, **kwargs)  # type: ignore[misc]


def test_failed_backlog_iteration_change_keeps_the_existing_one_and_adds_iterations() -> None:
    class Client(FailingSettingsPatch, SpecificBacklogClient):
        pass

    client = Client()
    events: list[str] = []
    stats = apply_backlog(
        client, _team_ir("@currentIteration"), dry_run=False, emit=lambda _s, m: events.append(m)
    )
    assert any("FAILED team settings PATCH 'Accounting'" in m for m in events)
    assert not any("backlog iteration not set" in m for m in events)
    assert sorted(client.team_iters["MSBPCProj/Accounting"]) == ["phase1-guid", "s1-guid"]
    assert stats["team_iterations"] == 2


def test_failed_patch_with_no_backlog_iteration_still_skips_iterations() -> None:
    class Client(FailingSettingsPatch, BacklogFakeClient):
        pass

    client = Client()
    events: list[str] = []
    stats = apply_backlog(
        client, _team_ir("@currentIteration"), dry_run=False, emit=lambda _s, m: events.append(m)
    )
    assert any("backlog iteration not set" in m for m in events)
    assert "MSBPCProj/Accounting" not in client.team_iters
    assert stats["team_iterations"] == 0


def test_unknown_backlog_iteration_path_is_a_warning() -> None:
    client = BacklogFakeClient()
    events: list[str] = []
    stats = apply_backlog(
        client, _team_ir("MSBPCProj\\No such sprint"), dry_run=False, emit=lambda _s, m: events.append(m)
    )
    [patch] = [p for p in client.patches if p["path"] == "/_apis/work/teamsettings"]
    assert patch["body"]["backlogIteration"] == "root-guid"
    assert stats["warnings"] == 1
    assert any("Backlog iteration 'MSBPCProj\\No such sprint' isn't an iteration" in m for m in events)


class IterationTreeClient(BacklogFakeClient):
    def __init__(self, *, reject: str = "") -> None:
        super().__init__()
        self.reject = reject
        self.gets: list[str] = []

    def get(self, path: str, **kwargs: Any) -> Any:
        self.gets.append(path)
        return super().get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        if "classificationnodes" in path.lower():
            name = kwargs["json_body"]["name"]
            if name == self.reject:
                raise AdoError("ADO 403 Forbidden: no permission", status=403)
            parent = self.tree
            for part in path.split("/Iterations")[1].strip("/").split("/"):
                if part:
                    parent = next(c for c in parent["children"] if c["name"] == unquote(part))
            parent["children"].append({"name": name, "identifier": f"{name}-guid", "children": []})
        return super().post(path, **kwargs)


def _iterations_ir(paths: list[str]) -> dict[str, Any]:
    ir = _backlog_ir(["Accounting"])
    ir["project"]["iteration_paths"] = [{"path": path} for path in paths]
    return ir


def test_iteration_tree_is_read_once_and_only_missing_nodes_are_created() -> None:
    client = IterationTreeClient()
    stats = apply_backlog(
        client,
        _iterations_ir(["Phase 1 - Initiate\\Sprint 1", "Phase 1 - Initiate\\Sprint 2", "Phase 2\\Sprint 3"]),
        dry_run=False,
        emit=lambda *_: None,
    )
    created = [(p["path"], p["body"]["name"]) for p in client.posts if "classificationnodes" in p["path"]]
    assert created == [
        ("/_apis/wit/classificationnodes/Iterations/Phase%201%20-%20Initiate", "Sprint 2"),
        ("/_apis/wit/classificationnodes/Iterations", "Phase 2"),
        ("/_apis/wit/classificationnodes/Iterations/Phase%202", "Sprint 3"),
    ]
    assert stats["iterations"] == 3
    tree_reads = [g for g in client.gets if "classificationnodes" in g.lower()]
    # Once to find missing nodes, once more for the new nodes' GUIDs; no per-node GETs.
    assert tree_reads == ["/_apis/wit/classificationnodes/Iterations?$depth=10"] * 2
    assert stats["team_iterations"] == 5


def test_rerun_with_all_iterations_present_reads_the_tree_once() -> None:
    client = IterationTreeClient()
    apply_backlog(client, _iterations_ir(["Phase 1 - Initiate\\Sprint 1"]), dry_run=False, emit=lambda *_: None)
    assert [g for g in client.gets if "classificationnodes" in g.lower()] == [
        "/_apis/wit/classificationnodes/Iterations?$depth=10"
    ]


def test_iteration_that_cannot_be_created_fails_the_step_after_the_teams() -> None:
    client = IterationTreeClient(reject="Phase 2")
    events: list[str] = []
    with pytest.raises(StepSetupError) as caught:
        apply_backlog(
            client,
            _iterations_ir(["Phase 2\\Sprint 3", "Phase 2\\Sprint 4", "Phase 1 - Initiate\\Sprint 2"]),
            dry_run=False,
            emit=lambda _s, m: events.append(m),
        )
    message = str(caught.value)
    assert "Backlog step: 1 change(s)" in message
    assert "iteration 'Phase 2' couldn't be created" in message and "403" in message
    # The rest of the step still ran: Sprint 2 created and the team configured.
    assert any(p["body"]["name"] == "Sprint 2" for p in client.posts if "classificationnodes" in p["path"])
    assert client.team_iters["MSBPCProj/Accounting"]


def test_area_only_teams_get_defaults_and_case_duplicates_are_configured_once() -> None:
    ir = _backlog_ir(["Accounting", "ACCOUNTING"])
    ir["project"]["area_paths"] = [
        {"path": "HR", "team": "Payroll"},
        {"path": "HR\\Benefits", "team": "Benefits"},
        {"path": "Fin", "team": "accounting"},
    ]
    client = BacklogFakeClient()
    stats = apply_backlog(client, ir, dry_run=False, emit=lambda *_: None)
    assert sorted(client.team_iters) == ["MSBPCProj/Accounting", "MSBPCProj/Benefits", "MSBPCProj/Payroll"]
    payroll = next(p for p in client.patches if p["project"] == "MSBPCProj/Payroll")
    assert payroll["body"] == {
        "bugsBehavior": "asRequirements",
        "backlogIteration": "root-guid",
        "defaultIterationMacro": "@CurrentIteration",
    }
    assert stats["team_settings"] == 3
    assert apply_backlog(BacklogFakeClient(), ir, dry_run=True, emit=lambda *_: None)["team_settings"] == 3


TWO_PHASE_TREE = {
    "name": "MSBPCProj",
    "identifier": "root-guid",
    "children": [
        {"name": "Phase 1", "identifier": "p1", "children": [{"name": "Sprint 1", "identifier": "p1s1"}]},
        {"name": "Phase 2", "identifier": "p2", "children": [{"name": "Sprint 1", "identifier": "p2s1"}]},
        {"name": "Phase 3", "identifier": "p3", "children": [{"name": "Sprint 9", "identifier": "p3s9"}]},
    ],
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("msbpcproj\\phase 2\\SPRINT 1", "p2s1"),
        ("Phase 1\\Sprint 1", "p1s1"),
        ("Phase 1/Sprint 1", "p1s1"),
        ("Project name\\Phase 3", "p3"),
        ("MSBPCProj", "root-guid"),
        ("sprint 9", "p3s9"),
        ("Phase 9", None),
    ],
)
def test_backlog_iteration_lookup_ignores_case_and_the_project_prefix(path: str, expected: str | None) -> None:
    _root, iteration_map, _children = _index_iteration_tree(TWO_PHASE_TREE, "MSBPCProj")
    assert _lookup_iteration(iteration_map, path, "MSBPCProj") == (expected, [])


def test_ambiguous_bare_backlog_iteration_name_warns_and_uses_the_root() -> None:
    client = BacklogFakeClient()
    client.tree = TWO_PHASE_TREE
    events: list[str] = []
    stats = apply_backlog(client, _team_ir("Sprint 1"), dry_run=False, emit=lambda _s, m: events.append(m))
    [patch] = [p for p in client.patches if p["path"] == "/_apis/work/teamsettings"]
    assert patch["body"]["backlogIteration"] == "root-guid"
    assert stats["warnings"] == 1
    assert any(
        "Backlog iteration 'Sprint 1' matches 2 iterations (MSBPCProj\\Phase 1\\Sprint 1, "
        "MSBPCProj\\Phase 2\\Sprint 1)" in m
        for m in events
    )


def test_iteration_paths_drop_only_one_leading_project_segment() -> None:
    client = IterationTreeClient()
    apply_backlog(
        client,
        _iterations_ir(["msbpcproj\\Phase 2", "MSBPCProj\\MSBPCProj\\Sprint 5"]),
        dry_run=False,
        emit=lambda *_: None,
    )
    created = [(p["path"], p["body"]["name"]) for p in client.posts if "classificationnodes" in p["path"]]
    assert created == [
        ("/_apis/wit/classificationnodes/Iterations", "Phase 2"),
        ("/_apis/wit/classificationnodes/Iterations", "MSBPCProj"),
        ("/_apis/wit/classificationnodes/Iterations/MSBPCProj", "Sprint 5"),
    ]


def test_iteration_check_that_errors_fails_the_step_after_the_rest() -> None:
    class Client(BacklogFakeClient):
        def get(self, path: str, **kwargs: Any) -> Any:
            if "$depth" in path:
                raise AdoError("ADO 500 Internal Server Error", status=500)
            if path.endswith("/Iterations/Phase%202"):
                raise AdoError("ADO 403 Forbidden: no permission", status=403)
            return super().get(path, **kwargs)

    client = Client()
    with pytest.raises(StepSetupError) as caught:
        apply_backlog(
            client, _iterations_ir(["Phase 2\\Sprint 3", "Phase 4"]), dry_run=False, emit=lambda *_: None
        )
    message = str(caught.value)
    assert "Backlog step: 1 change(s)" in message
    assert "iteration 'Phase 2' couldn't be checked" in message and "403" in message
    assert [p["body"]["name"] for p in client.posts if "classificationnodes" in p["path"]] == ["Phase 4"]
