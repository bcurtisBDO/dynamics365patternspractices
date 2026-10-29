"""Backfill records that can't be written, map appends rolled back on a write error, long
Retry-After, create replies without an ID, and Cancel during throttle / retry waits."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app import ado_client, catalog_runtime
from app.ado_client import AdoClient, AdoError, RunCancelled
from app.catalog_import import WorkItemDraft
from app.catalog_runtime import (
    PENDING_STATES_CSV,
    _create_one,
    backfill_parent_links,
    import_drafts_parent_aware,
)

PAT = "x" * 52


def _draft(key: str, *, parent_key: str | None = None, wit: str = "Scenario", row: int = 1) -> WorkItemDraft:
    return WorkItemDraft(
        key=key,
        parent_key=parent_key,
        work_item_type=wit,
        title=key,
        fields={"System.Title": key, "MSBPC.microsoftid": f"MS-{key}"},
        source_file="bpc.xlsx",
        source_row=row,
    )


def _client(monkeypatch: pytest.MonkeyPatch, responses: list) -> tuple[AdoClient, list]:
    client = AdoClient("https://dev.azure.com/contoso", PAT)
    calls: list[str] = []

    def fake_send(method, url, data, headers):
        calls.append(method)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(client, "_send", fake_send)
    return client, calls


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(ado_client.time, "sleep", sleeps.append)
    return sleeps


# Backfill: an id map or report that can't be written


class _UnlinkedClient:
    def __init__(self) -> None:
        self.links: list[tuple[int, int]] = []

    def get_work_items_batch(self, project, ids, *, expand_relations=False):
        return [{"id": i, "relations": []} for i in ids]

    def add_parent_link(self, project, child_id, parent_id, **kwargs):
        self.links.append((child_id, parent_id))


def _backfill(tmp_path: Path, id_map_path: Path, events: list[str]) -> dict[str, int]:
    return backfill_parent_links(
        _UnlinkedClient(),  # type: ignore[arg-type]
        [_draft("root", row=1), _draft("a", parent_key="root", row=2)],
        {"root": 1, "a": 2},
        project="Demo",
        output_dir=tmp_path,
        emit=lambda _s, msg: events.append(msg),
        id_map_path=id_map_path,
    )


def test_backfill_still_writes_its_report_when_the_id_map_cannot_be_written(tmp_path: Path) -> None:
    blocked = tmp_path / "ado-id-map.csv"
    blocked.mkdir()  # opening it for append fails, like a file Excel holds open on Windows
    events: list[str] = []
    counts = _backfill(tmp_path, blocked, events)
    assert counts["linked"] == 1 and counts["newly_recorded"] == 0 and counts["not_recorded"] == 1
    assert any("couldn't record 1 confirmed parent link(s) in ado-id-map.csv" in e for e in events)
    with (tmp_path / "backfill-links-report.csv").open(encoding="utf-8-sig") as handle:
        assert [row["Status"] for row in csv.DictReader(handle)] == ["linked"]


def test_backfill_map_with_a_bad_header_is_a_warning(tmp_path: Path) -> None:
    id_map = tmp_path / "ado-id-map.csv"
    id_map.write_text("Something,Else\n1,2\n", encoding="utf-8")
    events: list[str] = []
    counts = _backfill(tmp_path, id_map, events)
    assert counts["not_recorded"] == 1
    assert id_map.read_text(encoding="utf-8") == "Something,Else\n1,2\n"
    assert (tmp_path / "backfill-links-report.csv").is_file()


def _disk_fills_after(monkeypatch: pytest.MonkeyPatch, ok_writes: int) -> None:
    """Appends succeed ok_writes times; the next one gets half its bytes on disk, then fails."""
    writes = {"n": 0}
    real_init = catalog_runtime._RollbackAppender.__init__

    class FillingFile:
        def __init__(self, raw) -> None:
            self._raw = raw

        def write(self, data) -> int:
            writes["n"] += 1
            if writes["n"] <= ok_writes:
                return self._raw.write(data)
            if writes["n"] == ok_writes + 1:
                return self._raw.write(bytes(data[: len(data) // 2]))
            raise OSError(28, "No space left on device")

        def __getattr__(self, name: str):
            return getattr(self._raw, name)

    def init(self, path: Path) -> None:
        real_init(self, path)
        self._raw = FillingFile(self._raw)

    monkeypatch.setattr(catalog_runtime._RollbackAppender, "__init__", init)


def test_a_create_whose_map_row_fails_part_way_leaves_no_cut_off_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _disk_fills_after(monkeypatch, ok_writes=2)  # header, then k1

    class FakeClient:
        next_id = 12340

        def create_work_item(self, project, wit, fields, **kwargs):
            self.next_id += 1
            return {"id": self.next_id}

    id_map_path = tmp_path / "ado-id-map.csv"
    events: list[str] = []
    _, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        [_draft(f"k{i}", row=i) for i in range(1, 4)],
        project="Demo",
        id_map={},
        id_map_path=id_map_path,
        parallel_workers=1,
        emit=lambda _s, msg: events.append(msg),
    )
    assert stats["created"] == 1 and [f["key"] for f in failures] == ["k2"]
    assert "No space left" in failures[0]["error"]
    lines = id_map_path.read_bytes().decode("utf-8").split("\r\n")
    assert lines[-1] == "" and len(lines) == 3
    assert lines[1].startswith("k1,12341,")
    assert catalog_runtime.load_id_map(id_map_path) == {"k1": 12341}


def test_backfill_rows_that_fail_part_way_are_all_rolled_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    id_map = tmp_path / "ado-id-map.csv"
    before = "Key,WorkItemId,WorkItemType,Title,ParentKey,ParentId\r\nroot,1,Scenario,root,,\r\na,2,Scenario,a,,\r\n"
    id_map.write_text(before, encoding="utf-8", newline="")
    _disk_fills_after(monkeypatch, ok_writes=0)
    events: list[str] = []
    counts = _backfill(tmp_path, id_map, events)
    assert counts["linked"] == 1 and counts["newly_recorded"] == 0 and counts["not_recorded"] == 1
    assert id_map.read_bytes().decode("utf-8") == before
    assert any("couldn't record 1 confirmed parent link(s)" in e for e in events)


@pytest.mark.parametrize("stalled", [0, None])
def test_a_write_that_makes_no_progress_fails_and_rolls_back(tmp_path: Path, stalled) -> None:
    path = tmp_path / "ado-id-map.csv"
    path.write_bytes(b"Key,WorkItemId\r\n")
    handle, writer = catalog_runtime._open_csv_for_append(path, ["Key", "WorkItemId"], ("Key",))
    real = handle._raw
    calls = {"n": 0}

    class StallingFile:
        def write(self, data):
            calls["n"] += 1
            return real.write(bytes(data[:3])) if calls["n"] == 1 else stalled

        def __getattr__(self, name: str):
            return getattr(real, name)

    handle._raw = StallingFile()
    writer.writerow({"Key": "k1", "WorkItemId": 12345})
    with pytest.raises(OSError, match="No bytes written"):
        handle.flush()
    handle.close()
    assert calls["n"] == 2
    assert path.read_bytes() == b"Key,WorkItemId\r\n"


def test_backfill_report_that_cannot_be_written_is_a_warning(tmp_path: Path) -> None:
    (tmp_path / "backfill-links-report.csv").mkdir()
    events: list[str] = []
    counts = _backfill(tmp_path, tmp_path / "ado-id-map.csv", events)
    assert counts["linked"] == 1 and counts["newly_recorded"] == 1
    assert "WARNING: couldn't write backfill-links-report.csv" in events[-1]


# Retry-After longer than a minute


def test_retry_after_up_to_five_minutes_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _record_sleeps(monkeypatch)
    client, calls = _client(
        monkeypatch,
        [(429, "Too Many Requests", {"Retry-After": "120"}, b""), (200, "OK", {}, b"{}")],
    )
    client.post("/_apis/wit/workitems/$Task", [], idempotent=False)
    assert calls == ["POST", "POST"]
    assert len(sleeps) == 1 and 119.0 < sleeps[0] <= 120.0
    assert ado_client.retry_delay_seconds("9999", 0) == 300.0


def test_notice_names_a_longer_wait_than_the_cap() -> None:
    notice = ado_client._ThrottleGate().observe(200, {"Retry-After": "900"})
    assert notice is not None
    assert "pausing all requests for 300 s (Azure DevOps asked for 900 s)" in notice
    plain = ado_client._ThrottleGate().observe(200, {"Retry-After": "90"})
    assert plain is not None and "pausing all requests for 90 s." in plain


# A create reply without a work item ID


@pytest.mark.parametrize("reply", [{}, None, {"id": "not-a-number"}])
def test_create_reply_without_an_id_is_looked_up(monkeypatch: pytest.MonkeyPatch, reply) -> None:
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (0.0,))

    class FakeClient:
        def __init__(self) -> None:
            self.creates = 0

        def create_work_item(self, *args, **kwargs):
            self.creates += 1
            return reply

        def find_work_item_ids(self, project, wit, *, title, field_ref=None, field_value=None):
            assert (title, field_value) == ("root", "MS-root")
            return [77]

    client = FakeClient()
    recorded: list[tuple[str, int]] = []
    result = _create_one(
        client,  # type: ignore[arg-type]
        _draft("root"),
        project="Demo",
        parent_id=None,
        record=lambda draft, wid, _parent: recorded.append((draft.key, wid)),
    )
    assert result == (77, None, True)
    assert client.creates == 1 and recorded == [("root", 77)]


# Cancel while a request waits


def test_cancel_stops_a_throttle_pause_before_the_request_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _record_sleeps(monkeypatch)
    client, calls = _client(monkeypatch, [(200, "OK", {"Retry-After": "60"}, b"{}")])
    client.get("/_apis/projects")
    checks = iter([False, True])
    client.cancel_waits = lambda: next(checks)
    with pytest.raises(RunCancelled):
        client.post("/_apis/wit/workitems/$Task", [], idempotent=False)
    assert calls == ["GET"]
    assert sleeps == [ado_client.CANCEL_POLL_S]


def test_cancel_stops_a_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_sleeps(monkeypatch)
    client, calls = _client(monkeypatch, [(500, "Server Error", {}, b"")])
    client.cancel_waits = lambda: True
    with pytest.raises(RunCancelled):
        client.get("/_apis/projects")
    assert calls == ["GET"]


def test_no_cancel_check_means_one_plain_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _record_sleeps(monkeypatch)
    client, _calls = _client(
        monkeypatch, [(200, "OK", {"Retry-After": "7"}, b"{}"), (200, "OK", {}, b"{}")]
    )
    client.get("/_apis/projects")
    client.get("/_apis/projects")
    assert len(sleeps) == 1 and 6.5 < sleeps[0] <= 7.0


def test_create_cancelled_while_waiting_is_not_started_and_its_children_wait(tmp_path: Path) -> None:
    class FakeClient:
        def create_work_item(self, project, wit, fields, **kwargs):
            raise RunCancelled("Run cancelled by user")

    events: list[str] = []
    id_map, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        [_draft("root", row=1), _draft("child", parent_key="root", row=2)],
        project="Demo",
        id_map={},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda _s, msg: events.append(msg),
        should_cancel=lambda: True,
    )
    assert id_map == {} and failures == []
    assert stats["cancelled"] == 2 and stats["failed"] == 0 and stats["missing_parent"] == 0
    assert any("Cancelled: 2 work item(s) not started" in e for e in events)


def test_cancel_during_the_lookup_after_a_lost_create_is_not_treated_as_not_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(catalog_runtime, "RECONCILE_DELAYS_S", (0.0,))

    class FakeClient:
        def create_work_item(self, project, wit, fields, **kwargs):
            raise AdoError("Network error (outcome unknown)", transient=True, outcome_unknown=True)

        def find_work_item_ids(self, *args, **kwargs):
            raise RunCancelled("Run cancelled by user")

    events: list[str] = []
    _, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        [_draft("root")],
        project="Demo",
        id_map={},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda _s, msg: events.append(msg),
    )
    assert failures == [] and stats["cancelled"] == 1
    assert any("Cancelled while checking whether 1 create(s)" in e for e in events)


def test_state_moves_cancelled_while_waiting_stay_pending(tmp_path: Path) -> None:
    class FakeClient:
        def create_work_item(self, project, wit, fields, **kwargs):
            if "System.State" in fields:
                raise AdoError(
                    "ADO 400 Bad Request: The field 'System.State' contains the value 'New' "
                    "that is not in the list of supported values",
                    status=400,
                )
            return {"id": 101}

        def update_work_items_batch(self, updates):
            raise RunCancelled("Run cancelled by user")

    draft = _draft("tc1", wit="Test Case")
    draft.fields["System.State"] = "New"
    events: list[str] = []
    _, failures, stats = import_drafts_parent_aware(
        FakeClient(),  # type: ignore[arg-type]
        [draft],
        project="Demo",
        id_map={},
        id_map_path=tmp_path / "ado-id-map.csv",
        parallel_workers=1,
        emit=lambda _s, msg: events.append(msg),
    )
    assert failures == [] and stats["created"] == 1
    assert stats["state_cancelled"] == 1 and stats["state_not_set"] == 0
    with (tmp_path / PENDING_STATES_CSV).open(encoding="utf-8") as handle:
        assert [row["Status"] for row in csv.DictReader(handle)] == ["pending"]
    assert any("the next Catalog Import moves them" in e for e in events)
