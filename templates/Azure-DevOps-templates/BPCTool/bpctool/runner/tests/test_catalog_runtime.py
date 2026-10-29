"""Resume, parent-aware ready queue, and id-map helpers for catalog import."""

from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from app import catalog_runtime
from app.ado_client import AdoError
from app.catalog_import import WorkItemDraft
from app.catalog_runtime import (
    ReadyQueue,
    append_id_map_row,
    backfill_parent_links,
    clamp_parallel_workers,
    import_drafts_parent_aware,
    load_id_map,
    load_id_map_parents,
    open_id_map_for_append,
)


def _draft(
    key: str,
    *,
    parent_key: str | None = None,
    wit: str = "Scenario",
    title: str | None = None,
    row: int = 1,
) -> WorkItemDraft:
    return WorkItemDraft(
        key=key,
        parent_key=parent_key,
        work_item_type=wit,
        title=title or key,
        fields={"System.Title": title or key},
        source_file="bpc.xlsx",
        source_row=row,
    )


def test_clamp_parallel_workers() -> None:
    assert clamp_parallel_workers(None) == 4
    assert clamp_parallel_workers(0) == 1
    assert clamp_parallel_workers(99) == 16
    assert clamp_parallel_workers(8) == 8


def test_load_id_map_resume(tmp_path: Path) -> None:
    path = tmp_path / "ado-id-map.csv"
    path.write_text(
        "Key,WorkItemId,WorkItemType,Title,ParentKey,ParentId\n"
        "root,101,End to end,Root,,\n"
        "child,102,Scenario,Child,root,101\n",
        encoding="utf-8",
    )
    assert load_id_map(path) == {"root": 101, "child": 102}
    assert load_id_map(tmp_path / "missing.csv") == {}


def test_load_id_map_parents(tmp_path: Path) -> None:
    path = tmp_path / "ado-id-map.csv"
    path.write_text(
        "Key,WorkItemId,WorkItemType,Title,ParentKey,ParentId\n"
        "root,101,End to end,Root,,\n"
        "child,102,Scenario,Child,root,101\n",
        encoding="utf-8",
    )
    assert load_id_map_parents(path) == {"child": 101}


def test_ready_queue_parent_aware() -> None:
    pending = {
        "a": _draft("a", row=1),
        "b": _draft("b", parent_key="a", row=2),
        "c": _draft("c", parent_key="missing", row=3),
        "d": _draft("d", row=4),
    }
    queue = ReadyQueue(pending, id_map={})
    assert [d.key for d in queue.pop(10)] == ["a", "d"]
    assert queue.pop(10) == []
    queue.parent_created("a")
    assert [d.key for d in queue.pop(10)] == ["b"]
    assert [d.key for d in queue.remaining()] == ["c"]


def test_ready_queue_respects_limit_and_source_order() -> None:
    pending = {key: _draft(key, row=row) for key, row in (("z", 1), ("y", 2), ("x", 3))}
    queue = ReadyQueue(pending, id_map={})
    assert [d.key for d in queue.pop(2)] == ["z", "y"]
    assert [d.key for d in queue.pop(2)] == ["x"]


def test_import_reports_missing_parent(tmp_path: Path) -> None:
    class FakeClient:
        def create_work_item(self, project, wit, fields, **kwargs):
            if fields["System.Title"] == "root":
                raise AdoError("ADO 400 Bad Request: boom", status=400)
            return {"id": 5}

    drafts = [_draft("root", row=1), _draft("child", parent_key="root", row=2)]
    _, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        drafts,
        project="Demo",
        id_map={},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=2,
        emit=lambda *_: None,
    )
    assert stats["failed"] == 2
    assert stats["missing_parent"] == 1
    assert stats["cancelled"] == 0
    assert {f["key"] for f in failures} == {"root", "child"}


