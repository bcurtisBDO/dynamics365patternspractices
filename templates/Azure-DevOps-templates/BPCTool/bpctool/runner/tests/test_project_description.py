"""Project description from UI/request is applied on create and update."""

from __future__ import annotations

from typing import Any

import pytest

from app.ado_client import AdoError
from app.writers.project import apply_project


class FakeClient:
    def __init__(
        self,
        *,
        existing_id: str | None = None,
        existing_process: str = "proc-guid",
        existing_description: str = "",
    ) -> None:
        self.existing_id = existing_id
        self.existing_process = existing_process
        self.existing_description = existing_description
        self.created: dict[str, Any] | None = None
        self.updated: dict[str, Any] | None = None
        self.should_cancel: Any = None

    def get(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        assert path == f"/_apis/projects/{self.existing_id}"
        return {"id": self.existing_id, "description": self.existing_description}

    def get_process_id(self, _name: str) -> str:
        return "proc-guid"

    def get_ready_project_id(self, _name: str, *, should_cancel: Any = None, on_wait: Any = None) -> str | None:
        self.should_cancel = should_cancel
        return self.existing_id

    def get_project_process_id(self, _project_id: str) -> str:
        return self.existing_process

    def list_processes(self) -> list[dict[str, Any]]:
        return [{"typeId": "proc-guid", "name": "MSBPCTemplate"}, {"typeId": "agile-guid", "name": "Agile"}]

    def create_project(
        self, project_name: str, process_id: str, *, description: str = "", should_cancel: Any = None
    ) -> str:
        self.should_cancel = should_cancel
        self.created = {
            "name": project_name,
            "process_id": process_id,
            "description": description,
        }
        return "proj-new"

    def update_project_description(self, project_id: str, description: str) -> None:
        self.updated = {"project_id": project_id, "description": description}


def test_create_project_uses_user_description() -> None:
    client = FakeClient()
    events: list[str] = []
    ir = {
        "targets": {
            "project_name": "MSBPCProj",
            "process_name": "MSBPCTemplate",
            "project_description": "Microsoft Business Process Catalog project",
        }
    }
    stats = apply_project(client, ir, dry_run=False, emit=lambda _s, m: events.append(m))
    assert stats["created"] is True
    assert client.created is not None
    assert client.created["description"] == "Microsoft Business Process Catalog project"
    assert "Project for process" not in (client.created["description"] or "")
    assert any("description set" in m for m in events)


def test_create_project_empty_description_not_process_id() -> None:
    client = FakeClient()
    ir = {
        "targets": {
            "project_name": "MSBPCProj",
            "process_name": "MSBPCTemplate",
            "project_description": "",
        }
    }
    apply_project(client, ir, dry_run=False, emit=lambda _s, _m: None)
    assert client.created is not None
    assert client.created["description"] == ""


def test_existing_project_updates_description_when_provided() -> None:
    client = FakeClient(existing_id="proj-existing")
    events: list[str] = []
    ir = {
        "targets": {
            "project_name": "MSBPCProj",
            "process_name": "MSBPCTemplate",
            "project_description": "Updated overview text",
        }
    }
    stats = apply_project(client, ir, dry_run=False, emit=lambda _s, m: events.append(m))
    assert stats["existed"] is True
    assert stats["description_updated"] is True
    assert client.updated == {
        "project_id": "proj-existing",
        "description": "Updated overview text",
    }
    assert client.created is None


def test_failed_description_update_is_a_step_warning() -> None:
    from app.run_outcome import describe_step

    class DescriptionRefused(FakeClient):
        def update_project_description(self, project_id: str, description: str) -> None:
            raise AdoError("ADO 403 Forbidden: no permission", status=403)

    client = DescriptionRefused(existing_id="proj-existing")
    ir = {"targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate", "project_description": "New"}}
    stats = apply_project(client, ir, dry_run=False, emit=lambda _s, _m: None)
    assert stats["description_updated"] is False
    assert stats["warnings"] == 1
    status, text = describe_step("project", stats, dry_run=False)
    assert status == "warning"
    assert "description could not be updated: ADO 403 Forbidden" in text


def test_existing_project_on_another_process_is_rejected() -> None:
    client = FakeClient(existing_id="proj-existing", existing_process="AGILE-GUID")
    ir = {"targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate"}}
    with pytest.raises(AdoError, match='uses process "Agile", not "MSBPCTemplate"'):
        apply_project(client, ir, dry_run=False, emit=lambda _s, _m: None)
    assert client.updated is None


def test_unchanged_description_is_not_sent_again() -> None:
    client = FakeClient(existing_id="proj-existing", existing_description="Same text")
    events: list[str] = []
    ir = {"targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate", "project_description": "Same text"}}
    stats = apply_project(client, ir, dry_run=False, emit=lambda _s, m: events.append(m))
    assert client.updated is None
    assert stats["description_updated"] is False and "warnings" not in stats
    assert any("already matches" in m for m in events)


def test_cancel_check_is_passed_to_project_creation() -> None:
    client = FakeClient()
    ir = {"targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate"}}

    def cancel() -> bool:
        return False

    apply_project(client, ir, dry_run=False, emit=lambda _s, _m: None, should_cancel=cancel)
    assert client.should_cancel is cancel


def test_existing_project_still_being_created_is_waited_for() -> None:
    class StillCreating(FakeClient):
        def get_ready_project_id(self, _name: str, *, should_cancel: Any = None, on_wait: Any = None) -> str:
            on_wait("createPending")
            return "proj-existing"

    client = StillCreating(existing_id="proj-existing")
    events: list[str] = []
    ir = {"targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate"}}
    stats = apply_project(client, ir, dry_run=False, emit=lambda _s, m: events.append(m))
    assert stats["existed"] is True and stats["project_id"] == "proj-existing"
    assert any("still being set up" in m and "createPending" in m for m in events)


def test_description_is_sent_when_it_cannot_be_read() -> None:
    class Unreadable(FakeClient):
        def get(self, path: str, **_kwargs: Any) -> dict[str, Any]:
            raise AdoError("ADO 500", status=500)

    client = Unreadable(existing_id="proj-existing")
    ir = {"targets": {"project_name": "MSBPCProj", "process_name": "MSBPCTemplate", "project_description": "New"}}
    stats = apply_project(client, ir, dry_run=False, emit=lambda _s, _m: None)
    assert client.updated == {"project_id": "proj-existing", "description": "New"}
    assert stats["description_updated"] is True
