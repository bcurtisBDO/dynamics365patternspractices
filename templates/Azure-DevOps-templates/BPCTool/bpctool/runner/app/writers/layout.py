"""Apply page layouts, including HtmlFieldControl groups for HTML fields."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from ..ado_client import AdoClient, AdoError
from .process import index_process_wits, resolve_process_wit, step_failure

Emit = Callable[[str, str], None]


def apply_layout(
    client: AdoClient,
    ir: dict[str, Any],
    *,
    dry_run: bool,
    emit: Emit,
    process_id: str | None = None,
) -> dict[str, Any]:
    process_name = ir["targets"]["process_name"]
    stats = {
        "wits_processed": 0,
        "html_controls": 0,
        "controls": 0,
        "fields_added": 0,
        "fields_updated": 0,
        "hidden_fields": 0,
        "controls_hidden": 0,
        "controls_shown": 0,
        "warnings": 0,
    }
    # Fields Catalog Import needs on a type; the step fails on these after laying out the rest.
    failures: list[str] = []

    def fail(message: str) -> None:
        failures.append(message)
        emit("layout", f"FAILED: {message}")

    # Form work left undone, by what stopped it: an unreadable form, or a page / group / HTML
    # control Azure DevOps didn't confirm. Those creates aren't sent again this run (a repeat can
    # add a duplicate); the fields they held fail the step.
    blockers: dict[str, str] = {}
    blocked: dict[str, list[str]] = {}

    html_count = ir["counts"].get("html_fields", 0)
    hidden_count = ir["counts"].get("hidden_field_placements", 0)
    if dry_run:
        emit(
            "layout",
            f"[dry-run] Would apply layouts for {len(ir['process']['work_item_types'])} WITs "
            f"including {html_count} HtmlFieldControl placements"
            + (
                f" and {hidden_count} hidden (H) fields added to WITs without a visible control"
                if hidden_count
                else ""
            ),
        )
        return stats

    process_id = process_id or client.get_process_id(process_name)
    if not process_id:
        raise AdoError(f'Process "{process_name}" not found for layout apply.')

    fields_by_label = {
        (f.get("label") or f.get("field_name") or "").strip(): f for f in ir["process"]["fields"]
    }
    process_wit_map = index_process_wits(client.list_process_wits(process_id))

    for wit in ir["process"]["work_item_types"]:
        placements = [(label, False) for label in wit.get("applicable_field_labels") or []]
        placements += [(label, True) for label in wit.get("hidden_field_labels") or []]
        if not placements:
            continue
        if wit.get("disabled"):
            continue
        wit_name = wit["name"]
        excel_ref = (wit.get("reference_name") or "").strip()
        process_wit = resolve_process_wit(process_wit_map, wit_name, excel_ref)
        if not process_wit:
            emit("layout", f"Skipping layout for missing WIT '{wit_name}'")
            stats["warnings"] += 1
            continue

        wit_ref = str(process_wit.get("referenceName") or excel_ref or "")
        wit_api_id = _process_api_wit_id(wit_name, wit_ref, process_wit)
        if not wit_api_id:
            emit("layout", f"Skipping layout for WIT '{wit_name}' — missing Process API id")
            stats["warnings"] += 1
            continue

        # Materialize system WITs before field/layout mutation (reference ensure_inherited_wit_customized).
        if _is_system_reference(wit_ref):
            error = _materialize_system_wit(client, process_id, wit_ref, process_wit)
            if error:
                fail(
                    f"WIT '{wit_name}' couldn't be customized in this process, so its fields and "
                    f"form weren't changed: {error}"
                )
                continue
            process_wit_map = index_process_wits(client.list_process_wits(process_id))
            refreshed = resolve_process_wit(process_wit_map, wit_name, excel_ref) or process_wit
            wit_ref = str(refreshed.get("referenceName") or wit_ref)
            wit_api_id = _process_api_wit_id(wit_name, wit_ref, refreshed)

        stats["wits_processed"] += 1
        enc_wit = quote(wit_api_id, safe="")
        layout_path = f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}/layout"
        wit_fields = _wit_field_refs(client, process_id, enc_wit)
        # Read once per type and kept up to date with what this run adds.
        layout: dict[str, Any] | None = None
        loaded = False

        for label, hidden in placements:
            field = fields_by_label.get(label)
            if not field:
                continue
            ref = field.get("reference_name")
            if not ref:
                continue
            field_type = (field.get("field_type") or "").strip()
            order = (
                int(field.get("field_sequence"))
                if str(field.get("field_sequence") or "").isdigit()
                else 1
            )
            group_order = (
                int(field.get("group_sequence"))
                if str(field.get("group_sequence") or "").isdigit()
                else 1
            )

            # Add field to WIT (booleans require defaultValue — VS403099)
            field_ok = _ensure_field_on_wit(
                client, process_id, enc_wit, field, emit=emit, wit_name=wit_name, present=wit_fields
            )
            if field_ok is True:
                stats["fields_added"] += 1
            elif field_ok is False:
                fail(f"field {ref} couldn't be added to '{wit_name}'")
                continue
            else:
                updated = _sync_wit_field(
                    client,
                    process_id,
                    enc_wit,
                    field,
                    (wit_fields or {}).get(ref.lower()),
                    emit=emit,
                    wit_name=wit_name,
                )
                if updated is True:
                    stats["fields_updated"] += 1
                elif updated is False:
                    stats["warnings"] += 1

            page_name = field.get("page_name") or "Details"
            group_name = field.get("group_name") or label
            section_label = field.get("group_location") or "Section1"

            form_key = _form_key(enc_wit, "form")
            if not loaded and form_key not in blockers:
                try:
                    layout = client.get(layout_path, api_version="7.1-preview.1")
                    loaded = True
                except AdoError as exc:
                    emit(
                        "layout",
                        f"Could not read the form of {wit_name}: {exc}. Its fields are still added "
                        "to the type, but its form isn't changed this run.",
                    )
                    blockers[form_key] = f"The form of '{wit_name}' couldn't be read ({exc})"
            if _block(blockers, blocked, form_key, ref):
                continue

            multivalue = _control_type(field) == "MultivalueContribution"
            contribution, native = _find_field_controls(layout, ref)
            current = contribution if multivalue else (native or contribution)
            if hidden:
                stats["hidden_fields"] += 1
                if _parse_required(field.get("required")) and not _has_default(field):
                    emit(
                        "layout",
                        f"Hidden field {ref} on {wit_name} is required with no default value; "
                        "users cannot fill it on the form",
                    )
                    stats["warnings"] += 1
                for group_id, control in (found for found in (contribution, native) if found):
                    if not control.get("visible", True):
                        continue
                    if _set_control_visibility(
                        client, process_id, enc_wit, group_id, str(control.get("id") or ref),
                        visible=False, emit=emit,
                    ):
                        control["visible"] = False
                        stats["controls_hidden"] += 1
                    else:
                        stats["warnings"] += 1
                continue

            if current is not None:
                group_id, control = current
                if control.get("visible", True) is False:
                    if _set_control_visibility(
                        client, process_id, enc_wit, group_id, str(control.get("id") or ref),
                        visible=True, emit=emit,
                    ):
                        control["visible"] = True
                        stats["controls_shown"] += 1
                    else:
                        stats["warnings"] += 1
                if multivalue and native is not None and native[1].get("visible", True):
                    if not _retire_native_control(
                        client, process_id, enc_wit, layout, native, emit=emit, wit_name=wit_name
                    ):
                        stats["warnings"] += 1
                continue

            page_id, layout = _ensure_page(
                client, process_id, enc_wit, layout, page_name, group_order, emit, wit_name,
                blockers=blockers,
            )
            if not page_id or not layout:
                if not _block(blockers, blocked, _form_key(enc_wit, "page", page_name), ref):
                    stats["warnings"] += 1
                continue

            section_id = _section_id(layout, page_id, section_label)
            if not section_id:
                emit("layout", f"No section '{section_label}' on {wit_name}/{page_name}")
                stats["warnings"] += 1
                continue

            if field.get("is_html") or field_type.lower() == "html":
                ok = _add_html_control(
                    client,
                    process_id,
                    enc_wit,
                    page_id,
                    section_id,
                    ref,
                    label,
                    group_name,
                    group_order,
                    order,
                    layout,
                    emit=emit,
                    wit_name=wit_name,
                    blockers=blockers,
                )
                if ok:
                    stats["html_controls"] += 1
                elif not _block(blockers, blocked, _form_key(enc_wit, "html", ref), ref):
                    stats["warnings"] += 1
                continue

            group_id = _ensure_group(
                client,
                process_id,
                enc_wit,
                page_id,
                section_id,
                layout,
                group_name,
                group_order,
                emit=emit,
                wit_name=wit_name,
                blockers=blockers,
            )
            if not group_id:
                if not _block(blockers, blocked, _form_key(enc_wit, "group", page_id, group_name), ref):
                    stats["warnings"] += 1
                continue

            status, control = _add_control_to_group(
                client,
                process_id,
                enc_wit,
                group_id,
                field,
                label,
                order,
                emit=emit,
                fallback=native is None,
            )
            if status == "failed":
                stats["warnings"] += 1
                if multivalue and native is not None:
                    emit(
                        "layout",
                        f"WARNING: Multi-select field {ref} on {wit_name} keeps its standard picklist "
                        "control (one value only): the DevLabs multivalue control couldn't be added. "
                        "Check the Multivalue control extension is installed and enabled, then run Layout again.",
                    )
                continue
            stats["controls"] += 1
            if control is not None and not _remember_control(layout, group_id, control):
                loaded = False
            if status == "native":
                stats["warnings"] += 1
                emit(
                    "layout",
                    f"WARNING: Multi-select field {ref} on {wit_name} got a standard picklist control "
                    "(one value only) because the DevLabs multivalue control couldn't be added. "
                    "Check the Multivalue control extension is installed and enabled, then run Layout again.",
                )
            elif multivalue and native is not None:
                if not _retire_native_control(
                    client, process_id, enc_wit, layout, native, emit=emit, wit_name=wit_name
                ):
                    stats["warnings"] += 1

    for key, refs in blocked.items():
        shown = ", ".join(refs[:5]) + (f" and {len(refs) - 5} more" if len(refs) > 5 else "")
        fail(f"{blockers[key]}, so the form wasn't updated for {shown}")

    emit(
        "layout",
        f"Layout apply finished: {stats['wits_processed']} WITs, "
        f"{stats['html_controls']} HTML controls, {stats['controls']} other controls"
        + (
            f", {stats['hidden_fields']} hidden (H) fields "
            f"({stats['controls_hidden']} existing controls hidden)"
            if stats["hidden_fields"]
            else ""
        )
        + (f", {stats['controls_shown']} controls un-hidden" if stats["controls_shown"] else "")
        + (f", {stats['fields_updated']} fields' Required / Default value updated" if stats["fields_updated"] else ""),
    )
    if failures:
        raise step_failure(
            "Layout",
            failures,
            advice=(
                "Catalog Import can't fill in fields missing from a work item type. Check the PAT's "
                "permissions and the run log, then run Layout again; it only adds what's missing."
            ),
            stats=stats,
        )
    return stats


def _is_multivalue_control(control: dict[str, Any], field_ref: str) -> bool:
    if not control.get("isContribution"):
        return False
    target = field_ref.lower()
    control_id = str(control.get("id") or "").lower()
    inputs = (control.get("contribution") or {}).get("inputs") or {}
    return control_id in {target, f"{target}.multivalue"} or str(inputs.get("FieldName") or "").lower() == target


def _find_field_controls(
    layout: dict[str, Any] | None, field_ref: str
) -> tuple[tuple[str | None, dict[str, Any]] | None, tuple[str | None, dict[str, Any]] | None]:
    """(group id, control) of the field's DevLabs multivalue control and of its standard control.

    Earlier BPCTool runs gave the multivalue control the field's own id; Microsoft's scripts
    use ``<field>.multivalue``. Either is recognised so re-runs don't add a second one.
    """
    target = field_ref.lower()
    contribution = native = None
    for page in (layout or {}).get("pages") or []:
        for section in page.get("sections") or []:
            for group in section.get("groups") or []:
                for control in group.get("controls") or []:
                    if _is_multivalue_control(control, field_ref):
                        contribution = contribution or (group.get("id"), control)
                    elif str(control.get("id") or "").lower() == target:
                        native = native or (group.get("id"), control)
    return contribution, native


def _set_control_visibility(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    group_id: str | None,
    control_id: str,
    *,
    visible: bool,
    emit: Emit,
) -> bool:
    if not group_id:
        return False
    try:
        client.patch(
            f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}"
            f"/layout/groups/{quote(group_id, safe='')}/controls/{quote(control_id, safe='')}",
            json_body={"id": control_id, "visible": visible},
            api_version="7.1-preview.1",
        )
        return True
    except AdoError as exc:
        emit(
            "layout",
            f"{'Show' if visible else 'Hide'} control {control_id}: {exc}",
        )
        return False


def _retire_native_control(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    layout: dict[str, Any] | None,
    native: tuple[str | None, dict[str, Any]],
    *,
    emit: Emit,
    wit_name: str,
) -> bool:
    """Remove a multi-select field's standard control once its multivalue control is on the form.

    A control the base type defines can't be removed, so it is hidden instead.
    """
    group_id, control = native
    control_id = str(control.get("id") or "")
    if not group_id or not control_id:
        return False
    try:
        client.delete(
            f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}"
            f"/layout/groups/{quote(group_id, safe='')}/controls/{quote(control_id, safe='')}",
            api_version="7.1-preview.1",
        )
    except AdoError as exc:
        if exc.status != 404:
            if _set_control_visibility(
                client, process_id, enc_wit, group_id, control_id, visible=False, emit=emit
            ):
                control["visible"] = False
                emit("layout", f"Hid the standard control for {control_id} on {wit_name}; the multivalue control replaces it")
                return True
            emit(
                "layout",
                f"WARNING: {wit_name} shows both the multivalue and the standard control for {control_id}; "
                f"remove the standard one in the form editor ({exc})",
            )
            return False
    _forget_control(layout, group_id, control)
    emit("layout", f"Removed the standard control for {control_id} on {wit_name}; the multivalue control replaces it")
    return True


def _find_group_node(layout: dict[str, Any] | None, group_id: str) -> dict[str, Any] | None:
    for page in (layout or {}).get("pages") or []:
        for section in page.get("sections") or []:
            for group in section.get("groups") or []:
                if group.get("id") == group_id:
                    return group
    return None


def _remember_control(layout: dict[str, Any] | None, group_id: str, control: dict[str, Any]) -> bool:
    group = _find_group_node(layout, group_id)
    if group is None:
        return False
    group.setdefault("controls", []).append(dict(control))
    return True


def _forget_control(layout: dict[str, Any] | None, group_id: str, control: dict[str, Any]) -> None:
    group = _find_group_node(layout, group_id)
    if group is not None:
        group["controls"] = [item for item in group.get("controls") or [] if item is not control]


def _remember_group(
    layout: dict[str, Any] | None, page_id: str, section_id: str, group: dict[str, Any]
) -> bool:
    for page in (layout or {}).get("pages") or []:
        if page.get("id") != page_id:
            continue
        for section in page.get("sections") or []:
            if section.get("id") == section_id:
                section.setdefault("groups", []).append(group)
                return True
    return False


def _form_key(enc_wit: str, *parts: str) -> str:
    return "|".join([enc_wit, *(str(part).strip().lower() for part in parts)])


def _block(blockers: dict[str, str], blocked: dict[str, list[str]], key: str, ref: str) -> bool:
    """Note ``ref`` as left off the form when ``key`` is blocked; False if it isn't."""
    if key not in blockers:
        return False
    blocked.setdefault(key, []).append(ref)
    return True