def test_import_cancel_stops_submitting_and_records_in_flight(tmp_path: Path) -> None:
    cancel = threading.Event()

    class FakeClient:
        def __init__(self) -> None:
            self.created: list[str] = []

        def create_work_item(self, project, wit, fields, **kwargs):
            self.created.append(fields["System.Title"])
            cancel.set()
            return {"id": 100 + len(self.created)}

    client = FakeClient()
    id_map_path = tmp_path / "ado-id-map.csv"
    drafts = [_draft(f"k{i}", row=i) for i in range(1, 6)]
    progress: list[dict] = []
    id_map, failures, stats = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        drafts,
        project="Demo",
        id_map={},
        id_map_path=id_map_path,
        parallel_workers=1,
        emit=lambda *_: None,
        progress=progress.append,
        should_cancel=cancel.is_set,
    )
    assert client.created == ["k1"]
    assert stats["created"] == 1
    assert stats["cancelled"] == 4
    assert stats["missing_parent"] == 0
    assert failures == []
    assert load_id_map(id_map_path) == {"k1": 101}
    assert progress[-1]["phase"] == "create"
    assert progress[-1]["created"] == 1


def test_cancel_keeps_failed_parent_descendants_as_missing_parent(tmp_path: Path) -> None:
    cancel = threading.Event()

    class FakeClient:
        def create_work_item(self, project, wit, fields, **kwargs):
            title = fields["System.Title"]
            if title == "bad":
                raise AdoError("ADO 400 Bad Request: boom", status=400)
            cancel.set()
            return {"id": 7}

    drafts = [
        _draft("bad", row=1),
        _draft("bad-child", parent_key="bad", row=2),
        _draft("good", row=3),
        _draft("good-child", parent_key="good", row=4),
        _draft("other", row=5),
    ]
    _, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        drafts,
        project="Demo",
        id_map={},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda *_: None,
        should_cancel=cancel.is_set,
    )
    assert stats["created"] == 1
    assert stats["missing_parent"] == 1
    assert {f["key"] for f in failures} == {"bad", "bad-child"}
    assert stats["cancelled"] == 2


_STATE_REJECTED = (
    "ADO 400 Bad Request: {\"customProperties\":{\"ErrorMessage\":\"The field 'State' contains the value "
    "'New' that is not in the list of supported values\",\"RuleValidationErrors\":[{\"fieldReferenceName\":"
    "\"System.State\"}]}}"
)


def _test_case(key: str, row: int) -> WorkItemDraft:
    draft = _draft(key, wit="Test Case", row=row)
    draft.fields["System.State"] = "New"
    return draft


class _InitialStateOnlyClient:
    """Like Azure DevOps for Test Case: State 'New' exists but isn't accepted on create."""

    def __init__(self, *, update_fails: bool = False, batch_error: AdoError | None = None) -> None:
        self.creates: list[dict] = []
        self.batches: list[list[tuple[int, dict]]] = []
        self.updates: list[tuple[int, dict]] = []
        self.update_fails = update_fails
        self.batch_error = batch_error

    def create_work_item(self, project, wit, fields, **kwargs):
        self.creates.append(dict(fields))
        if "System.State" in fields:
            raise AdoError(_STATE_REJECTED, status=400)
        return {"id": 100 + len(self.creates)}

    def update_work_items_batch(self, updates):
        if self.batch_error is not None:
            raise self.batch_error
        self.batches.append([(work_item_id, dict(fields)) for work_item_id, fields in updates])
        return ["ADO 400: TF401320: transition not allowed" if self.update_fails else None for _ in updates]

    def update_work_item_fields(self, project, work_item_id, fields):
        if self.update_fails:
            raise AdoError("ADO 400 Bad Request: TF401320: transition not allowed", status=400)
        self.updates.append((work_item_id, dict(fields)))
        return {"id": work_item_id}


def _pending_rows(tmp_path: Path) -> list[tuple[str, str, str]]:
    with (tmp_path / catalog_runtime.PENDING_STATES_CSV).open(encoding="utf-8") as handle:
        return [(r["Key"], r["WorkItemId"], r["Status"]) for r in csv.DictReader(handle)]


def _import(client, drafts, tmp_path: Path, *, id_map=None, events=None):
    return import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        drafts,
        project="Demo",
        id_map=dict(id_map or {}),
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda _s, msg: (events if events is not None else []).append(msg),
    )


