"""Cancel stops Process / Layout / Teams / Backlog at their next Azure DevOps request."""

from __future__ import annotations

import pytest

from app import apply_engine
from app.ado_client import AdoClient, RunCancelled

PAT = "x" * 52


def test_before_request_can_stop_a_request_before_it_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AdoClient("https://dev.azure.com/contoso", PAT)
    sent: list[str] = []
    monkeypatch.setattr(client, "_send", lambda method, *_a: sent.append(method) or (200, "", {}, b"{}"))

    client.get("/_apis/projects")
    cancelled = {"now": False}

    def check() -> None:
        if cancelled["now"]:
            raise RunCancelled("Run cancelled by user")

    client.before_request = check
    client.get("/_apis/projects")
    cancelled["now"] = True
    with pytest.raises(RunCancelled):
        client.post("/_apis/work/processes", json_body={})
    assert sent == ["GET", "GET"]


def test_catalog_and_project_keep_their_own_cancel_handling() -> None:
    assert apply_engine.CANCEL_BETWEEN_REQUESTS == {"process", "layout", "teams", "backlog"}