def _reload_layout(client: AdoClient, layout_path: str, layout: dict[str, Any]) -> bool:
    """Re-read the form into ``layout`` in place; False if it can't be read."""
    try:
        fresh = client.get(layout_path, api_version="7.1-preview.1") or {}
    except AdoError:
        return False
    if fresh is not layout:
        layout.clear()
        layout.update(fresh)
    return True


def _has_default(field: dict[str, Any]) -> bool:
    if _is_boolean_field(field):
        return True
    value = str(field.get("default_value") or "").strip()
    return bool(value) and value.lower() != "none"


MULTIVALUE_CONTRIBUTION_ID = (
    "ms-devlabs.vsts-extensions-multivalue-control.multivalue-form-control"
)


def _control_type(field: dict[str, Any] | str, reference_name: str = "") -> str:
    """Reference get_control_type — default is ``Field``, not FieldControl."""
    if isinstance(field, dict):
        field_type = field.get("field_type") or ""
        reference_name = field.get("reference_name") or reference_name
        is_picklist = (field_type or "").lower().startswith("picklist")
        if field.get("multi_select") and is_picklist:
            return "MultivalueContribution"
    else:
        field_type = field
        is_picklist = (field_type or "").lower().startswith("picklist")

    ft = (field_type or "").strip().lower()
    ref = (reference_name or "").strip().lower()

    if ref in {
        "system.areaid",
        "system.area",
        "system.areapath",
        "system.iterationid",
        "system.iteration",
        "system.iterationpath",
    }:
        return "WorkItemClassificationControl"
    if ref in {
        "system.assignedto",
        "system.createdby",
        "system.changedby",
        "system.authorizedas",
        "system.owner",
        "system.requestedby",
    } or ft == "identity":
        return "IdentityControl"
    if ref in {
        "system.createddate",
        "system.changeddate",
        "system.resolveddate",
        "system.closeddate",
    } or ft in {"datetime", "date time"}:
        return "DateTimeControl"
    if ft in {"boolean", "bool"}:
        return "BooleanControl"
    if ft == "html":
        return "HtmlFieldControl"
    if is_picklist:
        if ft in {"integer", "picklistinteger"}:
            return "PickListIntegerControl"
        return "PickListStringControl"
    return "Field"


