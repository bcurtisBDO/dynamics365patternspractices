"""Catalog import keeps created IDs and stays responsive when files, workers, or Cancel misbehave."""

from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from app import catalog_runtime
from app.ado_client import AdoError
from app.catalog_import import WorkItemDraft
from app.catalog_runtime import (
    append_id_map_row,
    import_drafts_parent_aware,
    load_id_map,
    load_id_map_parents,
    open_id_map_for_append,
)

_STATE_REJECTED = (
    "ADO 400 Bad Request: The field 'State' contains the value 'New' that is not in the list of "
    "supported values (System.State)"
)


def _draft(key: str, *, parent_key: str | None = None, wit: str = "Scenario", row: int = 1) -> WorkItemDraft:
    return WorkItemDraft(
        key=key,
        parent_key=parent_key,
        work_item_type=wit,
        title=key,
        fields={"System.Title": key},
        source_file="bpc.xlsx",
        source_row=row,
    )


def _test_case(key: str, row: int) -> WorkItemDraft:
    draft = _draft(key, wit="Test Case", row=row)
    draft.fields["System.State"] = "New"
    return draft


class _Client:
    """Creates ids 101, 102, ...; Test Case State 'New' is refused on create like Azure DevOps."""

    def __init__(self, *, batch_error: Exception | None = None) -> None:
        self.lock = threading.Lock()
        self.created: list[str] = []
        self.batches: list[list[int]] = []
        self.batch_error = batch_error

    def create_work_item(self, project, wit, fields, **kwargs):
        if "System.State" in fields:
            raise AdoError(_STATE_REJECTED, status=400)
        with self.lock:
            self.created.append(fields["System.Title"])
            return {"id": 100 + len(self.created)}

    def update_work_items_batch(self, updates):
        if self.batch_error is not None:
            raise self.batch_error
        self.batches.append([work_item_id for work_item_id, _ in updates])
        return [None for _ in updates]


def _import(client, drafts, tmp_path: Path, *, id_map=None, events=None, workers=1, should_cancel=None):
    return import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        drafts,
        project="Demo",
        id_map=dict(id_map or {}),
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=workers,
        emit=lambda _s, msg: (events if events is not None else []).append(msg),
        should_cancel=should_cancel,
    )


def _pending_rows(tmp_path: Path) -> list[tuple[str, str, str]]:
    with (tmp_path / catalog_runtime.PENDING_STATES_CSV).open(encoding="utf-8-sig", newline="") as handle:
        return [(r["Key"], r["WorkItemId"], r["Status"]) for r in csv.DictReader(handle)]


def test_unexpected_worker_error_fails_only_its_row(tmp_path: Path) -> None:
    class Client(_Client):
        def create_work_item(self, project, wit, fields, **kwargs):
            if fields["System.Title"] == "bad":
                raise RuntimeError("boom")
            return super().create_work_item(project, wit, fields, **kwargs)

    drafts = [_draft("good1", row=1), _draft("bad", row=2), _draft("good2", row=3)]
    id_map, failures, stats = _import(Client(), drafts, tmp_path, workers=3)
    assert [(f["key"], f["error"]) for f in failures] == [("bad", "boom")]
    assert stats["created"] == 2 and stats["failed"] == 1
    assert load_id_map(tmp_path / "ado-id-map.csv") == {k: v for k, v in id_map.items()}
    assert sorted(id_map) == ["good1", "good2"]


def test_pending_state_log_write_failure_keeps_created_ids(tmp_path: Path) -> None:
    (tmp_path / catalog_runtime.PENDING_STATES_CSV).mkdir()
    client = _Client()
    events: list[str] = []
    id_map, failures, stats = _import(
        client, [_test_case("tc1", 1), _test_case("tc2", 2)], tmp_path, events=events
    )
    assert failures == []
    assert load_id_map(tmp_path / "ado-id-map.csv") == id_map == {"tc1": 101, "tc2": 102}
    assert client.batches == [[101, 102]]
    assert stats["state_set_after_create"] == 2
    assert stats["state_log_error"]
    assert any(f"couldn't write {catalog_runtime.PENDING_STATES_CSV}" in e for e in events)


def test_id_map_write_failure_reports_the_created_id_and_stops_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = {"n": 0}

    def append(*args, **kwargs):
        writes["n"] += 1
        if writes["n"] == 2:
            raise OSError(28, "No space left on device")
        append_id_map_row(*args, **kwargs)

    monkeypatch.setattr(catalog_runtime, "append_id_map_row", append)
    client = _Client()
    events: list[str] = []
    drafts = [_draft(f"k{i}", row=i) for i in range(1, 5)]
    id_map, failures, stats = _import(client, drafts, tmp_path, events=events)
    assert client.created == ["k1", "k2"]
    assert load_id_map(tmp_path / "ado-id-map.csv") == {"k1": 101}
    assert id_map == {"k1": 101, "k2": 102}
    assert [f["key"] for f in failures] == ["k2"]
    assert "work item 102" in failures[0]["error"] and "No space left" in failures[0]["error"]
    assert stats["created"] == 1 and stats["failed"] == 1 and stats["cancelled"] == 2
    assert any("couldn't write ado-id-map.csv" in e for e in events)


