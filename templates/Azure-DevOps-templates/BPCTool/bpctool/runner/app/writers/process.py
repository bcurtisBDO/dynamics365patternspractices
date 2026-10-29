"""Apply process shell, work item types, picklists, and custom fields."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from ..ado_client import AdoClient, AdoError
from ..csv_safe import SafeDictWriter

Emit = Callable[[str, str], None]

# Field help text (the field's description) is limited to 255 characters.
FIELD_DESCRIPTION_MAX = 255
PICKLIST_LOOKUP_ATTEMPTS = 4
PICKLIST_LOOKUP_DELAY_S = 2.0
SHORTENED_DESCRIPTIONS_FILE = "descriptions-shortened.csv"
SHORTENED_FIELDS = ("kind", "name", "reference_name", "original_length", "sent_length", "sent", "original")


# Test Case has no "New" state (Design / Ready / Closed). Microsoft's setup adds one so
# catalog rows with State = New import as written. An added state goes first in its
# category; a type's first Proposed state is the only one Azure DevOps accepts on create.
TEST_CASE_REFERENCE = "Microsoft.VSTS.WorkItemTypes.TestCase"
_ADDED_STATES: dict[str, tuple[dict[str, str], ...]] = {
    TEST_CASE_REFERENCE.lower(): ({"name": "New", "color": "b2b2b2", "stateCategory": "Proposed"},),
}


def states_added_by_process(ir: dict[str, Any]) -> dict[str, list[str]]:
    """Work item type name → states the Process step adds (data checks treat them as valid)."""
    added: dict[str, list[str]] = {}
    for wit in ir.get("process", {}).get("work_item_types") or []:
        states = _ADDED_STATES.get(str(wit.get("reference_name") or "").strip().lower())
        if states and not wit.get("disabled") and wit.get("name"):
            added[str(wit["name"])] = [state["name"] for state in states]
    return added


def _first_state(states: list[dict[str, Any]], category: str, *, visible_only: bool) -> dict[str, Any] | None:
    """The lowest-``order`` state of ``category``; None when Azure DevOps didn't return orders."""
    ranked = [
        state
        for state in states
        if str(state.get("stateCategory") or "").casefold() == category.casefold()
        and isinstance(state.get("order"), int)
        and not (visible_only and state.get("hidden"))
    ]
    return min(ranked, key=lambda state: state["order"], default=None)


def _not_first(wit: str, state: str, first: Any, reason: str) -> str:
    return (
        f"State '{state}' added to '{wit}' but '{first}' is still its first state ({reason}). New "
        f"'{wit}' items start in '{first}'; Catalog Import moves rows to '{state}' after create. To "
        f"change it, move '{state}' up in the process's '{wit}' states."
    )