def _ensure_page(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    layout: dict[str, Any] | None,
    page_name: str,
    order: int,
    emit: Emit,
    wit_name: str,
    blockers: dict[str, str] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    page = _find_page(layout, page_name)
    if page and page.get("id"):
        return page.get("id"), layout
    key = _form_key(enc_wit, "page", page_name)
    if blockers is not None and key in blockers:
        return None, layout
    layout_path = f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}/layout"
    error: AdoError | None = None
    created = None
    try:
        created = client.post(
            f"{layout_path}/pages",
            json_body={
                "label": page_name,
                "order": order,
                "visible": True,
                "inherited": True,
            },
            api_version="7.1-preview.1",
            idempotent=False,
        )
    except AdoError as exc:
        if exc.status not in {400, 409} and not exc.outcome_unknown:
            emit("layout", f"Could not create page '{page_name}' on {wit_name}: {exc}")
            return None, layout
        # The page may already exist (e.g. an earlier request whose response was lost).
        error = exc
    page_id = created.get("id") if created else None
    try:
        layout = client.get(layout_path, api_version="7.1-preview.1")
    except AdoError as exc:
        if page_id:
            if layout is not None:
                layout.setdefault("pages", []).append({"label": page_name, **(created or {})})
            return page_id, layout
        emit("layout", f"Could not re-read the layout of {wit_name} after adding page '{page_name}': {exc}")
    else:
        if not page_id:
            page = _find_page(layout, page_name)
            page_id = page.get("id") if page else None
        if page_id:
            return page_id, layout
    unconfirmed = error is None or error.outcome_unknown
    reason = error or "Azure DevOps returned no page id"
    rerun = ". Run Layout again; it only adds what's missing." if unconfirmed else ""
    emit("layout", f"Could not create page '{page_name}' on {wit_name}: {reason}{rerun}")
    if unconfirmed and blockers is not None:
        blockers[key] = f"Azure DevOps didn't confirm page '{page_name}' was added to '{wit_name}' ({reason})"
    return None, layout


