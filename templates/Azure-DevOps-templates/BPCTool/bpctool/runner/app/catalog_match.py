"""Workbook rows already in Azure DevOps under another key, matched by Microsoft ID.

Catalog Import resumes by key (Process sequence ID). When a release renumbers a row,
its key is new but its Microsoft ID is already on a work item an earlier import created.
Before creating new rows, look their Microsoft IDs up in the project and reuse such items.
"""

from __future__ import annotations

import csv
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ado_client import AdoClient
from .catalog_import import MICROSOFT_ID_HEADER, WorkItemDraft, catalog_id_headers, catalog_id_values
from .catalog_runtime import ID_MAP_FIELDS, read_id_map_rows
from .csv_safe import SafeDictWriter
from .failure_explain import SEVERAL_MATCHES_PREFIX

Emit = Callable[[str, str], None]

MICROSOFT_ID = "MSBPC.microsoftid"
SEQUENCE_ID = "MSBPC.processsequenceid"
MATCH_FIELDS = ["System.Id", "System.WorkItemType", "System.Title", MICROSOFT_ID, SEQUENCE_ID]
WIQL_VALUES_PER_QUERY = 200
BATCH_SIZE = 200
MATCHED_CSV = "matched-by-microsoft-id.csv"


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


@dataclass
class Reuse:
    draft: WorkItemDraft
    work_item_id: int
    previous_keys: list[str]
    title_in_ado: str = ""


@dataclass
class Several:
    draft: WorkItemDraft
    work_item_ids: list[int]


@dataclass
class ExistingMatches:
    reused: list[Reuse] = field(default_factory=list)
    several: list[Several] = field(default_factory=list)
    # Rows still in the workbook whose ado-id-map.csv item now belongs to another row
    # (their key was given a new Microsoft ID); they are created as new items.
    released: list[tuple[WorkItemDraft, int]] = field(default_factory=list)
    checked: int = 0

    @property
    def reused_ids(self) -> set[int]:
        return {match.work_item_id for match in self.reused}

    @property
    def held_keys(self) -> dict[str, str]:
        """Rows not to create, with the reason recorded as their failure."""
        return {
            s.draft.key: SEVERAL_MATCHES_PREFIX + ", ".join(f"#{i}" for i in s.work_item_ids)
            for s in self.several
        }


def find_existing_by_microsoft_id(
    reader: AdoClient,
    drafts: list[WorkItemDraft],
    id_map: dict[str, int],
    *,
    project: str,
    retired: list[WorkItemDraft] = (),
    complete_workbook: bool = True,
    workers: int = 4,
    emit: Emit,
) -> ExistingMatches:
    """Match rows not in ``id_map`` to work items with the same type and Microsoft ID.

    An item is free to reuse when no workbook row still claims it: it isn't in the map,
    or every key mapped to it is gone from the workbook or now carries another Microsoft ID.
    ``retired`` rows (skipped Deprecated / Deleted) still claim their items. When the
    workbook is capped (``complete_workbook`` False), keys outside it also keep their items.
    """
    result = ExistingMatches()
    pending = [d for d in drafts if d.key not in id_map and _norm(d.fields.get(MICROSOFT_ID))]
    if not pending:
        return result
    result.checked = len(pending)
    if not reader.has_work_item_with_field(project, MICROSOFT_ID):
        return result

    current = {d.key: d for d in drafts}
    claims: dict[str, tuple[str, str]] = {
        d.key: (_norm(d.work_item_type), _norm(d.fields.get(MICROSOFT_ID))) for d in [*drafts, *retired]
    }
    working = dict(id_map)
    used: set[int] = set()
    # A released row is looked up too: its new Microsoft ID may be on another changed key's item.
    while pending:
        items = _items_with_microsoft_ids(reader, pending, project=project, workers=workers)
        keys_by_id: dict[int, list[str]] = {}
        for key, work_item_id in working.items():
            keys_by_id.setdefault(work_item_id, []).append(key)

        by_both: dict[tuple[str, str, str], list[int]] = {}
        by_id: dict[tuple[str, str], list[int]] = {}
        for work_item_id, fields in items.items():
            identity = (_norm(fields.get("System.WorkItemType")), _norm(fields.get(MICROSOFT_ID)))
            owners = keys_by_id.get(work_item_id, [])
            if work_item_id in used or _claimed(owners, identity, claims, complete_workbook):
                continue
            by_both.setdefault((*identity, _norm(fields.get(SEQUENCE_ID))), []).append(work_item_id)
            by_id.setdefault(identity, []).append(work_item_id)

        released_now: list[WorkItemDraft] = []
        for draft in pending:
            identity = (_norm(draft.work_item_type), _norm(draft.fields.get(MICROSOFT_ID)))
            # Same sequence ID too first, as the change report matches.
            candidates = by_both.get((*identity, _norm(draft.fields.get(SEQUENCE_ID)))) or by_id.get(identity, [])
            candidates = [i for i in candidates if i not in used]
            if len(candidates) > 1:
                used.update(candidates)
                result.several.append(Several(draft, sorted(candidates)))
            elif candidates:
                work_item_id = candidates[0]
                used.add(work_item_id)
                previous = sorted(keys_by_id.get(work_item_id, []))
                result.reused.append(
                    Reuse(draft, work_item_id, previous, str(items[work_item_id].get("System.Title") or ""))
                )
                for key in previous:
                    working.pop(key, None)
                    if key in current:
                        result.released.append((current[key], work_item_id))
                        released_now.append(current[key])
                working[draft.key] = work_item_id
        pending = [d for d in released_now if _norm(d.fields.get(MICROSOFT_ID))]
    settled = {m.draft.key for m in result.reused} | {s.draft.key for s in result.several}
    result.released = [(d, wid) for d, wid in result.released if d.key not in settled]
    if result.reused or result.several:
        emit(
            "catalog",
            f"Microsoft ID check: {len(result.reused)} of {result.checked} new row(s) are already in "
            f"Azure DevOps under another key"
            + (f"; {len(result.several)} match several work items" if result.several else ""),
        )
    return result