def shorten_description(text: str, limit: int = FIELD_DESCRIPTION_MAX) -> str:
    """Cut at a word boundary and end with an ellipsis so the result fits ``limit``."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def long_field_descriptions(ir: dict[str, Any]) -> list[tuple[str, int]]:
    """Custom fields whose description exceeds the limit: (reference name, length)."""
    found = []
    for field in ir.get("process", {}).get("fields") or []:
        length = len((field.get("description") or "").strip())
        if field.get("custom_field") and field.get("reference_name") and length > FIELD_DESCRIPTION_MAX:
            found.append((str(field["reference_name"]), length))
    return found


def write_shortened_descriptions_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = SafeDictWriter(handle, fieldnames=list(SHORTENED_FIELDS))
        writer.writeheader()
        writer.writerows(rows)


def multiselect_field_labels(ir: dict[str, Any]) -> set[str]:
    """Labels marked Multi-select on the Fields sheet (suggested picklists)."""
    return {
        str(field.get("label") or field.get("field_name") or "").strip()
        for field in ir.get("process", {}).get("fields") or []
        if field.get("multi_select") and (field.get("label") or field.get("field_name"))
    }


def integer_picklist_labels(ir: dict[str, Any]) -> set[str]:
    """Casefolded labels of PicklistInteger fields; their picklists hold integers."""
    return {
        str(field.get("label") or field.get("field_name") or "").strip().casefold()
        for field in ir.get("process", {}).get("fields") or []
        if str(field.get("field_type") or "").strip().casefold() == "picklistinteger"
    }


def apply_process(
    client: AdoClient,
    ir: dict[str, Any],
    *,
    dry_run: bool,
    emit: Emit,
) -> dict[str, Any]:
    process_name = ir["targets"]["process_name"]
    stats = {
        "process_id": None,
        "wits_created": 0,
        "wits_updated": 0,
        "wits_disabled": 0,
        "wits_skipped": 0,
        "picklists": 0,
        "fields_created": 0,
        "fields_existing": 0,
        "fields_skipped": 0,
        "states_added": 0,
        "warnings": 0,
        "descriptions_shortened": [],
    }
    # Items the workbook defines that Azure DevOps refused to create; the step fails on these.
    errors: list[str] = []

    def warn(message: str) -> None:
        stats["warnings"] += 1
        emit("process", f"WARNING: {message}")

    def fail(message: str) -> None:
        errors.append(message)
        emit("process", f"FAILED: {message}")

    def send(
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        api_version: str,
        kind: str,
        name: str,
        original: str | None = None,
    ) -> Any:
        """Send a create / update; a description Azure DevOps rejects is shortened, then dropped."""
        prepared = (payload.get("description") or "").strip()
        created, sent = _send_with_description_fallback(
            client, method, path, payload, api_version=api_version
        )
        workbook_text = (prepared if original is None else original).strip()
        if sent != workbook_text:
            note_description(
                kind,
                name,
                str(payload.get("referenceName") or ""),
                workbook_text,
                sent,
                rejected=sent != prepared,
            )
        return created

    def note_description(
        kind: str, name: str, reference: str, original: str, sent: str, *, rejected: bool
    ) -> None:
        stats["descriptions_shortened"].append(
            {
                "kind": kind,
                "name": name,
                "reference_name": reference,
                "original_length": len(original),
                "sent_length": len(sent),
                "sent": sent,
                "original": original,
            }
        )
        how = f"shortened to {len(sent)} characters" if sent else "left empty"
        why = (
            "because Azure DevOps rejected it"
            if rejected
            else f"to fit Azure DevOps' {FIELD_DESCRIPTION_MAX}-character limit"
        )
        warn(
            f"{kind.capitalize()} '{name}': its {len(original)}-character description was {how} "
            f"{why}. Full text in {SHORTENED_DESCRIPTIONS_FILE}; shorten it in the workbook to choose the wording."
        )

    disabled_count = int(ir["counts"].get("disabled_work_item_types") or 0)

    if dry_run:
        emit(
            "process",
            f"[dry-run] Would ensure process '{process_name}' with "
            f"{ir['counts']['work_item_types']} WITs / {disabled_count} disabled / "
            f"{ir['counts']['fields']} fields / "
            f"{ir['counts']['html_fields']} HTML fields"
            + "".join(
                f"; '{wit}' gets state(s) {', '.join(states)} if missing"
                for wit, states in states_added_by_process(ir).items()
            ),
        )
        return stats

    process_id = client.create_process(process_name)
    stats["process_id"] = process_id
    emit("process", f"Process ready: {process_name} ({process_id})")

    existing = index_process_wits(client.list_process_wits(process_id))

    for wit in ir["process"]["work_item_types"]:
        name = wit["name"]
        custom = bool(wit.get("custom"))
        disabled = bool(wit.get("disabled"))
        ref_name = (wit.get("reference_name") or _build_ref(process_name, name)).strip()
        color = (wit.get("color") or "0078D4").lstrip("#")
        icon = wit.get("icon") or "icon_gear"
        description = wit.get("help_text") or ""

        existing_wit = resolve_process_wit(existing, name, ref_name)
        system_ref = _is_system_reference(ref_name)

        # Disabled system WITs (Epic/Feature/User Story): materialize process-local
        # inheritance then PATCH isDisabled. Layout/backlog skip disabled rows.
        if disabled:
            try:
                process_ref = _ensure_process_local_wit(
                    client,
                    send,
                    process_id,
                    existing,
                    name=name,
                    ref_name=ref_name,
                    color=color,
                    icon=icon,
                    description=description,
                    custom=custom,
                    system_ref=system_ref,
                    stats=stats,
                    emit=emit,
                )
                if not process_ref:
                    stats["wits_skipped"] += 1
                    warn(
                        f"WIT '{name}' is Disabled in Excel but could not be "
                        "materialized for disable — set Disabled in ADO Process UI."
                    )
                    continue
                client.patch(
                    f"/_apis/work/processes/{process_id}/workItemTypes/"
                    f"{quote(str(process_ref), safe='')}",
                    json_body={"isDisabled": True},
                    api_version="7.1-preview.2",
                )
                stats["wits_disabled"] += 1
                emit("process", f"Disabled WIT '{name}' ({process_ref})")
            except AdoError as exc:
                stats["wits_skipped"] += 1
                warn(
                    f"WIT '{name}' disable failed ({exc}). "
                    "Disable it manually in Organization Settings → Process."
                )
            continue

        # Reference-script behavior:
        # - Custom=No + Microsoft.VSTS.* → create process-local WIT inheriting the OOB type
        # - Custom=Yes → create custom WIT; do NOT pass Excel Inherit from by default
        #   (avoids duplicate inherited system WIT conflicts)
        if not custom:
            if not system_ref:
                stats["wits_skipped"] += 1
                continue
            local_ref = _short_reference_name(ref_name)
            inherit_from = ref_name
            create_ref = local_ref
        else:
            inherit_from = ""
            create_ref = _short_reference_name(ref_name) or name.replace(" ", "")

        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "color": color,
            "icon": icon,
            "referenceName": create_ref,
        }
        if inherit_from:
            payload["inheritsFrom"] = inherit_from

        customization = str((existing_wit or {}).get("customization") or "").strip().lower()
        # System entries in the process list are not process-local yet — create/inherit.
        if existing_wit and customization not in {"", "system"}:
            full_ref = existing_wit.get("referenceName") or ref_name
            update: dict[str, Any] = {"description": description, "color": color, "icon": icon}
            reenable = bool(existing_wit.get("isDisabled"))
            if reenable:
                update["isDisabled"] = False
            try:
                send(
                    "PATCH",
                    f"/_apis/work/processes/{process_id}/workItemTypes/{quote(str(full_ref), safe='')}",
                    update,
                    api_version="7.1-preview.2",
                    kind="work item type",
                    name=name,
                )
                stats["wits_updated"] += 1
                if reenable:
                    existing_wit["isDisabled"] = False
                    emit("process", f"Enabled WIT '{name}' again ({full_ref}): the workbook no longer disables it")
            except AdoError as exc:
                if reenable:
                    fail(f"WIT '{name}' is disabled in Azure DevOps and couldn't be enabled again: {exc}")
                else:
                    warn(f"WIT '{name}' exists but its description / color / icon couldn't be updated: {exc}")
        else:
            try:
                created = send(
                    "POST",
                    f"/_apis/work/processes/{process_id}/workItemTypes",
                    payload,
                    api_version="7.1-preview.2",
                    kind="work item type",
                    name=name,
                )
                if created:
                    _index_one_wit(existing, created)
                stats["wits_created"] += 1
            except AdoError as exc:
                if _already_exists(exc):
                    stats["wits_skipped"] += 1
                else:
                    fail(f"WIT '{name}' could not be created: {exc}")

    # Refresh after creates so later steps see process-local refs (Process.Bug, etc.)
    existing = index_process_wits(client.list_process_wits(process_id))

    for wit in ir["process"]["work_item_types"]:
        wanted = _ADDED_STATES.get(str(wit.get("reference_name") or "").strip().lower())
        if not wanted or wit.get("disabled"):
            continue
        name = wit["name"]
        names = " / ".join(f"'{state['name']}'" for state in wanted)
        fallback = f"catalog rows with State {names} start in the type's first state instead"
        local = _process_local_wit(existing, name, wit["reference_name"]) or {}
        local_ref = str(local.get("referenceName") or "").strip()
        if not local_ref:
            warn(f"State {names} not added to '{name}': it isn't customized in this process; {fallback}.")
            continue
        path = f"/_apis/work/processes/{process_id}/workItemTypes/{quote(local_ref, safe='')}/states"
        try:
            current = (client.get(path, api_version="7.1") or {}).get("value") or []
            have = {str(state.get("name") or "").casefold() for state in current}
            for state in wanted:
                if state["name"].casefold() in have:
                    continue
                body = dict(state)
                ahead = _first_state(current, state["stateCategory"], visible_only=False)
                if ahead is not None:
                    body["order"] = ahead["order"]
                try:
                    client.post(path, json_body=body, api_version="7.1")
                except AdoError as exc:
                    if _already_exists(exc):
                        continue
                    if "order" not in body:
                        raise
                    try:
                        client.post(path, json_body=dict(state), api_version="7.1")
                    except AdoError as again:
                        if not _already_exists(again):
                            raise
                    stats["states_added"] += 1
                    warn(_not_first(name, state["name"], ahead.get("name"), f"not placed first: {exc}"))
                    continue
                stats["states_added"] += 1
                emit("process", f"Added state '{state['name']}' ({state['stateCategory']}) to '{name}'")
                if ahead is None:
                    continue
                try:
                    after = (client.get(path, api_version="7.1") or {}).get("value") or []
                except AdoError:
                    continue
                first = _first_state(after, state["stateCategory"], visible_only=True)
                if first is not None and str(first.get("name") or "").casefold() != state["name"].casefold():
                    warn(_not_first(name, state["name"], first.get("name"), "Azure DevOps kept it below"))
        except AdoError as exc:
            warn(f"State {names} couldn't be added to '{name}' ({exc}); {fallback}.")

    # Multi-select Labels (Application family, Products, Industries, …) use
    # suggested-picklist semantics so semicolon-separated values can be stored;
    # layout then binds the DevLabs multivalue control.
    multiselect_labels = multiselect_field_labels(ir)
    if multiselect_labels:
        emit(
            "process",
            "Multi-value picklists (suggested): "
            + ", ".join(sorted(multiselect_labels)),
        )

    # Picklists (Azure DevOps list names ignore case, so they're keyed casefolded)
    lists_data = client.get("/_apis/work/processes/lists", api_version="7.1") or {}
    existing_lists = {
        str(item.get("name") or "").casefold(): item for item in lists_data.get("value") or [] if item.get("name")
    }
    picklist_ids = {key: item["id"] for key, item in existing_lists.items() if item.get("id")}
    failed_picklists: set[str] = set()
    integer_lists = integer_picklist_labels(ir)
    for label, values in ir["process"]["picklists"].items():
        key = label.casefold()
        payload = {
            "name": label,
            "type": "Integer" if key in integer_lists else "String",
            "items": values,
            # Suggested lists accept stored values beyond a single picklist token
            # (semicolon composites for multi-select / DevLabs).
            "isSuggested": label in multiselect_labels,
        }
        if key in existing_lists:
            picklist_id = existing_lists[key]["id"]
            try:
                client.put(
                    f"/_apis/work/processes/lists/{picklist_id}",
                    json_body=payload,
                    api_version="7.1",
                )
                stats["picklists"] += 1
            except AdoError as exc:
                warn(f"Picklist '{label}' exists but couldn't be updated: {exc}")
        else:
            try:
                # Not retried: Azure DevOps can hold two lists with the same name.
                created = (
                    client.post(
                        "/_apis/work/processes/lists", json_body=payload, api_version="7.1", idempotent=False
                    )
                    or {}
                )
                stats["picklists"] += 1
                if created.get("id"):
                    picklist_ids[key] = created["id"]
            except AdoError as exc:
                if exc.outcome_unknown:
                    # Looked up by name below; its fields fail if it wasn't created.
                    emit("process", f"Picklist '{label}': {exc}; checking whether Azure DevOps created it")
                elif not _already_exists(exc):
                    failed_picklists.add(key)
                    fail(f"Picklist '{label}' could not be created: {exc}")

    missing = [
        label
        for label in ir["process"]["picklists"]
        if label.casefold() not in picklist_ids and label.casefold() not in failed_picklists
    ]
    if missing:
        picklist_ids.update(_find_picklist_ids(client, missing))

    fields_data = client.get("/_apis/wit/fields", api_version="7.1") or {}
    existing_fields = {
        str(item.get("referenceName") or "").lower(): item for item in fields_data.get("value") or []
    }

    for field in ir["process"]["fields"]:
        if not field.get("custom_field"):
            stats["fields_skipped"] += 1
            continue
        reference = field.get("reference_name")
        if not reference:
            continue
        field_type = field.get("field_type") or "String"
        label = field.get("label") or field.get("field_name")
        name = field.get("field_name") or label
        workbook_description = (field.get("description") or "").strip()
        description = shorten_description(workbook_description)
        is_multi = bool(field.get("multi_select"))

        if reference.lower() in existing_fields:
            # Organization fields can't be changed after creation (the Fields update API only
            # locks / restores them), so an existing field is used as is; Layout binds it.
            stats["fields_existing"] += 1
            continue

        picklist_type = field_type.casefold()
        if picklist_type in {"pickliststring", "picklistinteger"}:
            picklist_id = picklist_ids.get(str(label).casefold())
            if not picklist_id:
                if str(label).casefold() not in failed_picklists:
                    fail(f"Field '{label}' ({reference}) not created — its picklist '{label}' wasn't found")
                continue
            payload = {
                "name": name,
                "referenceName": reference,
                "type": "integer" if picklist_type == "picklistinteger" else "String",
                "isPicklist": True,
                "isPicklistSuggested": is_multi,
                "picklistId": picklist_id,
                "description": description,
            }
        else:
            payload = {
                "name": name,
                "type": field_type,
                "referenceName": reference,
                "description": description,
            }

        try:
            send(
                "POST",
                "/_apis/wit/fields",
                payload,
                api_version="7.1",
                kind="field",
                name=name,
                original=workbook_description,
            )
            stats["fields_created"] += 1
        except AdoError as exc:
            if _already_exists(exc):
                stats["fields_existing"] += 1
            else:
                fail(f"Field '{reference}' could not be created: {exc}")

    emit(
        "process",
        "Process apply finished: "
        f"WITs +{stats['wits_created']}/~{stats['wits_updated']}/"
        f"disabled {stats['wits_disabled']}, "
        f"fields +{stats['fields_created']} ({stats['fields_existing']} already existed), "
        f"HTML fields in template: {ir['counts'].get('html_fields', 0)}"
        + (f", {stats['warnings']} warning(s)" if stats["warnings"] else ""),
    )
    if errors:
        shown = "; ".join(errors[:3]) + (f"; and {len(errors) - 3} more" if len(errors) > 3 else "")
        raise ProcessSetupError(
            f"Process step: {len(errors)} work item type(s) / picklist(s) / field(s) the workbook "
            f"defines could not be created or enabled, so later steps would fail — {shown}. "
            "Check the PAT's scopes and permissions (see the run log), then run Process again.",
            stats=stats,
        )
    return stats


class StepSetupError(AdoError):
    """Work later steps depend on failed; the step fails after doing everything else."""

    def __init__(self, message: str, *, stats: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stats = stats or {}


class ProcessSetupError(StepSetupError):
    """Something the workbook defines couldn't be created; the Process step fails."""