def _add_control_to_group(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    group_id: str,
    field: dict[str, Any],
    label: str,
    order: int,
    *,
    emit: Emit,
    fallback: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    """Add the field's control; returns ("added" | "native" | "failed", the control sent).

    "native": the DevLabs multivalue control was refused and a standard picklist control was
    added instead. ``fallback=False`` skips that (the field already has a standard control).
    """
    ref = field.get("reference_name")
    if not ref or not group_id:
        return "failed", None
    control_type = _control_type(field)
    bodies: list[dict[str, Any]] = []
    if control_type == "MultivalueContribution":
        # DevLabs multivalue control (reference Script 2 add_multivalue_control_if_missing).
        bodies.append(
            {
                "id": f"{ref}.multivalue",
                "label": label,
                "visible": True,
                "inherited": False,
                "order": order,
                "isContribution": True,
                "contribution": {
                    "contributionId": MULTIVALUE_CONTRIBUTION_ID,
                    "inputs": {"FieldName": ref},
                    "height": 50,
                },
                "height": 50,
            }
        )
        if fallback:
            bodies.append(
                {
                    "id": ref,
                    "label": label,
                    "controlType": "PickListStringControl",
                    "visible": True,
                    "inherited": True,
                    "order": order,
                }
            )
    else:
        type_candidates = [control_type]
        if control_type == "Field":
            type_candidates.append("FieldControl")
        elif control_type == "FieldControl":
            type_candidates.append("Field")
        for candidate in type_candidates:
            bodies.append(
                {
                    "id": ref,
                    "label": label,
                    "controlType": candidate,
                    "visible": True,
                    "inherited": True,
                    "order": order,
                }
            )

    last_error: AdoError | None = None
    for index, body in enumerate(bodies):
        try:
            client.post(
                f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}"
                f"/layout/groups/{quote(group_id, safe='')}/controls",
                json_body=body,
                api_version="7.1-preview.1",
            )
        except AdoError as exc:
            if exc.status != 409:
                last_error = exc
                continue
        native = control_type == "MultivalueContribution" and index > 0
        if native and last_error:
            emit("layout", f"Multivalue control {ref}: {last_error}")
        return ("native" if native else "added"), body
    if last_error:
        emit("layout", f"Control {ref} ({control_type}): {last_error}")
    return "failed", None