def test_state_refused_on_create_is_set_after_create_in_one_batch(tmp_path: Path) -> None:
    client = _InitialStateOnlyClient()
    events: list[str] = []
    id_map, failures, stats = _import(
        client, [_test_case("tc1", 1), _test_case("tc2", 2), _test_case("tc3", 3)], tmp_path, events=events
    )
    assert failures == []
    assert stats["created"] == 3 and stats["state_set_after_create"] == 3 and stats["state_not_set"] == 0
    # Only the first row tries State on create; the rest go straight to create-then-update.
    assert ["System.State" in fields for fields in client.creates] == [True, False, False, False]
    assert client.batches == [[(102, {"System.State": "New"}), (103, {"System.State": "New"}),
                               (104, {"System.State": "New"})]]
    assert client.updates == []
    assert sorted(id_map) == ["tc1", "tc2", "tc3"]
    assert any("moved to their State" in e for e in events)
    assert _pending_rows(tmp_path) == [
        ("tc1", "102", "pending"), ("tc2", "103", "pending"), ("tc3", "104", "pending"),
        ("tc1", "102", "done"), ("tc2", "103", "done"), ("tc3", "104", "done"),
    ]


def test_state_moves_are_sent_in_batches_of_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_runtime, "STATE_BATCH_SIZE", 2)
    client = _InitialStateOnlyClient()
    _, _, stats = _import(client, [_test_case(f"tc{i}", i) for i in range(1, 6)], tmp_path)
    assert [len(batch) for batch in client.batches] == [2, 2, 1]
    assert stats["state_set_after_create"] == 5


def test_state_that_cannot_be_set_after_create_is_reported_and_kept_pending(tmp_path: Path) -> None:
    client = _InitialStateOnlyClient(update_fails=True)
    events: list[str] = []
    id_map, failures, stats = _import(client, [_test_case("tc1", 7)], tmp_path, events=events)
    assert failures == [] and id_map == {"tc1": 102}
    assert stats["state_not_set"] == 1
    example = stats["state_not_set_examples"][0]
    assert example["workItemId"] == 102 and example["state"] == "New" and example["sourceRow"] == 7
    assert "TF401320" in example["error"]
    assert any("couldn't be moved to their State" in e for e in events)
    assert _pending_rows(tmp_path) == [("tc1", "102", "pending"), ("tc1", "102", "failed")]

    client.update_fails = False
    _, _, again = _import(client, [_test_case("tc1", 7)], tmp_path, id_map=id_map)
    assert again["state_resumed"] == 1 and again["state_set_after_create"] == 1
    assert _pending_rows(tmp_path)[-1] == ("tc1", "102", "done")


def test_batch_refused_falls_back_to_one_update_per_item(tmp_path: Path) -> None:
    client = _InitialStateOnlyClient(batch_error=AdoError("ADO 404 Not Found: no $batch", status=404))
    events: list[str] = []
    _, _, stats = _import(client, [_test_case("tc1", 1), _test_case("tc2", 2)], tmp_path, events=events)
    assert client.updates == [(102, {"System.State": "New"}), (103, {"System.State": "New"})]
    assert stats["state_set_after_create"] == 2
    assert sum("one work item at a time" in e for e in events) == 1


def test_state_moves_left_by_a_stopped_run_are_sent_by_the_next_run(tmp_path: Path) -> None:
    (tmp_path / catalog_runtime.PENDING_STATES_CSV).write_text(
        "Key,WorkItemId,WorkItemType,State,Status,Message\n"
        "tc1,501,Test Case,New,pending,\n"
        "tc2,502,Test Case,New,pending,\n"
        "tc2,502,Test Case,New,done,\n"
        "gone,503,Test Case,New,pending,\n"
        "moved,504,Test Case,New,pending,\n",
        encoding="utf-8",
    )
    drafts = [_test_case("tc1", 1), _test_case("tc2", 2), _test_case("moved", 3)]
    drafts[0].fields["System.State"] = "Ready"
    client = _InitialStateOnlyClient()
    events: list[str] = []
    _, _, stats = _import(
        client, drafts, tmp_path, id_map={"tc1": 501, "tc2": 502, "gone": 503, "moved": 999}, events=events
    )
    # tc1 takes the workbook's State; "gone" isn't in the workbook and keeps the logged one;
    # "moved" now points at another work item, so its move is dropped.
    assert client.batches == [[(501, {"System.State": "Ready"}), (503, {"System.State": "New"})]]
    assert client.creates == []
    assert stats["state_resumed"] == 2 and stats["state_set_after_create"] == 2
    assert any("left pending by an earlier run" in e for e in events)

    client.batches.clear()
    _import(client, drafts, tmp_path, id_map={"tc1": 501, "tc2": 502, "gone": 503, "moved": 999})
    assert client.batches == []


