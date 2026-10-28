"""Catalog import runtime: resume id-map, parent-aware parallel create, backfill."""

from __future__ import annotations

import csv
import heapq
import threading
import time
from collections import defaultdict
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .ado_client import WORK_ITEM_BATCH_LIMIT, AdoClient, AdoError, parent_id_from_relations
from .catalog_import import WorkItemDraft
from .csv_safe import SafeDictWriter

Emit = Callable[[str, str], None]
Progress = Callable[[dict[str, Any]], None]
ShouldCancel = Callable[[], bool]

ID_MAP_FIELDS = [
    "Key",
    "WorkItemId",
    "WorkItemType",
    "Title",
    "ParentKey",
    "ParentId",
]

# Field set on every draft (defaults to the row key) and used to find a work
# item whose create outcome is unknown.
RECONCILE_FIELD_REF = "MSBPC.microsoftid"
# Waits before each lookup: a slow create can take a while to show up in WIQL.
RECONCILE_DELAYS_S = (2.0, 5.0, 10.0, 15.0)
PROGRESS_INTERVAL_S = 1.0
TEXT_PROGRESS_EVERY = 500
STATE_FIELD_REF = "System.State"
STATE_NOT_SET_EXAMPLES = 5
PENDING_STATES_CSV = "pending-state-updates.csv"
PENDING_STATE_FIELDS = ["Key", "WorkItemId", "WorkItemType", "State", "Status", "Message"]
STATE_MOVE_DONE = "done"
STATE_BATCH_SIZE = WORK_ITEM_BATCH_LIMIT
# Relations come with every field (Azure DevOps won't narrow an expanded read), so
# keep each read well under the 200-item limit.
BACKFILL_BATCH = 100

BACKFILL_REPORT_FIELDS = [
    "Key",
    "Parent Key",
    "ADO ID",
    "Parent ADO ID",
    "Work Item Type",
    "Title",
    "Status",
    "Message",
]


def clamp_parallel_workers(value: int | None, *, default: int = 4) -> int:
    try:
        workers = int(value) if value is not None else default
    except (TypeError, ValueError):
        workers = default
    return max(1, min(workers, 16))


def _never_cancel() -> bool:
    return False


def read_id_map_rows(path: Path) -> list[dict[str, str]]:
    return read_csv_rows(path)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    # utf-8-sig: Excel's "CSV UTF-8" adds a BOM, which would otherwise end up in the Key header.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_id_map(path: Path) -> dict[str, int]:
    """Load Key → ADO work item id from ado-id-map.csv (resume)."""
    return _ids_from_rows(read_id_map_rows(path))