def _is_boolean_field(field: dict[str, Any]) -> bool:
    return (field.get("field_type") or "").strip().lower() in {"boolean", "bool"}


def _parse_required(value: Any) -> bool:
    return str(value or "").strip().lower() in {"yes", "true", "1", "y"}


def _wit_field_payload(field: dict[str, Any]) -> dict[str, Any]:
    ref = field["reference_name"]
    required = _parse_required(field.get("required"))
    if _is_boolean_field(field):
        # VS402851: Boolean WIT fields must default to false. ADO Process API
        # accepts the string "false" more reliably than JSON false.
        return {
            "referenceName": ref,
            "defaultValue": "false",
            "required": required,
            "requiredValue": "false",
            "visible": True,
        }
    payload: dict[str, Any] = {
        "referenceName": ref,
        "required": required,
        "visible": True,
    }
    field_type = (field.get("field_type") or "").strip().lower()
    if field_type == "identity":
        payload["allowGroups"] = True
    default_value = field.get("default_value")
    if default_value is not None and str(default_value).strip() and str(default_value).strip().lower() != "none":
        payload["defaultValue"] = default_value
    return payload


def _boolean_field_variants(ref: str) -> list[dict[str, Any]]:
    """Reference-script compatible retry payloads (VS403099 / VS402851)."""
    variants: list[dict[str, Any]] = [
        {"referenceName": ref, "defaultValue": "false", "visible": True},
        {"referenceName": ref, "defaultValue": False, "visible": True},
    ]
    for required in (False, True):
        variants.extend(
            [
                {
                    "referenceName": ref,
                    "defaultValue": "false",
                    "required": required,
                    "visible": True,
                },
                {
                    "referenceName": ref,
                    "defaultValue": "false",
                    "required": required,
                    "requiredValue": "false",
                    "visible": True,
                },
                {
                    "referenceName": ref,
                    "defaultValue": False,
                    "required": required,
                    "visible": True,
                },
                {
                    "referenceName": ref,
                    "defaultValue": False,
                    "required": required,
                    "requiredValue": False,
                    "visible": True,
                },
            ]
        )
    return variants


