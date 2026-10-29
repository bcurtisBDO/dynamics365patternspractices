"""Cross-site write protection, network-path rejection and unreadable-workbook errors."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import jobs, main
from app.main import app

VALID = {
    "org_url": "https://dev.azure.com/contoso",
    "process_name": "Demo Process",
    "project_name": "Demo Project",
    "excel_file": "/tmp/template.xlsx",
}


def _call(
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    raw_headers = [(b"host", b"127.0.0.1")]
    if body is not None:
        raw_headers.append((b"content-length", str(len(body)).encode()))
    raw_headers += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw_headers,
        "server": ("127.0.0.1", 8787),
        "client": ("127.0.0.1", 50000),
    }
    messages: list[dict] = []
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(payload) if payload else {}


def _post_json(path: str, body: dict, **headers: str) -> tuple[int, dict]:
    return _call(
        "POST",
        path,
        json.dumps(body).encode(),
        {"content-type": "application/json", **headers},
    )


@pytest.fixture
def no_dry_run(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    monkeypatch.setattr(main, "run_dry_run", lambda request: calls.append(request))
    monkeypatch.setattr(main, "build_ir", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(jobs, "run_dry_run", lambda request: calls.append(request))
    return calls


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "http://localhost.evil.example", "null", "http://127.0.0.1.nip.io:5173"],
)
def test_cross_site_origin_is_refused(origin: str, no_dry_run: list) -> None:
    status, payload = _post_json("/plan/dry-run", VALID, origin=origin)
    assert status == 403
    assert "Cross-site" in payload["detail"]
    assert no_dry_run == []


def test_cross_site_fetch_metadata_is_refused(no_dry_run: list) -> None:
    status, _ = _post_json("/jobs", VALID, **{"sec-fetch-site": "cross-site"})
    assert status == 403
    assert no_dry_run == []


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_simple_post_bodies_are_refused(content_type: str | None, no_dry_run: list) -> None:
    headers = {"content-type": content_type} if content_type else {}
    status, payload = _call("POST", "/jobs", json.dumps(VALID).encode(), headers)
    assert status == 415
    assert "application/json" in payload["detail"]
    assert no_dry_run == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://[::1]:5173",
        "https://localhost:4173",
        "http://localhost",
    ],
)
def test_local_ui_origins_are_allowed(origin: str) -> None:
    # 422: the request reached validation, past the cross-site check.
    status, _ = _post_json(
        "/plan/dry-run", {"excel_file": ""}, origin=origin, **{"sec-fetch-site": "same-origin"}
    )
    assert status == 422


def test_requests_without_origin_and_bodyless_posts_are_allowed() -> None:
    assert _post_json("/plan/dry-run", {"excel_file": ""})[0] == 422
    status, _ = _call("POST", "/jobs/missing-job/cancel", headers={"origin": "http://localhost:5173"})
    assert status == 404


def test_reads_are_not_checked() -> None:
    status, payload = _call("GET", "/health", headers={"origin": "https://evil.example"})
    assert status == 200
    assert payload["pid"] > 0
    assert "instance" in payload


NETWORK_FIELDS = [
    ("excel_file", r"\\attacker\share\template.xlsx"),
    ("catalog_source_dir", "//attacker/share/catalog"),
    ("output_root", r"\\?\UNC\attacker\share\out"),
]


@pytest.mark.parametrize("path", ["/plan/dry-run", "/ir/parse", "/jobs"])
@pytest.mark.parametrize("field,value", NETWORK_FIELDS)
def test_network_paths_are_rejected(path: str, field: str, value: str, no_dry_run: list) -> None:
    status, payload = _post_json(path, {**VALID, field: value})
    assert status == 422
    assert any(
        e["loc"] == ["body", field] and "Network paths" in e["msg"] for e in payload["detail"]
    )
    assert no_dry_run == []


def _locked(*_args, **_kwargs):
    raise PermissionError(13, "Permission denied", "/Users/me/BPC/template.xlsx")


@pytest.mark.parametrize("path,target", [("/plan/dry-run", "run_dry_run"), ("/ir/parse", "build_ir")])
def test_unreadable_workbook_answers_400(path: str, target: str, monkeypatch) -> None:
    monkeypatch.setattr(main, target, _locked)
    status, payload = _post_json(path, VALID)
    assert status == 400
    assert payload["detail"].startswith("Can't open /Users/me/BPC/template.xlsx: permission denied")
    assert "Errno" not in payload["detail"]


def test_other_os_errors_answer_400(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_request):
        raise OSError(5, "Input/output error", "/Volumes/usb/template.xlsx")

    monkeypatch.setattr(main, "run_dry_run", broken)
    status, payload = _post_json("/plan/dry-run", VALID)
    assert status == 400
    assert payload["detail"] == "Can't read /Volumes/usb/template.xlsx: Input/output error."


def test_unreadable_workbook_in_a_dry_run_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "run_dry_run", _locked)
    status, payload = _post_json("/jobs", VALID)
    assert status == 200
    assert payload["status"] == "failed"
    assert payload["error"].startswith("Can't open /Users/me/BPC/template.xlsx: permission denied")
    assert "Errno" not in payload["events"][-1]["message"]