def _ids_from_rows(rows: list[dict[str, str]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for row in rows:
        key = str(row.get("Key") or "").strip()
        raw_id = str(row.get("WorkItemId") or "").strip()
        if not key or not raw_id:
            continue
        try:
            mapping[key] = int(raw_id)
        except ValueError:
            continue
    return mapping


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


def load_id_map_parents(path: Path) -> dict[str, int]:
    """Key → ParentId known to be linked (in the create payload, or confirmed by backfill).

    Only rows for the key's current work item count: a parent recorded for an item the
    key no longer points at says nothing about the new one.
    """
    rows = read_id_map_rows(path)
    current = _ids_from_rows(rows)
    parents: dict[str, int] = {}
    for row in rows:
        key = str(row.get("Key") or "").strip()
        parent_id = _int_or_none(row.get("ParentId"))
        if key and parent_id is not None and _int_or_none(row.get("WorkItemId")) == current.get(key):
            parents[key] = parent_id
    return parents


def open_id_map_for_append(path: Path) -> tuple[Any, csv.DictWriter]:
    """Open id-map CSV for append, writing header only when creating a new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.is_file() or path.stat().st_size == 0
    # A map saved by Excel may lack a final newline; the next row would join the last one.
    needs_newline = False
    if not write_header:
        with path.open("rb") as existing:
            existing.seek(-1, 2)
            needs_newline = existing.read(1) not in (b"\n", b"\r")
    handle = path.open("a", encoding="utf-8", newline="")
    if needs_newline:
        handle.write("\r\n")
    writer = csv.DictWriter(handle, fieldnames=ID_MAP_FIELDS)
    if write_header:
        writer.writeheader()
        handle.flush()
    return handle, writer


def append_id_map_row(
    writer: csv.DictWriter,
    handle: Any,
    draft: WorkItemDraft,
    work_item_id: int,
    parent_id: int | None,
    *,
    lock: threading.Lock | None = None,
) -> None:
    row = {
        "Key": draft.key,
        "WorkItemId": work_item_id,
        "WorkItemType": draft.work_item_type,
        "Title": draft.title,
        "ParentKey": draft.parent_key or "",
        "ParentId": parent_id or "",
    }

    def _write() -> None:
        writer.writerow(row)
        handle.flush()

    if lock is None:
        _write()
    else:
        with lock:
            _write()


def _draft_order(draft: WorkItemDraft) -> tuple[str, int, str]:
    return (draft.source_file, draft.source_row, draft.key)


class ReadyQueue:
    """Parent-aware scheduler: children become ready when their parent is created.

    Ready drafts pop in source order (file, row, key). Drafts whose parent
    never gets created stay in ``remaining()``.
    """

    def __init__(self, pending: dict[str, WorkItemDraft], id_map: dict[str, int]) -> None:
        self._pending = dict(pending)
        self._children: dict[str, list[str]] = defaultdict(list)
        self._heap: list[tuple[tuple[str, int, str], str]] = []
        for key, draft in self._pending.items():
            if not draft.parent_key or draft.parent_key in id_map:
                heapq.heappush(self._heap, (_draft_order(draft), key))
            else:
                self._children[draft.parent_key].append(key)

    def pop(self, limit: int) -> list[WorkItemDraft]:
        batch: list[WorkItemDraft] = []
        while self._heap and len(batch) < limit:
            _, key = heapq.heappop(self._heap)
            draft = self._pending.pop(key, None)
            if draft is not None:
                batch.append(draft)
        return batch

    def parent_created(self, key: str) -> None:
        for child_key in self._children.pop(key, []):
            draft = self._pending.get(child_key)
            if draft is not None:
                heapq.heappush(self._heap, (_draft_order(draft), child_key))

    def remaining(self) -> list[WorkItemDraft]:
        return sorted(self._pending.values(), key=_draft_order)


class _ProgressTracker:
    def __init__(
        self,
        *,
        phase: str,
        total: int,
        baseline: int,
        counts: dict[str, int],
        emit: Emit,
        progress: Progress | None,
        text_label: str,
    ) -> None:
        self.phase = phase
        self.total = total
        self.baseline = baseline
        self.done = baseline
        self.counts = counts
        self.emit = emit
        self.progress = progress
        self.text_label = text_label
        self.started = time.monotonic()
        self._last_sent = 0.0

    def advance(self) -> None:
        self.done += 1
        if self.done % TEXT_PROGRESS_EVERY == 0:
            summary = ", ".join(f"{k} {v}" for k, v in self.counts.items() if v)
            self.emit(
                "catalog",
                f"{self.text_label} {self.done}/{self.total}"
                + (f" ({summary})" if summary else "")
                + "...",
            )
        self.flush(force=False)

    def flush(self, *, force: bool = True) -> None:
        if self.progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_sent < PROGRESS_INTERVAL_S:
            return
        self._last_sent = now
        self.progress(
            {
                "phase": self.phase,
                "done": self.done,
                "total": self.total,
                "baseline": self.baseline,
                "elapsedMs": int((now - self.started) * 1000),
                **self.counts,
            }
        )


def import_drafts_parent_aware(
    client: AdoClient,
    drafts: list[WorkItemDraft],
    *,
    project: str,
    id_map: dict[str, int],
    id_map_path: Path,
    parallel_workers: int,
    emit: Emit,
    progress: Progress | None = None,
    should_cancel: ShouldCancel | None = None,
    hold: dict[str, str] | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]], dict[str, int]]:
    """
    Create drafts with resume + parent-aware parallelism.

    ``hold``: keys not to create, each recorded as a failure with its reason; their
    children are reported as blocked by parent.

    Returns (id_map, failures, stats) where stats has created/skipped/linked/failed/
    missing_parent (stuck without parent)/cancelled (not started before cancel)/
    reconciled (outcome-unknown creates resolved by lookup)/outcome_unknown.
    """
    cancel = should_cancel or _never_cancel
    workers = clamp_parallel_workers(parallel_workers)
    stats = {
        "created": 0,
        "skipped": 0,
        "linked": 0,
        "failed": 0,
        "missing_parent": 0,
        "cancelled": 0,
        "reconciled": 0,
        "outcome_unknown": 0,
    }
    failures: list[dict[str, Any]] = []
    later_state = StateAfterCreate(
        client, project=project, log_path=id_map_path.with_name(PENDING_STATES_CSV), emit=emit
    )
    try:
        later_state.resume(drafts, id_map)
        _create_drafts(
            client,
            drafts,
            project=project,
            id_map=id_map,
            id_map_path=id_map_path,
            workers=workers,
            emit=emit,
            progress=progress,
            cancel=cancel,
            hold=hold or {},
            stats=stats,
            failures=failures,
            later_state=later_state,
        )
    finally:
        # Runs on cancel too: queued moves are for items that already exist.
        later_state.finish()
    stats["state_set_after_create"] = later_state.set_after_create
    stats["state_not_set"] = later_state.not_set
    stats["state_not_set_examples"] = later_state.not_set_examples
    stats["state_resumed"] = later_state.resumed
    if later_state.set_after_create:
        emit(
            "catalog",
            f"{later_state.set_after_create} work item(s) were created in their type's first state "
            "and then moved to their State (Azure DevOps accepts only the first state when "
            "creating, e.g. Test Case 'New')",
        )
    if later_state.not_set:
        emit(
            "catalog",
            f"WARNING: {later_state.not_set} work item(s) were created but couldn't be moved to "
            f"their State; they stay in their type's first state. They're listed in "
            f"{PENDING_STATES_CSV}, and re-running Catalog Import tries again.",
        )
    if stats["outcome_unknown"]:
        emit(
            "catalog",
            f"{stats['outcome_unknown']} create(s) had a network error and couldn't be confirmed "
            "afterwards, so they weren't sent again. Re-run Catalog Import to pick them up "
            "(see import-failures.csv).",
        )
    return id_map, failures, stats


def _create_drafts(
    client: AdoClient,
    drafts: list[WorkItemDraft],
    *,
    project: str,
    id_map: dict[str, int],
    id_map_path: Path,
    workers: int,
    emit: Emit,
    progress: Progress | None,
    cancel: ShouldCancel,
    hold: dict[str, str],
    stats: dict[str, int],
    failures: list[dict[str, Any]],
    later_state: StateAfterCreate,
) -> None:
    already = [d for d in drafts if d.key in id_map]
    stats["skipped"] = len(already)
    if already:
        emit(
            "catalog",
            f"Resume: skipping {len(already)} work item(s) already in {id_map_path.name}",
        )

    pending = {d.key: d for d in drafts if d.key not in id_map}
    for key, reason in (hold or {}).items():
        draft = pending.pop(key, None)
        if draft is not None:
            stats["failed"] += 1
            failures.append(_failure(draft, reason))
    if not pending:
        emit("catalog", "Resume: nothing left to create")
        return

    emit(
        "catalog",
        f"Creating {len(pending)} work item(s) with {workers} parent-aware worker(s)",
    )

    counts = {"created": 0, "skipped": stats["skipped"], "failed": stats["failed"]}
    tracker = _ProgressTracker(
        phase="create",
        total=len(drafts),
        baseline=stats["skipped"] + stats["failed"],
        counts=counts,
        emit=emit,
        progress=progress,
        text_label="Imported",
    )
    tracker.flush()

    queue = ReadyQueue(pending, id_map)
    handle, writer = open_id_map_for_append(id_map_path)
    lock = threading.Lock()
    map_lock = threading.Lock()

    def mapped_ids() -> set[int]:
        with map_lock:
            return set(id_map.values())

    in_flight: dict[Future, WorkItemDraft] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while True:
                if not cancel():
                    for draft in queue.pop(workers - len(in_flight)):
                        parent_id = id_map.get(draft.parent_key) if draft.parent_key else None
                        future = executor.submit(
                            _create_one,
                            client,
                            draft,
                            project=project,
                            parent_id=parent_id,
                            mapped_ids=mapped_ids,
                            later_state=later_state,
                        )
                        in_flight[future] = draft
                if not in_flight:
                    break
                completed, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in completed:
                    draft = in_flight.pop(future)
                    try:
                        work_item_id, linked_parent_id, reconciled = future.result()
                    except (AdoError, KeyError, TypeError, ValueError) as exc:
                        stats["failed"] += 1
                        if getattr(exc, "outcome_unknown", False):
                            stats["outcome_unknown"] += 1
                        failures.append(_failure(draft, str(exc)))
                    else:
                        with map_lock:
                            id_map[draft.key] = work_item_id
                        append_id_map_row(
                            writer, handle, draft, work_item_id, linked_parent_id, lock=lock
                        )
                        stats["created"] += 1
                        if linked_parent_id is not None:
                            stats["linked"] += 1
                        if reconciled:
                            stats["reconciled"] += 1
                        queue.parent_created(draft.key)
                    counts["created"] = stats["created"]
                    counts["failed"] = stats["failed"]
                    tracker.advance()
    finally:
        handle.close()

    leftover = queue.remaining()
    cancelled_keys = _blocked_only_by_unstarted(leftover, id_map) if cancel() else set()
    for draft in leftover:
        if draft.key in cancelled_keys:
            stats["cancelled"] += 1
            continue
        stats["missing_parent"] += 1
        stats["failed"] += 1
        failures.append(_failure(draft, f"Parent not imported: {draft.parent_key}"))
    if stats["cancelled"]:
        emit(
            "catalog",
            f"Cancelled: {stats['cancelled']} work item(s) not started; "
            f"re-run resumes from {id_map_path.name}",
        )
    counts["failed"] = stats["failed"]
    tracker.flush()


def _blocked_only_by_unstarted(
    leftover: list[WorkItemDraft], id_map: dict[str, int]
) -> set[str]:
    """Keys whose ancestor chain reaches an imported item (or a root) via unstarted drafts.

    Those were stopped by cancel; the rest descend from a parent that failed or is missing.
    """
    remaining = {draft.key: draft for draft in leftover}
    result: set[str] = set()
    for draft in leftover:
        seen = {draft.key}
        parent = draft.parent_key
        while parent in remaining and parent not in seen:
            seen.add(parent)
            parent = remaining[parent].parent_key
        if not parent or parent in id_map:
            result.add(draft.key)
    return result


@dataclass(frozen=True)
class _StateMove:
    key: str
    work_item_id: int
    work_item_type: str
    state: str
    source_file: str = ""
    source_row: int | str = ""


class StateAfterCreate:
    """States Azure DevOps refuses on create (e.g. Test Case 'New': only the type's first
    state is accepted then). Those rows are created without State and moved to it in
    ``$batch`` requests sent from a background thread.

    Each move is logged to pending-state-updates.csv as ``pending`` before it is queued
    and as ``done`` once Azure DevOps confirms it. Moves a stopped run never sent, or that
    failed, are sent again by the next run (:meth:`resume`).
    """

    def __init__(
        self,
        client: AdoClient,
        *,
        project: str,
        log_path: Path,
        emit: Emit,
    ) -> None:
        self._client = client
        self._project = project
        self._log_path = log_path
        self._emit = emit
        self._batch_size = max(1, STATE_BATCH_SIZE)
        self._lock = threading.Lock()
        self._rejected: set[tuple[str, str]] = set()
        self._queue: list[_StateMove] = []
        self._sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-moves")
        self._sent: list[Future] = []
        self._use_batch = True
        self._log_handle: Any = None
        self._log_writer: csv.DictWriter | None = None
        self.set_after_create = 0
        self.not_set = 0
        self.resumed = 0
        self.not_set_examples: list[dict[str, Any]] = []

    @staticmethod
    def _pair(draft: WorkItemDraft, state: str) -> tuple[str, str]:
        return draft.work_item_type.casefold(), state.casefold()

    def rejected(self, draft: WorkItemDraft, state: str) -> bool:
        with self._lock:
            return self._pair(draft, state) in self._rejected

    def remember(self, draft: WorkItemDraft, state: str) -> None:
        with self._lock:
            self._rejected.add(self._pair(draft, state))

    def resume(self, drafts: list[WorkItemDraft], id_map: dict[str, int]) -> int:
        """Queue moves an earlier run logged but never confirmed; returns how many.

        A move is dropped once its key points at another work item. The workbook's
        current State wins over the logged one.
        """
        latest: dict[tuple[str, int], dict[str, str]] = {}
        for row in read_csv_rows(self._log_path):
            key = str(row.get("Key") or "").strip()
            work_item_id = _int_or_none(row.get("WorkItemId"))
            if key and work_item_id is not None:
                latest[(key, work_item_id)] = row
        drafts_by_key = {draft.key: draft for draft in drafts}
        moves: list[_StateMove] = []
        for (key, work_item_id), row in latest.items():
            if str(row.get("Status") or "").strip() == STATE_MOVE_DONE or id_map.get(key) != work_item_id:
                continue
            draft = drafts_by_key.get(key)
            state = str((draft.fields.get(STATE_FIELD_REF) if draft else row.get("State")) or "").strip()
            if not state:
                continue
            moves.append(
                _StateMove(
                    key,
                    work_item_id,
                    draft.work_item_type if draft else str(row.get("WorkItemType") or ""),
                    state,
                    draft.source_file if draft else "",
                    draft.source_row if draft else "",
                )
            )
        if moves:
            self._emit(
                "catalog",
                f"Moving {len(moves)} work item(s) to their State left pending by an earlier run "
                f"({self._log_path.name})",
            )
            with self._lock:
                self.resumed += len(moves)
                for move in moves:
                    self._enqueue_locked(move)
        return len(moves)

    def defer(self, draft: WorkItemDraft, work_item_id: int, state: str) -> None:
        move = _StateMove(
            draft.key, work_item_id, draft.work_item_type, state, draft.source_file, draft.source_row
        )
        with self._lock:
            self._log_locked(move, "pending", "")
            self._enqueue_locked(move)

    def finish(self) -> None:
        """Send what is still queued and wait for every batch."""
        with self._lock:
            self._submit_locked()
            sent, self._sent = self._sent, []
        try:
            for future in sent:
                future.result()
        finally:
            self._sender.shutdown(wait=True)
            with self._lock:
                if self._log_handle is not None:
                    self._log_handle.close()
                    self._log_handle = None
                    self._log_writer = None

    def _enqueue_locked(self, move: _StateMove) -> None:
        self._queue.append(move)
        if len(self._queue) >= self._batch_size:
            self._submit_locked()

    def _submit_locked(self) -> None:
        batch, self._queue = self._queue, []
        if batch:
            self._sent.append(self._sender.submit(self._send, batch))

    def _send(self, batch: list[_StateMove]) -> None:
        errors: list[str | None] | None = None
        if self._use_batch:
            try:
                errors = self._client.update_work_items_batch(
                    [(move.work_item_id, {STATE_FIELD_REF: move.state}) for move in batch]
                )
            except AdoError as exc:
                if not exc.transient:
                    self._use_batch = False
                    self._emit(
                        "catalog",
                        f"Azure DevOps didn't accept a batch State update ({exc}); "
                        "moving States one work item at a time",
                    )
        if errors is None:
            errors = []
            for move in batch:
                try:
                    self._client.update_work_item_fields(
                        self._project, move.work_item_id, {STATE_FIELD_REF: move.state}
                    )
                    errors.append(None)
                except AdoError as exc:
                    errors.append(str(exc))
        with self._lock:
            for move, error in zip(batch, errors, strict=True):
                if error is None:
                    self.set_after_create += 1
                    self._log_locked(move, STATE_MOVE_DONE, "")
                    continue
                self.not_set += 1
                self._log_locked(move, "failed", error[:300])
                if len(self.not_set_examples) < STATE_NOT_SET_EXAMPLES:
                    self.not_set_examples.append(
                        {
                            "key": move.key,
                            "workItemId": move.work_item_id,
                            "workItemType": move.work_item_type,
                            "state": move.state,
                            "sourceFile": move.source_file,
                            "sourceRow": move.source_row,
                            "error": error[:300],
                        }
                    )

    def _log_locked(self, move: _StateMove, status: str, message: str) -> None:
        if self._log_writer is None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self._log_path.is_file() or self._log_path.stat().st_size == 0
            self._log_handle = self._log_path.open("a", encoding="utf-8", newline="")
            self._log_writer = csv.DictWriter(self._log_handle, fieldnames=PENDING_STATE_FIELDS)
            if new_file:
                self._log_writer.writeheader()
        self._log_writer.writerow(
            {
                "Key": move.key,
                "WorkItemId": move.work_item_id,
                "WorkItemType": move.work_item_type,
                "State": move.state,
                "Status": status,
                "Message": message,
            }
        )
        self._log_handle.flush()


def state_rejected_on_create(exc: AdoError) -> bool:
    text = str(exc)
    return exc.status == 400 and STATE_FIELD_REF in text and "not in the list of supported values" in text


def _failure(draft: WorkItemDraft, error: str) -> dict[str, Any]:
    return {
        "key": draft.key,
        "workItemType": draft.work_item_type,
        "title": draft.title,
        "parentKey": draft.parent_key,
        "error": error,
        "sourceFile": draft.source_file,
        "sourceRow": draft.source_row,
    }


def _send_create(
    client: AdoClient,
    draft: WorkItemDraft,
    *,
    project: str,
    parent_id: int | None,
    later_state: StateAfterCreate | None = None,
) -> int:
    def create(fields: dict[str, Any]) -> int:
        created = client.create_work_item(
            project,
            draft.work_item_type,
            fields,
            parent_id=parent_id,
            dry_run=False,
            markdown_format_fields=draft.markdown_format_fields,
        )
        return int(created["id"])

    state = str(draft.fields.get(STATE_FIELD_REF) or "").strip()
    if later_state is None or not state:
        return create(draft.fields)
    if not later_state.rejected(draft, state):
        try:
            return create(draft.fields)
        except AdoError as exc:
            if not state_rejected_on_create(exc):
                raise
            later_state.remember(draft, state)
    # A 400 means nothing was created, so creating again without State is safe.
    work_item_id = create({ref: value for ref, value in draft.fields.items() if ref != STATE_FIELD_REF})
    later_state.defer(draft, work_item_id, state)
    return work_item_id


def _create_one(
    client: AdoClient,
    draft: WorkItemDraft,
    *,
    project: str,
    parent_id: int | None,
    mapped_ids: Callable[[], set[int]] | None = None,
    later_state: StateAfterCreate | None = None,
) -> tuple[int, int | None, bool]:
    """Return (work item id, parent id linked in the create payload, reconciled).

    ``mapped_ids``: work item ids already in the id map (under other keys), read when
    an unknown-outcome create is looked up.
    """
    try:
        work_item_id = _send_create(
            client, draft, project=project, parent_id=parent_id, later_state=later_state
        )
        return work_item_id, parent_id, False
    except AdoError as exc:
        if not exc.outcome_unknown:
            raise
        original = exc

    # After an unknown-outcome create, record no ParentId so backfill verifies the link.
    existing = _find_existing_after_unknown_create(
        client, draft, project=project, original=original, claimed=mapped_ids
    )
    if existing is not None:
        state = str(draft.fields.get(STATE_FIELD_REF) or "").strip()
        if later_state is not None and state and later_state.rejected(draft, state):
            later_state.defer(draft, existing, state)
        return existing, None, True
    # Never re-sent: Azure DevOps may still be saving the first create.
    raise AdoError(
        f"{original}; the work item didn't show up in Azure DevOps, so it wasn't created again "
        "(that could make a duplicate). Re-run Catalog Import: if the item was created, it is "
        "matched by its Microsoft ID and kept; if not, it is created then.",
        outcome_unknown=True,
    )


def _find_existing_after_unknown_create(
    client: AdoClient,
    draft: WorkItemDraft,
    *,
    project: str,
    original: AdoError,
    claimed: Callable[[], set[int]] | None = None,
) -> int | None:
    """The item the lost create made, ignoring items the id map gives to other rows."""
    ids: list[int] = []
    for delay in RECONCILE_DELAYS_S:
        time.sleep(delay)
        try:
            ids = client.find_work_item_ids(
                project,
                draft.work_item_type,
                title=draft.title,
                field_ref=RECONCILE_FIELD_REF,
                field_value=draft.fields.get(RECONCILE_FIELD_REF),
            )
        except AdoError as lookup_exc:
            raise AdoError(
                f"{original}; couldn't check whether it was created ({lookup_exc}), so it "
                "wasn't created again. Re-run Catalog Import: if the item was created, it is "
                "matched by its Microsoft ID and kept; if not, it is created then.",
                outcome_unknown=True,
            ) from lookup_exc
        if ids and claimed is not None:
            taken = claimed()
            ids = [i for i in ids if i not in taken]
        if ids:
            break
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    raise AdoError(
        f"{original}; {len(ids)} matching work items already exist "
        f"({', '.join(str(i) for i in ids[:5])}). Remove duplicates, then add the "
        "correct id to ado-id-map.csv before re-running.",
        outcome_unknown=True,
    )


def _check_parent_links(
    client: AdoClient, project: str, pairs: list[tuple[int, int]]
) -> list[tuple[str, str]]:
    """(status, message) per (child id, expected parent id); one read for all the children."""
    try:
        items = client.get_work_items_batch(
            project, sorted({child_id for child_id, _ in pairs}), expand_relations=True
        )
        found = {int(item["id"]): item for item in items}
    except (AdoError, KeyError, TypeError, ValueError) as exc:
        return [("failed", f"Couldn't read the work item's links: {exc}")] * len(pairs)
    results: list[tuple[str, str]] = []
    for child_id, expected_parent_id in pairs:
        item = found.get(child_id)
        if item is None:
            results.append(
                ("failed", f"Work item {child_id} wasn't found (deleted, or this PAT can't read it)")
            )
            continue
        results.append(
            _ensure_parent_link(
                client,
                project,
                child_id,
                expected_parent_id,
                parent_id_from_relations(item.get("relations")),
            )
        )
    return results


def _ensure_parent_link(
    client: AdoClient,
    project: str,
    child_id: int,
    expected_parent_id: int,
    current_parent_id: int | None,
) -> tuple[str, str]:
    try:
        if current_parent_id == expected_parent_id:
            return "already_linked", ""
        if current_parent_id is not None:
            return (
                "conflict_existing_parent",
                f"Existing parent {current_parent_id}; expected {expected_parent_id}",
            )
        try:
            client.add_parent_link(project, child_id, expected_parent_id)
        except AdoError as exc:
            if not (exc.outcome_unknown or exc.status == 400):
                raise
            # The link may have been saved before the error (or by a lost earlier attempt).
            if client.get_parent_id(project, child_id) == expected_parent_id:
                return "linked", ""
            raise
        return "linked", ""
    except (AdoError, KeyError, TypeError, ValueError) as exc:
        return "failed", str(exc)


def _record_confirmed_parents(id_map_path: Path, rows: list[dict[str, Any]]) -> int:
    """Append a row with ParentId per confirmed link (the map's last row per key wins)."""
    if not rows:
        return 0
    handle, writer = open_id_map_for_append(id_map_path)
    try:
        for row in rows:
            writer.writerow(
                {
                    "Key": row["Key"],
                    "WorkItemId": row["ADO ID"],
                    "WorkItemType": row["Work Item Type"],
                    "Title": row["Title"],
                    "ParentKey": row["Parent Key"],
                    "ParentId": row["Parent ADO ID"],
                }
            )
    finally:
        handle.close()
    return len(rows)


def backfill_parent_links(
    client: AdoClient,
    drafts: list[WorkItemDraft],
    id_map: dict[str, int],
    *,
    project: str,
    output_dir: Path,
    emit: Emit,
    recorded_parents: dict[str, int] | None = None,
    parallel_workers: int = 4,
    progress: Progress | None = None,
    should_cancel: ShouldCancel | None = None,
    id_map_path: Path | None = None,
) -> dict[str, int]:
    """
    For created items with a known parent id, ensure Hierarchy-Reverse exists.

    Items whose id-map row records the expected ParentId (linked in the create payload,
    or confirmed by an earlier backfill) are reported ``already_linked`` without a read.
    The rest are read ``BACKFILL_BATCH`` at a time, batches in parallel; links found or
    added are appended to ``id_map_path`` so later runs skip them. Writes
    backfill-links-report.csv (reference-script compatible statuses).
    """
    cancel = should_cancel or _never_cancel
    recorded = recorded_parents or {}
    candidates = [
        draft
        for draft in drafts
        if draft.key in id_map and draft.parent_key and draft.parent_key in id_map
    ]
    counts = {
        "linked": 0,
        "already_linked": 0,
        "conflict_existing_parent": 0,
        "failed": 0,
        "candidates": len(candidates),
        "recorded_in_map": 0,
        "checked": 0,
        "newly_recorded": 0,
        "cancelled": 0,
    }
    if not candidates:
        emit("catalog", "Parent-link backfill: no candidates")
        return counts

    rows: list[dict[str, Any]] = []
    to_check: list[int] = []
    for draft in candidates:
        child_id = id_map[draft.key]
        expected_parent_id = id_map[draft.parent_key or ""]
        row = {
            "Key": draft.key,
            "Parent Key": draft.parent_key or "",
            "ADO ID": child_id,
            "Parent ADO ID": expected_parent_id,
            "Work Item Type": draft.work_item_type,
            "Title": draft.title,
            "Status": "planned",
            "Message": "",
        }
        if recorded.get(draft.key) == expected_parent_id:
            row["Status"] = "already_linked"
            row["Message"] = "Recorded in ado-id-map.csv"
            counts["already_linked"] += 1
            counts["recorded_in_map"] += 1
        else:
            to_check.append(len(rows))
        rows.append(row)

    workers = clamp_parallel_workers(parallel_workers)
    batches = [to_check[i : i + BACKFILL_BATCH] for i in range(0, len(to_check), BACKFILL_BATCH)]
    emit(
        "catalog",
        f"Parent-link backfill: {counts['recorded_in_map']} already recorded in ado-id-map.csv; "
        f"checking {len(to_check)} work item(s) in {len(batches)} batch(es) with {workers} worker(s)…",
    )

    if batches:
        tracker = _ProgressTracker(
            phase="backfill",
            total=len(to_check),
            baseline=0,
            counts={"linked": 0, "conflict": 0, "failed": 0},
            emit=emit,
            progress=progress,
            text_label="Parent-link backfill",
        )
        tracker.flush()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _check_parent_links,
                    client,
                    project,
                    [(int(rows[i]["ADO ID"]), int(rows[i]["Parent ADO ID"])) for i in batch],
                ): batch
                for batch in batches
            }
            cancelled = False
            for future in as_completed(futures):
                if not cancelled and cancel():
                    cancelled = True
                    for pending in futures:
                        pending.cancel()
                batch = futures[future]
                try:
                    results = future.result()
                except CancelledError:
                    for index in batch:
                        rows[index]["Message"] = "Not checked (run cancelled)"
                    counts["cancelled"] += len(batch)
                    continue
                for index, (status, message) in zip(batch, results, strict=True):
                    rows[index]["Status"] = status
                    rows[index]["Message"] = message
                    counts[status] += 1
                    counts["checked"] += 1
                    tracker.counts.update(
                        linked=counts["linked"],
                        conflict=counts["conflict_existing_parent"],
                        failed=counts["failed"],
                    )
                    tracker.advance()
        tracker.flush()

    if id_map_path is not None:
        confirmed = [rows[i] for i in to_check if rows[i]["Status"] in ("already_linked", "linked")]
        counts["newly_recorded"] = _record_confirmed_parents(id_map_path, confirmed)
        if counts["newly_recorded"]:
            emit(
                "catalog",
                f"Recorded {counts['newly_recorded']} confirmed parent link(s) in {id_map_path.name}, "
                "so later runs don't check them again",
            )

    report_path = output_dir / "backfill-links-report.csv"
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = SafeDictWriter(handle, fieldnames=BACKFILL_REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    summary = ", ".join(
        f"{k}={v}"
        for k, v in sorted(counts.items())
        if k != "candidates" and v
    )
    emit(
        "catalog",
        f"Parent-link backfill finished: {summary or 'no changes'}; "
        f"wrote {report_path.name}",
    )
    return counts