def _wit_field_refs(
    client: AdoClient, process_id: str, enc_wit: str
) -> dict[str, dict[str, Any]] | None:
    """Fields already on the WIT (incl. inherited) by lowercase reference name, or None if unreadable."""
    try:
        data = client.get(
            f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}/fields",
            api_version="7.1-preview.2",
        ) or {}
    except AdoError:
        return None
    return {
        str(item.get("referenceName") or "").lower(): item
        for item in data.get("value") or []
        if item.get("referenceName")
    }


def _ensure_field_on_wit(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    field: dict[str, Any],
    *,
    emit: Emit,
    wit_name: str,
    present: dict[str, dict[str, Any]] | None = None,
) -> bool | None:
    """Add field to WIT. Returns True if newly added, None if already present, False on failure.

    ``present`` (from :func:`_wit_field_refs`) skips the POST for fields already on the type.
    A 400 is a real rejection (e.g. VS403099 for a boolean default), so the next payload
    variant is tried; only 409 or an "already" message means the field is there.
    """
    ref = field.get("reference_name")
    if not ref:
        return False
    if present is not None and ref.lower() in present:
        return None
    # Prefer the reference scripts' camelCase segment; ADO accepts both, but match the script path.
    path = f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}/fields"
    payloads = [_wit_field_payload(field)]
    if _is_boolean_field(field):
        payloads.extend(_boolean_field_variants(ref))

    last_error: AdoError | None = None
    for payload in payloads:
        try:
            client.post(path, json_body=payload, api_version="7.1-preview.2")
            if present is not None:
                # Empty: added by this run, so there is nothing to compare on a later placement.
                present[ref.lower()] = {}
            return True
        except AdoError as exc:
            if exc.status == 409 or "already" in str(exc).lower():
                return None
            last_error = exc
            if exc.status != 400:
                break

    if last_error:
        kind = "boolean field" if _is_boolean_field(field) else "field"
        emit("layout", f"Add {kind} {ref} on {wit_name}: {last_error}")
    return False


def _default_text(value: Any, *, boolean: bool) -> str:
    text = "" if value is None else str(value).strip()
    return text.lower() if boolean else text