def test_other_create_errors_are_not_retried_without_state(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.creates = 0

        def create_work_item(self, project, wit, fields, **kwargs):
            self.creates += 1
            raise AdoError("ADO 400 Bad Request: TF401347: Invalid tree name", status=400)

    client = FakeClient()
    _, failures, stats = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        [_test_case("tc1", 1)],
        project="Demo",
        id_map={},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda *_: None,
    )
    assert client.creates == 1 and stats["failed"] == 1
    assert "TF401347" in failures[0]["error"]


def test_load_id_map_reads_excel_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "ado-id-map.csv"
    path.write_bytes(
        "Key,WorkItemId,WorkItemType,Title,ParentKey,ParentId\r\n"
        "root,101,End to end,Root,,\r\n"
        "child,102,Scenario,Child,root,101".encode("utf-8-sig")
    )
    assert load_id_map(path) == {"root": 101, "child": 102}
    assert load_id_map_parents(path) == {"child": 101}

    handle, writer = open_id_map_for_append(path)
    append_id_map_row(writer, handle, _draft("leaf", parent_key="child"), 103, 102)
    handle.close()
    assert load_id_map(path) == {"root": 101, "child": 102, "leaf": 103}


def test_create_outcome_unknown_adopts_single_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (0, 0, 0))

    class FakeClient:
        def __init__(self) -> None:
            self.creates = 0
            self.lookups = 0

        def create_work_item(self, project, wit, fields, **kwargs):
            self.creates += 1
            raise AdoError("socket reset", transient=True, outcome_unknown=True)

        def find_work_item_ids(self, project, wit, **kwargs):
            self.lookups += 1
            return [777] if self.lookups == 3 else []

    client = FakeClient()
    id_map_path = tmp_path / "ado-id-map.csv"
    id_map, failures, stats = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        [_draft("only", row=1)],
        project="Demo",
        id_map={},
        id_map_path=id_map_path,
        parallel_workers=1,
        emit=lambda *_: None,
    )
    assert client.creates == 1
    assert client.lookups == 3
    assert failures == []
    assert id_map == {"only": 777}
    assert stats["reconciled"] == 1
    # ParentId left blank so backfill re-checks the link instead of trusting it.
    assert load_id_map_parents(tmp_path / "ado-id-map.csv") == {}


def test_create_outcome_unknown_is_not_sent_again_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (1.0, 2.0, 3.0))
    monkeypatch.setattr(catalog_runtime.time, "sleep", sleeps.append)

    class FakeClient:
        def __init__(self) -> None:
            self.creates = 0
            self.lookups = 0

        def create_work_item(self, project, wit, fields, **kwargs):
            self.creates += 1
            raise AdoError("socket reset", transient=True, outcome_unknown=True)

        def find_work_item_ids(self, project, wit, **kwargs):
            self.lookups += 1
            return []

    client = FakeClient()
    events: list[str] = []
    id_map, failures, stats = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        [_draft("only", parent_key="root", row=2), _draft("leaf", parent_key="only", row=3)],
        project="Demo",
        id_map={"root": 10},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda _s, msg: events.append(msg),
    )
    assert client.creates == 1
    assert client.lookups == 3
    # Waits are slept in short slices so Cancel is noticed.
    assert sum(sleeps) == 6.0 and max(sleeps) <= catalog_runtime.RECONCILE_POLL_S
    assert id_map == {"root": 10}
    assert stats["outcome_unknown"] == 1
    by_key = {f["key"]: f for f in failures}
    assert "wasn't created again" in by_key["only"]["error"]
    assert "Re-run Catalog Import" in by_key["only"]["error"]
    assert by_key["leaf"]["error"] == "Parent not imported: only"
    assert any("weren't sent again" in e for e in events)


def test_create_outcome_unknown_with_duplicates_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (0,))

    class FakeClient:
        def create_work_item(self, project, wit, fields, **kwargs):
            raise AdoError("socket reset", transient=True, outcome_unknown=True)

        def find_work_item_ids(self, project, wit, **kwargs):
            return [1, 2]

    _, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        [_draft("only", row=1)],
        project="Demo",
        id_map={},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda *_: None,
    )
    assert stats["outcome_unknown"] == 1
    assert "2 matching work items" in failures[0]["error"]