def step_failure(step: str, failures: list[str], *, advice: str, stats: dict[str, Any]) -> StepSetupError:
    """One error listing the first few failures, raised once the step has done the rest."""
    shown = "; ".join(failures[:3]) + (f"; and {len(failures) - 3} more" if len(failures) > 3 else "")
    return StepSetupError(
        f"{step} step: {len(failures)} change(s) that later steps depend on failed — {shown}. {advice}",
        stats=stats,
    )


def _already_exists(exc: AdoError) -> bool:
    return exc.status == 409 or "already exist" in str(exc).lower()


def _find_picklist_ids(client: AdoClient, names: list[str]) -> dict[str, str]:
    """IDs of picklists by casefolded name, re-reading a few times: a list created moments ago can be missing."""
    wanted = {name.casefold() for name in names}
    found: dict[str, str] = {}
    for attempt in range(PICKLIST_LOOKUP_ATTEMPTS):
        if attempt:
            time.sleep(PICKLIST_LOOKUP_DELAY_S)
        data = client.get("/_apis/work/processes/lists", api_version="7.1") or {}
        for item in data.get("value") or []:
            key = str(item.get("name") or "").casefold()
            if key in wanted and item.get("id"):
                found[key] = item["id"]
        if len(found) == len(wanted):
            break
    return found