def _sync_wit_field(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    field: dict[str, Any],
    current: dict[str, Any] | None,
    *,
    emit: Emit,
    wit_name: str,
) -> bool | None:
    """Apply workbook Required / Default value changes to a field already on the WIT.

    True if updated, None if nothing changed, False on failure. Only fields the process
    added are changed; the base type's own fields (e.g. Title) keep Azure DevOps' rules.
    A default value removed from the workbook is left in place.
    """
    ref = field.get("reference_name")
    if not ref or not current:
        return None
    if not (field.get("custom_field") or str(current.get("customization") or "").strip().lower() == "custom"):
        return None
    wanted = _wit_field_payload(field)
    boolean = _is_boolean_field(field)
    default = wanted.get("defaultValue")
    required_changed = bool(current.get("required")) != bool(wanted["required"])
    default_changed = default is not None and _default_text(default, boolean=boolean) != _default_text(
        current.get("defaultValue"), boolean=boolean
    )
    if not (required_changed or default_changed):
        return None
    update: dict[str, Any] = {"required": bool(wanted["required"])}
    if default is not None:
        update["defaultValue"] = default
    try:
        client.patch(
            f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}/fields/{quote(ref, safe='')}",
            json_body=update,
            api_version="7.1-preview.2",
        )
    except AdoError as exc:
        emit("layout", f"WARNING: Could not update Required / Default value of {ref} on {wit_name}: {exc}")
        return False
    current.update(update)
    emit(
        "layout",
        f"Updated {ref} on {wit_name}: required {'yes' if update['required'] else 'no'}"
        + (f", default value '{default}'" if default is not None else ""),
    )
    return True


SYSTEM_WIT_PREFIX = "Microsoft.VSTS.WorkItemTypes."


def _is_system_reference(reference_name: str) -> bool:
    return str(reference_name or "").strip().startswith(SYSTEM_WIT_PREFIX)


def _process_api_wit_id(wit_name: str, wit_ref: str, process_wit: dict[str, Any]) -> str:
    """System Microsoft.VSTS.* refs often need the display name on Process APIs."""
    resolved_ref = str(process_wit.get("referenceName") or wit_ref or "").strip()
    resolved_name = str(process_wit.get("name") or wit_name or "").strip()
    if resolved_ref.startswith(SYSTEM_WIT_PREFIX):
        return resolved_name or wit_name or resolved_ref
    return resolved_ref or resolved_name or wit_name


def _materialize_system_wit(
    client: AdoClient,
    process_id: str,
    wit_ref: str,
    wit_info: dict[str, Any],
) -> AdoError | None:
    """Customize a system WIT in the process; returns Azure DevOps' error if it refused.

    Like the reference script, only 2xx or 409 (already customized) count as done.
    """
    customization = str(wit_info.get("customization") or "").strip().lower()
    if customization in {"inherited", "custom"}:
        return None
    if not _is_system_reference(wit_ref):
        return None
    payload = {
        "description": wit_info.get("description"),
        "color": wit_info.get("color"),
        "icon": wit_info.get("icon"),
        "isDisabled": wit_info.get("isDisabled", False),
    }
    display = str(wit_info.get("name") or "").strip()
    # The display-name path is the reference fallback for tenants that reject the reference name.
    api_ids = [wit_ref] + ([display] if display and display != wit_ref else [])
    error: AdoError | None = None
    for api_id in api_ids:
        try:
            client.patch(
                f"/_apis/work/processes/{process_id}/workItemTypes/{quote(api_id, safe='')}",
                json_body=payload,
                api_version="7.1-preview.2",
            )
        except AdoError as exc:
            if exc.status != 409:
                error = error or exc
                continue
        wit_info["customization"] = "inherited"
        return None
    return error