def test_create_outcome_unknown_ignores_matches_mapped_to_other_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (0, 0))

    class FakeClient:
        def __init__(self) -> None:
            self.lookups = 0

        def create_work_item(self, project, wit, fields, **kwargs):
            raise AdoError("ADO 500 Internal Server Error: boom", status=500, outcome_unknown=True)

        def find_work_item_ids(self, project, wit, **kwargs):
            self.lookups += 1
            # 10 is the item imported earlier under "twin"; 55 shows up on the second look.
            return [10] if self.lookups == 1 else [10, 55]

    client = FakeClient()
    id_map, failures, stats = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        [_draft("only", row=2)],
        project="Demo",
        id_map={"twin": 10},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda *_: None,
    )
    assert failures == []
    assert id_map == {"twin": 10, "only": 55}
    assert stats["reconciled"] == 1


def test_create_outcome_unknown_with_only_claimed_matches_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (0, 0))

    class FakeClient:
        def create_work_item(self, project, wit, fields, **kwargs):
            raise AdoError("socket reset", transient=True, outcome_unknown=True)

        def find_work_item_ids(self, project, wit, **kwargs):
            return [10]

    id_map, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        [_draft("only", row=2)],
        project="Demo",
        id_map={"twin": 10},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda *_: None,
    )
    assert id_map == {"twin": 10}
    assert stats["outcome_unknown"] == 1
    assert "didn't show up" in failures[0]["error"]


def _with_parent(work_item_id: int, parent_id: int | None) -> dict:
    relations = (
        [{"rel": "System.LinkTypes.Hierarchy-Reverse", "url": f"https://x/_apis/wit/workItems/{parent_id}"}]
        if parent_id
        else []
    )
    return {"id": work_item_id, "relations": relations}


@pytest.mark.parametrize(
    "error",
    [
        AdoError("Network error (outcome unknown, not retried)", transient=True, outcome_unknown=True),
        AdoError("ADO 400 Bad Request: TF201036: already has a parent", status=400),
    ],
)
def test_backfill_rechecks_parent_after_unclear_link_error(tmp_path: Path, error: AdoError) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.parent: int | None = None
            self.links = 0

        def get_work_items_batch(self, project, ids, *, expand_relations=False):
            assert expand_relations
            return [_with_parent(i, self.parent) for i in ids]

        def get_parent_id(self, project, child_id):
            return self.parent

        def add_parent_link(self, project, child_id, parent_id, **kwargs):
            self.links += 1
            self.parent = parent_id
            raise error

    client = FakeClient()
    counts = backfill_parent_links(
        client,  # type: ignore[arg-type]
        [_draft("root", row=1), _draft("a", parent_key="root", row=2)],
        {"root": 1, "a": 2},
        project="Demo",
        output_dir=tmp_path,
        emit=lambda *_: None,
    )
    assert client.links == 1
    assert counts["linked"] == 1 and counts["failed"] == 0