def _send_with_description_fallback(
    client: AdoClient,
    method: str,
    path: str,
    payload: dict[str, Any],
    *,
    api_version: str,
) -> tuple[Any, str]:
    """Send ``payload``; on a 400 retry with the description shortened, then empty.

    Azure DevOps doesn't document its description limits, so a rejected description
    shouldn't stop the item being created. Returns (response, description sent).
    """
    call = getattr(client, method.lower())
    description = (payload.get("description") or "").strip()
    try:
        return call(path, json_body=payload, api_version=api_version), description
    except AdoError as exc:
        if exc.status != 400 or not description or _already_exists(exc):
            raise
        first_error = exc
    fallbacks = [shorten_description(description), ""]
    for text in dict.fromkeys(item for item in fallbacks if item != description):
        try:
            response = call(path, json_body={**payload, "description": text}, api_version=api_version)
            return response, text
        except AdoError as exc:
            if exc.status != 400 or _already_exists(exc):
                raise
    raise first_error


def _ensure_process_local_wit(
    client: AdoClient,
    send: Callable[..., Any],
    process_id: str,
    existing: dict[str, Any],
    *,
    name: str,
    ref_name: str,
    color: str,
    icon: str,
    description: str,
    custom: bool,
    system_ref: bool,
    stats: dict[str, Any],
    emit: Emit,
) -> str | None:
    """Return a process-local WIT referenceName suitable for isDisabled PATCH."""
    existing_wit = resolve_process_wit(existing, name, ref_name)
    if existing_wit:
        customization = str(existing_wit.get("customization") or "").strip().lower()
        full_ref = str(existing_wit.get("referenceName") or "").strip()
        if full_ref and customization not in {"", "system"}:
            return full_ref

    if custom:
        create_ref = _short_reference_name(ref_name) or name.replace(" ", "")
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "color": color,
            "icon": icon,
            "referenceName": create_ref,
        }
    elif system_ref:
        payload = {
            "name": name,
            "description": description,
            "color": color,
            "icon": icon,
            "referenceName": _short_reference_name(ref_name),
            "inheritsFrom": ref_name,
        }
    else:
        return None

    try:
        created = send(
            "POST",
            f"/_apis/work/processes/{process_id}/workItemTypes",
            payload,
            api_version="7.1-preview.2",
            kind="work item type",
            name=name,
        )
        if created:
            _index_one_wit(existing, created)
            stats["wits_created"] += 1
            return str(created.get("referenceName") or payload["referenceName"])
    except AdoError as exc:
        if exc.status == 409:
            # Already process-local — refresh index and resolve again.
            refreshed = index_process_wits(client.list_process_wits(process_id))
            existing.clear()
            existing.update(refreshed)
            again = resolve_process_wit(existing, name, ref_name)
            if again:
                ref = str(again.get("referenceName") or "").strip()
                customization = str(again.get("customization") or "").strip().lower()
                if ref and customization not in {"", "system"}:
                    return ref
        else:
            emit("process", f"WIT '{name}' materialize for disable: {exc}")
    return None