def _claimed(
    owners: list[str], identity: tuple[str, str], claims: dict[str, tuple[str, str]], complete_workbook: bool
) -> bool:
    """True when a key mapped to the item is a workbook row with the item's type and Microsoft ID."""
    for key in owners:
        if key in claims:
            if claims[key] == identity:
                return True
        elif not complete_workbook:
            return True
    return False


def _items_with_microsoft_ids(
    reader: AdoClient, drafts: list[WorkItemDraft], *, project: str, workers: int
) -> dict[int, dict[str, Any]]:
    values = sorted({str(d.fields.get(MICROSOFT_ID)).strip() for d in drafts})
    chunks = [values[i : i + WIQL_VALUES_PER_QUERY] for i in range(0, len(values), WIQL_VALUES_PER_QUERY)]
    ids: set[int] = set()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks)))) as pool:
        for found in pool.map(
            lambda chunk: reader.find_work_items_by_field_values(project, MICROSOFT_ID, chunk), chunks
        ):
            ids.update(found)
    ordered = sorted(ids)
    batches = [ordered[i : i + BATCH_SIZE] for i in range(0, len(ordered), BATCH_SIZE)]
    items: dict[int, dict[str, Any]] = {}
    if not batches:
        return items
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
        for batch in pool.map(
            lambda chunk: reader.get_work_items_batch(project, chunk, fields=MATCH_FIELDS), batches
        ):
            for item in batch:
                items[int(item["id"])] = item.get("fields") or {}
    return items


def apply_to_id_map(matches: ExistingMatches, id_map: dict[str, int], id_map_path: Path) -> dict[str, int]:
    """Point reused items at their new keys in memory and in ado-id-map.csv.

    Rows for the item's previous keys are dropped, so each item keeps one key and a
    released key is created as a new item. ParentId is left empty so backfill checks the link.
    """
    if not matches.reused:
        return id_map
    reused_ids = matches.reused_ids
    updated = {key: wid for key, wid in id_map.items() if wid not in reused_ids}
    rows = [
        row
        for row in read_id_map_rows(id_map_path)
        if not str(row.get("WorkItemId") or "").strip().isdigit()
        or int(str(row["WorkItemId"]).strip()) not in reused_ids
    ]
    for match in matches.reused:
        updated[match.draft.key] = match.work_item_id
        rows.append(
            {
                "Key": match.draft.key,
                "WorkItemId": match.work_item_id,
                "WorkItemType": match.draft.work_item_type,
                "Title": match.draft.title,
                "ParentKey": match.draft.parent_key or "",
                "ParentId": "",
                **catalog_id_values(match.draft),
            }
        )
    temp = id_map_path.with_name(id_map_path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ID_MAP_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, id_map_path)
    return updated


MATCHED_FIELDS = [
    "Action",
    "Key",
    "Work item ID",
    "Work item type",
    "Title",
    MICROSOFT_ID_HEADER,
    "Previous key",
    "Source file",
    "Source row",
    *catalog_id_headers(without=(MICROSOFT_ID_HEADER,)),
]


def write_matched_csv(matches: ExistingMatches, path: Path) -> None:
    if not (matches.reused or matches.several):
        path.unlink(missing_ok=True)
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = SafeDictWriter(handle, fieldnames=MATCHED_FIELDS)
        writer.writeheader()
        for match in matches.reused:
            writer.writerow(
                {
                    "Action": "Reused the existing work item (added to ado-id-map.csv under this key)",
                    "Key": match.draft.key,
                    "Work item ID": match.work_item_id,
                    "Work item type": match.draft.work_item_type,
                    "Title": match.draft.title,
                    MICROSOFT_ID_HEADER: match.draft.fields.get(MICROSOFT_ID) or "",
                    "Previous key": "; ".join(match.previous_keys),
                    "Source file": match.draft.source_file,
                    "Source row": match.draft.source_row,
                    **catalog_id_values(match.draft, without=(MICROSOFT_ID_HEADER,)),
                }
            )
        for draft, work_item_id in matches.released:
            writer.writerow(
                {
                    "Action": "Key now has another Microsoft ID: its earlier work item went to the row "
                    "with that item's Microsoft ID, and this row is created as a new item",
                    "Key": draft.key,
                    "Work item ID": work_item_id,
                    "Work item type": draft.work_item_type,
                    "Title": draft.title,
                    MICROSOFT_ID_HEADER: draft.fields.get(MICROSOFT_ID) or "",
                    "Previous key": "",
                    "Source file": draft.source_file,
                    "Source row": draft.source_row,
                    **catalog_id_values(draft, without=(MICROSOFT_ID_HEADER,)),
                }
            )
        for item in matches.several:
            writer.writerow(
                {
                    "Action": "Not created: several work items already have this Microsoft ID",
                    "Key": item.draft.key,
                    "Work item ID": ", ".join(str(i) for i in item.work_item_ids),
                    "Work item type": item.draft.work_item_type,
                    "Title": item.draft.title,
                    MICROSOFT_ID_HEADER: item.draft.fields.get(MICROSOFT_ID) or "",
                    "Previous key": "",
                    "Source file": item.draft.source_file,
                    "Source row": item.draft.source_row,
                    **catalog_id_values(item.draft, without=(MICROSOFT_ID_HEADER,)),
                }
            )