def test_backfill_reports_failure_when_recheck_finds_no_parent(tmp_path: Path) -> None:
    class FakeClient:
        def get_work_items_batch(self, project, ids, *, expand_relations=False):
            return [_with_parent(i, None) for i in ids]

        def get_parent_id(self, project, child_id):
            return None

        def add_parent_link(self, project, child_id, parent_id, **kwargs):
            raise AdoError("ADO 500 Internal Server Error (outcome unknown)", status=500, outcome_unknown=True)

    counts = backfill_parent_links(
        FakeClient(),  # type: ignore[arg-type]
        [_draft("root", row=1), _draft("a", parent_key="root", row=2)],
        {"root": 1, "a": 2},
        project="Demo",
        output_dir=tmp_path,
        emit=lambda *_: None,
    )
    assert counts["failed"] == 1 and counts["linked"] == 0
    with (tmp_path / "backfill-links-report.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert "outcome unknown" in rows[0]["Message"]


class _BatchParentsClient:
    def __init__(self, parents: dict[int, int | None]) -> None:
        self.lock = threading.Lock()
        self.parents = parents
        self.reads: list[list[int]] = []
        self.linked: list[tuple[int, int]] = []

    def get_work_items_batch(self, project, ids, *, expand_relations=False):
        assert expand_relations
        with self.lock:
            self.reads.append(sorted(ids))
        return [_with_parent(i, self.parents[i]) for i in ids if i in self.parents]

    def get_parent_id(self, project, child_id):
        raise AssertionError("backfill reads parents in batches")

    def add_parent_link(self, project, child_id, parent_id, **kwargs):
        with self.lock:
            self.linked.append((child_id, parent_id))
            self.parents[child_id] = parent_id


def test_backfill_skips_recorded_links_and_reads_the_rest_in_one_batch(tmp_path: Path) -> None:
    client = _BatchParentsClient({2: 1, 3: None, 4: 99, 5: 1})
    drafts = [
        _draft("root", row=1),
        _draft("a", parent_key="root", row=2),
        _draft("b", parent_key="root", row=3),
        _draft("c", parent_key="root", row=4),
        _draft("d", parent_key="root", row=5),
    ]
    id_map = {"root": 1, "a": 2, "b": 3, "c": 4, "d": 5}
    counts = backfill_parent_links(
        client,  # type: ignore[arg-type]
        drafts,
        id_map,
        project="Demo",
        output_dir=tmp_path,
        emit=lambda *_: None,
        recorded_parents={"a": 1},
        parallel_workers=4,
    )
    assert client.reads == [[3, 4, 5]]
    assert client.linked == [(3, 1)]
    assert counts["recorded_in_map"] == 1
    assert counts["already_linked"] == 2
    assert counts["linked"] == 1
    assert counts["conflict_existing_parent"] == 1
    raw = (tmp_path / "backfill-links-report.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfKey,")
    with (tmp_path / "backfill-links-report.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["Key"], r["Status"]) for r in rows] == [
        ("a", "already_linked"),
        ("b", "linked"),
        ("c", "conflict_existing_parent"),
        ("d", "already_linked"),
    ]


def test_backfill_splits_reads_into_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_runtime, "BACKFILL_BATCH", 2)
    client = _BatchParentsClient({i: 1 for i in range(2, 7)})
    drafts = [_draft("root", row=1)] + [_draft(f"k{i}", parent_key="root", row=i) for i in range(2, 7)]
    counts = backfill_parent_links(
        client,  # type: ignore[arg-type]
        drafts,
        {"root": 1, **{f"k{i}": i for i in range(2, 7)}},
        project="Demo",
        output_dir=tmp_path,
        emit=lambda *_: None,
        parallel_workers=2,
    )
    assert sorted(client.reads) == [[2, 3], [4, 5], [6]]
    assert counts["already_linked"] == 5 and counts["checked"] == 5


def test_backfill_reports_missing_items_and_failed_reads(tmp_path: Path) -> None:
    class FakeClient(_BatchParentsClient):
        def get_work_items_batch(self, project, ids, *, expand_relations=False):
            if 9 in ids:
                raise AdoError("ADO 500 Internal Server Error: boom", status=500)
            return super().get_work_items_batch(project, ids, expand_relations=expand_relations)

    drafts = [_draft("root", row=1), _draft("gone", parent_key="root", row=2), _draft("bad", parent_key="root", row=3)]
    for draft_ids, expected in (({"gone": 2}, "wasn't found"), ({"bad": 9}, "Couldn't read")):
        counts = backfill_parent_links(
            FakeClient({}),  # type: ignore[arg-type]
            drafts,
            {"root": 1, **draft_ids},
            project="Demo",
            output_dir=tmp_path,
            emit=lambda *_: None,
        )
        assert counts["failed"] == 1
        with (tmp_path / "backfill-links-report.csv").open(encoding="utf-8-sig") as handle:
            assert expected in list(csv.DictReader(handle))[0]["Message"]