def _build_ref(process_name: str, wit_name: str) -> str:
    safe_process = process_name.replace(" ", "")
    safe_name = wit_name.replace(" ", "").replace("-", "").replace("_", "")
    return f"{safe_process}.{safe_name}"


SYSTEM_WIT_PREFIX = "Microsoft.VSTS.WorkItemTypes."


def _is_system_reference(reference_name: str) -> bool:
    return str(reference_name or "").strip().startswith(SYSTEM_WIT_PREFIX)


def _short_reference_name(reference_name: str) -> str:
    text = str(reference_name or "").strip()
    return text.rsplit(".", 1)[-1] if "." in text else text


def is_process_local(wit: dict[str, Any]) -> bool:
    return str(wit.get("customization") or "").strip().lower() not in {"", "system"}


def _index_one_wit(index: dict[str, Any], wit: dict[str, Any]) -> None:
    name = str(wit.get("name") or "").strip()
    ref = str(wit.get("referenceName") or "").strip()
    keys = set()
    if name:
        keys.add(name.lower())
        keys.add(name.replace(" ", "").lower())
    if ref:
        keys.add(ref.lower())
        keys.add(ref.rsplit(".", 1)[-1].lower())
    for key in keys:
        current = index.get(key)
        # The process list can hold both the system entry and the process's own copy under one name.
        if current is not None and is_process_local(current) and not is_process_local(wit):
            continue
        index[key] = wit