def _find_page(layout: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    if not layout:
        return None
    target = label.strip().lower()
    for page in layout.get("pages") or []:
        if str(page.get("label") or "").strip().lower() == target:
            return page
    return None


def _section_id(layout: dict[str, Any] | None, page_id: str, section_label: str) -> str | None:
    page = None
    for candidate in (layout or {}).get("pages") or []:
        if candidate.get("id") == page_id:
            page = candidate
            break
    if not page:
        return None
    sections = page.get("sections") or []
    if not sections:
        return None
    target = section_label.strip().lower().replace(" ", "")
    for section in sections:
        sid = str(section.get("id") or "").lower().replace(" ", "")
        slabel = str(section.get("label") or "").lower().replace(" ", "")
        if sid == target or slabel == target:
            return section.get("id")
    # Do not fall back to sections[0] — that silently misplaces controls.
    return None


def _control_exists_on_page(
    layout: dict[str, Any] | None, page_id: str | None, field_ref: str
) -> bool:
    if not layout:
        return False
    for page in layout.get("pages") or []:
        if page_id and page.get("id") != page_id:
            continue
        for section in page.get("sections") or []:
            for group in section.get("groups") or []:
                for control in group.get("controls") or []:
                    if control.get("id") == field_ref:
                        return True
    return False


def _find_group(layout: dict[str, Any] | None, page_id: str, group_label: str) -> str | None:
    target = group_label.strip().lower()
    for page in (layout or {}).get("pages") or []:
        if page.get("id") != page_id:
            continue
        for section in page.get("sections") or []:
            for group in section.get("groups") or []:
                if str(group.get("label") or "").strip().lower() == target:
                    return group.get("id")
    return None


def _ensure_group(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    page_id: str,
    section_id: str,
    layout: dict[str, Any] | None,
    group_label: str,
    group_order: int = 1,
    *,
    emit: Emit,
    wit_name: str,
    blockers: dict[str, str] | None = None,
) -> str | None:
    found = _find_group(layout, page_id, group_label)
    if found:
        return found
    key = _form_key(enc_wit, "group", page_id, group_label)
    if blockers is not None and key in blockers:
        return None
    layout_path = f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}/layout"
    error: AdoError | None = None
    created = None
    try:
        # Not retried: Azure DevOps accepts a second group with the same label.
        created = client.post(
            f"{layout_path}/pages/{page_id}/sections/{section_id}/groups",
            json_body={
                "label": group_label,
                "order": group_order,
                "visible": True,
                "inherited": True,
                "controls": [],
            },
            api_version="7.1-preview.1",
            idempotent=False,
        )
    except AdoError as exc:
        if not exc.outcome_unknown:
            emit("layout", f"Could not create group '{group_label}' on {wit_name}: {exc}")
            return None
        error = exc
    group_id = created.get("id") if created else None
    if group_id:
        remembered = {"label": group_label, **(created or {})}
        remembered["controls"] = list(remembered.get("controls") or [])
        if not _remember_group(layout, page_id, section_id, remembered):
            if layout is not None:
                _reload_layout(client, layout_path, layout)
        return group_id
    if layout is not None and _reload_layout(client, layout_path, layout):
        found = _find_group(layout, page_id, group_label)
        if found:
            return found
    reason = error or "Azure DevOps returned no group id"
    emit(
        "layout",
        f"Could not create group '{group_label}' on {wit_name}: {reason}. "
        "Run Layout again; it only adds what's missing.",
    )
    if blockers is not None:
        blockers[key] = f"Azure DevOps didn't confirm group '{group_label}' was added to '{wit_name}' ({reason})"
    return None


def _add_html_control(
    client: AdoClient,
    process_id: str,
    enc_wit: str,
    page_id: str,
    section_id: str,
    ref: str,
    label: str,
    group_name: str,
    group_order: int,
    field_order: int,
    layout: dict[str, Any] | None,
    *,
    emit: Emit,
    wit_name: str,
    blockers: dict[str, str] | None = None,
) -> bool:
    """HTML controls must be created inside a new group payload (reference layout behavior)."""
    if _control_exists_on_page(layout, page_id, ref):
        return True
    layout_path = f"/_apis/work/processes/{process_id}/workItemTypes/{enc_wit}/layout"
    group = {
        "label": group_name or label,
        "order": group_order,
        "visible": True,
        "inherited": True,
        "controls": [
            {
                "id": ref,
                "label": label,
                "order": field_order,
                "controlType": "HtmlFieldControl",
                "visible": True,
                "readOnly": False,
                "metadata": "",
                "watermark": "",
                "height": 250,
            }
        ],
    }
    try:
        # Not retried: a repeat would add a second group holding the same field.
        created = client.post(
            f"{layout_path}/pages/{page_id}/sections/{section_id}/groups",
            json_body=group,
            api_version="7.1-preview.1",
            idempotent=False,
        )
    except AdoError as exc:
        if exc.status == 409:
            return True
        if exc.outcome_unknown and layout is not None and _reload_layout(client, layout_path, layout):
            if _control_exists_on_page(layout, page_id, ref):
                return True
        rerun = ". Run Layout again; it only adds what's missing." if exc.outcome_unknown else ""
        emit("layout", f"HTML control {ref} on {wit_name}: {exc}{rerun}")
        if exc.outcome_unknown and blockers is not None:
            blockers[_form_key(enc_wit, "html", ref)] = (
                f"Azure DevOps didn't confirm the HTML control for {ref} was added to '{wit_name}' ({exc})"
            )
        return False
    remembered = {**group, **(created or {})}
    remembered["controls"] = list(remembered.get("controls") or group["controls"])
    if not remembered.get("id") or not _remember_group(layout, page_id, section_id, remembered):
        if layout is not None:
            _reload_layout(client, layout_path, layout)
    return True