def test_backfill_records_confirmed_links_so_the_next_run_skips_them(tmp_path: Path) -> None:
    id_map_path = tmp_path / "ado-id-map.csv"
    handle, writer = open_id_map_for_append(id_map_path)
    for draft, work_item_id in ((_draft("root"), 1), (_draft("a", parent_key="root"), 2),
                                (_draft("b", parent_key="root"), 3), (_draft("c", parent_key="root"), 4)):
        append_id_map_row(writer, handle, draft, work_item_id, None)
    handle.close()
    drafts = [_draft("root"), _draft("a", parent_key="root"), _draft("b", parent_key="root"),
              _draft("c", parent_key="root")]
    client = _BatchParentsClient({2: 1, 3: None, 4: 99})

    def run() -> dict[str, int]:
        return backfill_parent_links(
            client,  # type: ignore[arg-type]
            drafts,
            load_id_map(id_map_path),
            project="Demo",
            output_dir=tmp_path,
            emit=lambda *_: None,
            recorded_parents=load_id_map_parents(id_map_path),
            id_map_path=id_map_path,
        )

    first = run()
    assert first["newly_recorded"] == 2
    # The conflict (c) isn't recorded, so it is checked again.
    assert load_id_map_parents(id_map_path) == {"a": 1, "b": 1}
    assert load_id_map(id_map_path) == {"root": 1, "a": 2, "b": 3, "c": 4}
    second = run()
    assert client.reads == [[2, 3, 4], [4]]
    assert second["recorded_in_map"] == 2 and second["conflict_existing_parent"] == 1
    assert second["newly_recorded"] == 0


def test_recorded_parent_counts_only_for_the_keys_current_work_item(tmp_path: Path) -> None:
    path = tmp_path / "ado-id-map.csv"
    path.write_text(
        "Key,WorkItemId,WorkItemType,Title,ParentKey,ParentId\n"
        "child,102,Scenario,Child,root,101\n"
        "child,205,Scenario,Child,root,\n"
        "other,300,Scenario,Other,root,101\n"
        "other,300,Scenario,Other,root,\n",
        encoding="utf-8",
    )
    assert load_id_map_parents(path) == {"other": 101}


def test_import_resume_skips_existing(tmp_path: Path) -> None:
    id_map_path = tmp_path / "ado-id-map.csv"
    handle, writer = open_id_map_for_append(id_map_path)
    root = _draft("root", row=1)
    append_id_map_row(writer, handle, root, 10, None)
    handle.close()

    class FakeClient:
        def __init__(self) -> None:
            self.created: list[str] = []

        def create_work_item(self, project, wit, fields, **kwargs):
            key = fields["System.Title"]
            self.created.append(key)
            return {"id": 100 + len(self.created)}

    client = FakeClient()
    drafts = [
        _draft("root", row=1),
        _draft("child", parent_key="root", row=2, title="child"),
    ]
    events: list[str] = []
    id_map = load_id_map(id_map_path)
    id_map, failures, stats = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        drafts,
        project="Demo",
        id_map=id_map,
        id_map_path=id_map_path,
        parallel_workers=1,
        emit=lambda _s, msg: events.append(msg),
    )
    assert stats["skipped"] == 1
    assert stats["created"] == 1
    assert id_map["root"] == 10
    assert id_map["child"] == 101
    assert client.created == ["child"]
    assert failures == []
    assert any("Resume" in e for e in events)


def test_import_parallel_waits_for_parent(tmp_path: Path) -> None:
    id_map_path = tmp_path / "ado-id-map.csv"

    class FakeClient:
        def __init__(self) -> None:
            self.order: list[str] = []

        def create_work_item(self, project, wit, fields, **kwargs):
            key = fields["System.Title"]
            self.order.append(key)
            parent_id = kwargs.get("parent_id")
            if key == "child" and parent_id is None:
                raise AssertionError("child created before parent id known")
            return {"id": {"root": 1, "child": 2, "leaf": 3}[key]}

    client = FakeClient()
    drafts = [
        _draft("leaf", parent_key="child", row=3, title="leaf"),
        _draft("child", parent_key="root", row=2, title="child"),
        _draft("root", row=1, title="root"),
    ]
    id_map, failures, stats = import_drafts_parent_aware(
        client,  # type: ignore[arg-type]
        drafts,
        project="Demo",
        id_map={},
        id_map_path=id_map_path,
        parallel_workers=4,
        emit=lambda *_: None,
    )
    assert failures == []
    assert stats["created"] == 3
    assert stats["linked"] == 2
    assert id_map == {"root": 1, "child": 2, "leaf": 3}
    assert client.order[0] == "root"
