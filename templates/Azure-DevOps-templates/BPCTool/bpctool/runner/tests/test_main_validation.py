"""Request validation errors must not echo the submitted values (they include the PAT)."""

from __future__ import annotations

import asyncio
import json

from app.main import app

PAT = "s3cr3t-pat-value-" + "x" * 30


def _post(path: str, body: dict) -> tuple[int, dict]:
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json")],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
    }
    messages: list[dict] = []
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_invalid_job_request_does_not_echo_the_pat() -> None:
    status, payload = _post(
        "/jobs",
        # A missing field's error carries the whole body as its input.
        {"org_url": "x", "process_name": "BPC", "project_name": "Proj", "pat": PAT, "steps": ["nope"]},
    )
    assert status == 422
    assert PAT not in json.dumps(payload)
    errors = payload["detail"]
    assert {tuple(e) for e in map(sorted, errors)} == {("loc", "msg", "type")}
    assert {"loc": ["body", "excel_file"], "msg": "Field required", "type": "missing"} in errors
    assert ["body", "org_url"] in [e["loc"] for e in errors]
    assert any("Unknown step" in e["msg"] for e in errors)


def test_pat_of_the_wrong_type_is_not_echoed() -> None:
    status, payload = _post("/plan/dry-run", {"pat": {"token": PAT}})
    assert status == 422
    assert PAT not in json.dumps(payload)
    assert any(e["loc"] == ["body", "pat"] for e in payload["detail"])