def test_unexpected_state_update_error_leaves_rows_pending(tmp_path: Path) -> None:
    client = _Client(batch_error=ValueError("Expecting value: line 1 column 1"))
    drafts = [_test_case("tc1", 1), _test_case("tc2", 2)]
    id_map, failures, stats = _import(client, drafts, tmp_path)
    assert failures == [] and id_map == {"tc1": 101, "tc2": 102}
    assert stats["state_not_set"] == 2 and stats["state_set_after_create"] == 0
    assert "Expecting value" in stats["state_not_set_examples"][0]["error"]
    assert _pending_rows(tmp_path) == [("tc1", "101", "pending"), ("tc2", "102", "pending")]

    client.batch_error = None
    _, _, again = _import(client, drafts, tmp_path, id_map=id_map)
    assert again["state_resumed"] == 2 and again["state_set_after_create"] == 2
    assert client.batches == [[101, 102]]


def test_id_map_append_follows_a_reordered_header(tmp_path: Path) -> None:
    path = tmp_path / "ado-id-map.csv"
    path.write_text(
        "WorkItemId,Title,Key,WorkItemType,ParentKey,ParentId,Notes\n101,Root,root,End to end,,,mine\n",
        encoding="utf-8",
    )
    handle, writer = open_id_map_for_append(path)
    append_id_map_row(writer, handle, _draft("leaf", parent_key="root"), 103, 101)
    handle.close()
    assert load_id_map(path) == {"root": 101, "leaf": 103}
    assert load_id_map_parents(path) == {"leaf": 101}
    assert path.read_text(encoding="utf-8").splitlines()[-1] == "103,leaf,leaf,Scenario,root,101,"


def test_id_map_append_refuses_a_header_without_key_columns(tmp_path: Path) -> None:
    (tmp_path / "ado-id-map.csv").write_text("Id,Name\n1,x\n", encoding="utf-8")
    client = _Client()
    with pytest.raises(ValueError, match="no Key or WorkItemId column"):
        _import(client, [_draft("a")], tmp_path)
    assert client.created == []


def test_pending_state_log_without_final_newline_gets_one(tmp_path: Path) -> None:
    (tmp_path / catalog_runtime.PENDING_STATES_CSV).write_text(
        "Key,WorkItemId,WorkItemType,State,Status,Message\r\nold,9,Test Case,New,done,", encoding="utf-8"
    )
    _import(_Client(), [_test_case("tc1", 1)], tmp_path)
    assert _pending_rows(tmp_path) == [("old", "9", "done"), ("tc1", "101", "pending"), ("tc1", "101", "done")]


def test_cancel_during_reconcile_stops_promptly_and_leaves_the_row_unmapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = threading.Event()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        cancel.set()

    monkeypatch.setattr(catalog_runtime.time, "sleep", fake_sleep)

    class Client(_Client):
        lookups = 0

        def create_work_item(self, project, wit, fields, **kwargs):
            raise AdoError("socket reset", transient=True, outcome_unknown=True)

        def find_work_item_ids(self, project, wit, **kwargs):
            self.lookups += 1
            return []

    client = Client()
    events: list[str] = []
    drafts = [_draft("only", row=1), _draft("child", parent_key="only", row=2), _draft("other", row=3)]
    id_map, failures, stats = _import(client, drafts, tmp_path, events=events, should_cancel=cancel.is_set)
    assert sleeps == [catalog_runtime.RECONCILE_POLL_S]
    assert client.lookups == 0
    assert failures == [] and id_map == {}
    assert stats["failed"] == 0 and stats["outcome_unknown"] == 0 and stats["missing_parent"] == 0
    assert stats["cancelled"] == 3
    assert load_id_map(tmp_path / "ado-id-map.csv") == {}
    assert any("Cancelled while checking" in e for e in events)


def test_reconcile_skips_an_item_another_worker_just_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (0,))
    recorded = threading.Event()
    looked_up = threading.Event()

    def append(*args, **kwargs):
        append_id_map_row(*args, **kwargs)
        recorded.set()
        # Hold the creating worker so the main thread can't have mapped its item yet.
        looked_up.wait(5)

    monkeypatch.setattr(catalog_runtime, "append_id_map_row", append)

    class Client:
        def create_work_item(self, project, wit, fields, **kwargs):
            if fields["System.Title"] == "first":
                return {"id": 55}
            raise AdoError("socket reset", transient=True, outcome_unknown=True)

        def find_work_item_ids(self, project, wit, **kwargs):
            assert recorded.wait(5)
            looked_up.set()
            return [55]

    id_map, failures, stats = _import(
        Client(), [_draft("first", row=1), _draft("second", row=2)], tmp_path, workers=2
    )
    assert id_map == {"first": 55}
    assert [f["key"] for f in failures] == ["second"]
    assert "didn't show up" in failures[0]["error"]
    assert stats["reconciled"] == 0


def test_claims_give_an_item_to_one_reconcile_only() -> None:
    claims = catalog_runtime._Claims([10])
    assert claims.unclaimed([10, 55, 56]) == [55, 56]
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(claims.claim(55))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]