def index_process_wits(wits: list[dict[str, Any]]) -> dict[str, Any]:
    """Process work item types by name / reference name; the process's own copy wins."""
    index: dict[str, Any] = {}
    for wit in wits:
        _index_one_wit(index, wit)
    return index



def resolve_process_wit(
    index: dict[str, Any], name: str, reference_name: str = ""
) -> dict[str, Any] | None:
    """The type a workbook row means, preferring the process-local entry over the system one."""
    return _process_local_wit(index, name, reference_name) or _resolve_existing_wit(
        index, name, reference_name
    )


def _process_local_wit(
    index: dict[str, Any], name: str, reference_name: str
) -> dict[str, Any] | None:
    """The process's own copy of a type; the inherited system entry can share its name."""
    wanted = str(reference_name or "").strip().lower()
    if wanted:
        for wit in index.values():
            if is_process_local(wit) and str(wit.get("inherits") or "").strip().lower() == wanted:
                return wit
    found = _resolve_existing_wit(index, name, reference_name)
    if found and is_process_local(found):
        return found
    return None


def _resolve_existing_wit(
    index: dict[str, Any], name: str, reference_name: str
) -> dict[str, Any] | None:
    name = str(name or "")
    candidates = (
        name,
        reference_name,
        _short_reference_name(reference_name),
        f"{SYSTEM_WIT_PREFIX}{name.replace(' ', '')}" if name else "",
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        for key in (text.lower(), text.replace(" ", "").lower()):
            if key in index:
                return index[key]
    return None
