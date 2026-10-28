"""Layout: multivalue controls, process-local WITs, failures, field updates, and non-retried creates."""

from __future__ import annotations

from typing import Any

import pytest

from app.ado_client import AdoError
from app.writers.layout import MULTIVALUE_CONTRIBUTION_ID, apply_layout
from app.writers.process import StepSetupError

WIT_REF = "BPC.Workshop"
MULTI = "MSBPC.applicationfamily"


class LayoutClient:
    def __init__(
        self,
        *,
        controls: list[dict[str, Any]] | None = None,
        wit_fields: list[dict[str, Any]] | None = None,
        wits: list[dict[str, Any]] | None = None,
        fail: dict[str, AdoError] | None = None,
    ) -> None:
        self.layout = {
            "pages": [
                {
                    "id": "page-1",
                    "label": "Details",
                    "sections": [
                        {
                            "id": "Section1",
                            "groups": [{"id": "group-1", "label": "Existing", "controls": list(controls or [])}],
                        }
                    ],
                }
            ]
        }
        self.wit_fields = wit_fields or []
        self.wits = wits or [{"name": "Workshop", "referenceName": WIT_REF, "customization": "custom"}]
        self.fail = fail or {}
        self.calls: list[tuple[str, str, Any, dict[str, Any]]] = []
        self.layout_gets = 0

    def _maybe_fail(self, method: str, path: str) -> None:
        for key, exc in self.fail.items():
            verb, _, suffix = key.partition(" ")
            if verb == method and path.endswith(suffix):
                raise exc

    def list_process_wits(self, _process_id: str) -> list[dict[str, Any]]:
        return list(self.wits)

    def get(self, path: str, **kwargs: Any) -> Any:
        self.calls.append(("GET", path, None, kwargs))
        if path.endswith("/fields"):
            return {"value": self.wit_fields}
        self.layout_gets += 1
        return self.layout

    def post(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        self.calls.append(("POST", path, json_body, kwargs))
        self._maybe_fail("POST", path)
        if path.endswith("/groups"):
            return {"id": f"group-{json_body['label']}"}
        return {}

    def patch(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        self.calls.append(("PATCH", path, json_body, kwargs))
        self._maybe_fail("PATCH", path)
        return {}

    def delete(self, path: str, **kwargs: Any) -> Any:
        self.calls.append(("DELETE", path, None, kwargs))
        self._maybe_fail("DELETE", path)
        return None

    def sent(self, method: str, suffix: str) -> list[tuple[str, Any, dict[str, Any]]]:
        return [(p, body, kw) for m, p, body, kw in self.calls if m == method and p.endswith(suffix)]


def _ir(fields: list[dict[str, Any]], *, wit_name: str = "Workshop", wit_ref: str = WIT_REF) -> dict[str, Any]:
    return {
        "targets": {"process_name": "Demo"},
        "counts": {"html_fields": 0, "hidden_field_placements": 0},
        "process": {
            "fields": fields,
            "work_item_types": [
                {
                    "name": wit_name,
                    "reference_name": wit_ref,
                    "applicable_field_labels": [f["label"] for f in fields],
                    "hidden_field_labels": [],
                }
            ],
        },
    }


def _multi_field() -> dict[str, Any]:
    return {
        "label": "Application family",
        "reference_name": MULTI,
        "field_type": "PicklistString",
        "multi_select": True,
        "group_name": "Existing",
        "custom_field": True,
    }


def _run(client: LayoutClient, ir: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    events: list[str] = []
    stats = apply_layout(
        client,  # type: ignore[arg-type]
        ir,
        dry_run=False,
        emit=lambda _step, message: events.append(message),
        process_id="proc-1",
    )
    return stats, events


def test_multivalue_control_uses_the_reference_id() -> None:
    client = LayoutClient()
    stats, _ = _run(client, _ir([_multi_field()]))

    [(_, body, _)] = client.sent("POST", "/controls")
    assert body["id"] == f"{MULTI}.multivalue"
    assert body["isContribution"] is True
    assert body["contribution"]["contributionId"] == MULTIVALUE_CONTRIBUTION_ID
    assert body["contribution"]["inputs"] == {"FieldName": MULTI}
    assert stats["controls"] == 1 and stats["warnings"] == 0


def test_multivalue_fallback_to_a_picklist_control_is_a_warning() -> None:
    client = LayoutClient()
    client.post = _reject_contribution(client.post)  # type: ignore[method-assign]
    stats, events = _run(client, _ir([_multi_field()]))

    assert [body["id"] for _, body, _ in client.sent("POST", "/controls")] == [f"{MULTI}.multivalue", MULTI]
    assert stats["controls"] == 1 and stats["warnings"] == 1
    assert any("got a standard picklist control (one value only)" in m for m in events)


def _reject_contribution(post: Any) -> Any:
    def wrapped(path: str, json_body: Any = None, **kwargs: Any) -> Any:
        if path.endswith("/controls") and (json_body or {}).get("isContribution"):
            post(path, json_body, **kwargs)
            raise AdoError("ADO 400 Bad Request: contribution not found", status=400)
        return post(path, json_body, **kwargs)

    return wrapped


@pytest.mark.parametrize("control_id", [MULTI, f"{MULTI}.multivalue"])
def test_existing_multivalue_control_under_either_id_is_not_added_again(control_id: str) -> None:
    contribution = {
        "id": control_id,
        "isContribution": True,
        "contribution": {"contributionId": MULTIVALUE_CONTRIBUTION_ID, "inputs": {"FieldName": MULTI}},
    }
    client = LayoutClient(controls=[contribution], wit_fields=[{"referenceName": MULTI}])
    stats, _ = _run(client, _ir([_multi_field()]))

    assert client.sent("POST", "/controls") == []
    assert client.sent("DELETE", "") == []
    assert stats["warnings"] == 0


def test_native_control_is_replaced_by_the_multivalue_control() -> None:
    client = LayoutClient(controls=[{"id": MULTI, "visible": True}], wit_fields=[{"referenceName": MULTI}])
    stats, events = _run(client, _ir([_multi_field()]))

    assert [body["id"] for _, body, _ in client.sent("POST", "/controls")] == [f"{MULTI}.multivalue"]
    [(path, _, _)] = client.sent("DELETE", f"/controls/{MULTI}")
    assert "/layout/groups/group-1/" in path
    assert stats["controls"] == 1 and stats["warnings"] == 0
    assert any("Removed the standard control" in m for m in events)


def test_native_control_that_cannot_be_removed_is_hidden() -> None:
    client = LayoutClient(
        controls=[{"id": MULTI, "visible": True}],
        wit_fields=[{"referenceName": MULTI}],
        fail={f"DELETE /controls/{MULTI}": AdoError("ADO 400: inherited control", status=400)},
    )
    stats, _ = _run(client, _ir([_multi_field()]))

    [(_, body, _)] = client.sent("PATCH", f"/controls/{MULTI}")
    assert body == {"id": MULTI, "visible": False}
    assert stats["warnings"] == 0


def test_layout_uses_the_process_local_copy_when_the_system_entry_is_listed_last() -> None:
    local = {
        "name": "Bug",
        "referenceName": "Demo.Bug",
        "customization": "inherited",
        "inherits": "Microsoft.VSTS.WorkItemTypes.Bug",
    }
    system = {"name": "Bug", "referenceName": "Microsoft.VSTS.WorkItemTypes.Bug", "customization": "system"}
    field = {"label": "Notes", "reference_name": "MSBPC.notes", "field_type": "String", "group_name": "Existing"}
    client = LayoutClient(wits=[local, system])
    _run(client, _ir([field], wit_name="Bug", wit_ref="Microsoft.VSTS.WorkItemTypes.Bug"))

    # No materialize PATCH of the system type; fields go to the process copy.
    assert client.sent("PATCH", "") == []
    [(path, _, _)] = client.sent("POST", "/fields")
    assert "/workItemTypes/Demo.Bug/fields" in path


def test_materialize_rejected_with_400_fails_the_step() -> None:
    system = {"name": "Bug", "referenceName": "Microsoft.VSTS.WorkItemTypes.Bug", "customization": "system"}
    field = {"label": "Notes", "reference_name": "MSBPC.notes", "field_type": "String", "group_name": "Existing"}
    client = LayoutClient(
        wits=[system],
        fail={
            "PATCH Microsoft.VSTS.WorkItemTypes.Bug": AdoError("ADO 400 Bad Request: nope", status=400),
            "PATCH workItemTypes/Bug": AdoError("ADO 400 Bad Request: nope", status=400),
        },
    )
    with pytest.raises(StepSetupError, match="WIT 'Bug' couldn't be customized"):
        _run(client, _ir([field], wit_name="Bug", wit_ref="Microsoft.VSTS.WorkItemTypes.Bug"))
    assert client.sent("POST", "/fields") == []


def test_materialize_conflict_counts_as_done() -> None:
    system = {"name": "Bug", "referenceName": "Microsoft.VSTS.WorkItemTypes.Bug", "customization": "system"}
    field = {"label": "Notes", "reference_name": "MSBPC.notes", "field_type": "String", "group_name": "Existing"}
    client = LayoutClient(
        wits=[system],
        fail={"PATCH Microsoft.VSTS.WorkItemTypes.Bug": AdoError("ADO 409 Conflict", status=409)},
    )
    stats, _ = _run(client, _ir([field], wit_name="Bug", wit_ref="Microsoft.VSTS.WorkItemTypes.Bug"))
    assert stats["fields_added"] == 1


def _custom_field(**extra: Any) -> dict[str, Any]:
    return {
        "label": "Priority level",
        "reference_name": "MSBPC.prioritylevel",
        "field_type": "String",
        "group_name": "Existing",
        "custom_field": True,
        **extra,
    }


def test_required_and_default_changes_apply_to_a_field_already_on_the_wit() -> None:
    client = LayoutClient(
        controls=[{"id": "MSBPC.prioritylevel"}],
        wit_fields=[{"referenceName": "MSBPC.prioritylevel", "required": False, "defaultValue": None}],
    )
    stats, _ = _run(client, _ir([_custom_field(required="Yes", default_value="Medium")]))

    [(path, body, _)] = client.sent("PATCH", "/fields/MSBPC.prioritylevel")
    assert "/workItemTypes/BPC.Workshop/fields/" in path
    assert body == {"required": True, "defaultValue": "Medium"}
    assert stats["fields_updated"] == 1 and stats["warnings"] == 0


def test_unchanged_field_settings_send_nothing() -> None:
    client = LayoutClient(
        controls=[{"id": "MSBPC.prioritylevel"}],
        wit_fields=[{"referenceName": "MSBPC.prioritylevel", "required": True, "defaultValue": "Medium"}],
    )
    stats, _ = _run(client, _ir([_custom_field(required="Yes", default_value="Medium")]))
    assert client.sent("PATCH", "") == [] and stats["fields_updated"] == 0


def test_base_type_fields_keep_their_azure_devops_rules() -> None:
    title = {"label": "Title", "reference_name": "System.Title", "field_type": "String", "group_name": "Existing"}
    client = LayoutClient(
        controls=[{"id": "System.Title"}],
        wit_fields=[{"referenceName": "System.Title", "required": True, "customization": "system"}],
    )
    _run(client, _ir([title]))
    assert client.sent("PATCH", "") == []


def test_field_update_failure_is_a_warning() -> None:
    client = LayoutClient(
        controls=[{"id": "MSBPC.prioritylevel"}],
        wit_fields=[{"referenceName": "MSBPC.prioritylevel", "required": False}],
        fail={"PATCH /fields/MSBPC.prioritylevel": AdoError("ADO 400 Bad Request", status=400)},
    )
    stats, events = _run(client, _ir([_custom_field(required="Yes")]))
    assert stats["warnings"] == 1 and stats["fields_updated"] == 0
    assert any("Could not update Required / Default value of MSBPC.prioritylevel" in m for m in events)


def test_groups_and_pages_are_not_retried_and_the_layout_is_read_once() -> None:
    fields = [
        {"label": f"Field {n}", "reference_name": f"MSBPC.f{n}", "field_type": "String",
         "group_name": "New group", "page_name": "Details"}
        for n in range(3)
    ]
    client = LayoutClient()
    stats, _ = _run(client, _ir(fields))

    [(_, _, group_kwargs)] = client.sent("POST", "/groups")
    assert group_kwargs.get("idempotent") is False
    assert [body["id"] for _, body, _ in client.sent("POST", "/controls")] == ["MSBPC.f0", "MSBPC.f1", "MSBPC.f2"]
    assert client.layout_gets == 1
    assert stats["controls"] == 3


def test_group_create_with_unknown_outcome_reuses_the_group_if_it_was_made() -> None:
    field = {"label": "Notes", "reference_name": "MSBPC.notes", "field_type": "String", "group_name": "New group"}
    client = LayoutClient()
    original_post = client.post

    def post(path: str, json_body: Any = None, **kwargs: Any) -> Any:
        if path.endswith("/groups"):
            client.layout["pages"][0]["sections"][0]["groups"].append(
                {"id": "group-made", "label": "New group", "controls": []}
            )
            client.calls.append(("POST", path, json_body, kwargs))
            raise AdoError("Network error (outcome unknown, not retried)", transient=True, outcome_unknown=True)
        return original_post(path, json_body, **kwargs)

    client.post = post  # type: ignore[method-assign]
    stats, _ = _run(client, _ir([field]))

    [(path, _, _)] = client.sent("POST", "/controls")
    assert "/layout/groups/group-made/controls" in path
    assert stats["controls"] == 1 and stats["warnings"] == 0


def test_group_create_with_unknown_outcome_asks_for_a_rerun() -> None:
    field = {"label": "Notes", "reference_name": "MSBPC.notes", "field_type": "String", "group_name": "New group"}
    client = LayoutClient(
        fail={"POST /groups": AdoError("Network error (outcome unknown)", transient=True, outcome_unknown=True)}
    )
    events: list[str] = []
    with pytest.raises(StepSetupError, match="didn't confirm group 'New group'"):
        apply_layout(
            client,  # type: ignore[arg-type]
            _ir([field]),
            dry_run=False,
            emit=lambda _step, message: events.append(message),
            process_id="proc-1",
        )
    assert any("Could not create group 'New group'" in m and "Run Layout again" in m for m in events)


def _gateway_timeout() -> AdoError:
    return AdoError("ADO 504 Gateway Timeout", status=504, transient=True, outcome_unknown=True)


def _new_fields(count: int, **placement: str) -> list[dict[str, Any]]:
    return [
        {"label": f"Field {n}", "reference_name": f"MSBPC.f{n}", "field_type": "String", **placement}
        for n in range(count)
    ]


def _run_failing(client: LayoutClient, ir: dict[str, Any]) -> tuple[StepSetupError, list[str]]:
    events: list[str] = []
    with pytest.raises(StepSetupError) as caught:
        _run_with(client, ir, events)
    return caught.value, events


def _run_with(client: LayoutClient, ir: dict[str, Any], events: list[str]) -> dict[str, Any]:
    return apply_layout(
        client,  # type: ignore[arg-type]
        ir,
        dry_run=False,
        emit=lambda _step, message: events.append(message),
        process_id="proc-1",
    )


def _post_times_out(client: LayoutClient, suffix: str, made: dict[str, Any] | None) -> None:
    """POSTs ending in ``suffix`` time out; ``made`` (a page or group) still lands on the form."""
    original_post = client.post

    def post(path: str, json_body: Any = None, **kwargs: Any) -> Any:
        if path.endswith(suffix):
            client.calls.append(("POST", path, json_body, kwargs))
            if made is not None:
                if suffix == "/pages":
                    client.layout["pages"].append(made)
                else:
                    client.layout["pages"][0]["sections"][0]["groups"].append(made)
            raise _gateway_timeout()
        return original_post(path, json_body, **kwargs)

    client.post = post  # type: ignore[method-assign]


def test_unreadable_form_still_adds_every_field_and_fails_the_step() -> None:
    client = LayoutClient()
    original_get = client.get

    def get(path: str, **kwargs: Any) -> Any:
        if path.endswith("/layout"):
            client.layout_gets += 1
            raise AdoError("ADO 500 Internal Server Error", status=500)
        return original_get(path, **kwargs)

    client.get = get  # type: ignore[method-assign]
    error, events = _run_failing(client, _ir(_new_fields(3, group_name="Existing")))

    assert [body["referenceName"] for _, body, _ in client.sent("POST", "/fields")] == [
        "MSBPC.f0", "MSBPC.f1", "MSBPC.f2"
    ]
    assert client.sent("POST", "/controls") == [] and client.sent("POST", "/groups") == []
    assert client.layout_gets == 1
    assert error.stats["fields_added"] == 3
    assert "The form of 'Workshop' couldn't be read (ADO 500" in str(error)
    assert "the form wasn't updated for MSBPC.f0, MSBPC.f1, MSBPC.f2" in str(error)
    assert any("Could not read the form of Workshop" in m for m in events)


def test_group_create_timeout_reuses_the_group_for_the_next_field() -> None:
    client = LayoutClient()
    _post_times_out(client, "/groups", {"id": "group-made", "label": "New group", "controls": []})
    stats, _ = _run(client, _ir(_new_fields(2, group_name="New group")))

    assert len(client.sent("POST", "/groups")) == 1
    assert [path for path, _, _ in client.sent("POST", "/controls")] == [
        "/_apis/work/processes/proc-1/workItemTypes/BPC.Workshop/layout/groups/group-made/controls"
    ] * 2
    assert stats["controls"] == 2 and stats["warnings"] == 0


def test_group_create_timeout_not_on_the_form_is_not_sent_again() -> None:
    client = LayoutClient()
    _post_times_out(client, "/groups", None)
    error, events = _run_failing(client, _ir(_new_fields(2, group_name="New group")))

    assert len(client.sent("POST", "/groups")) == 1
    assert client.sent("POST", "/controls") == []
    assert "Azure DevOps didn't confirm group 'New group' was added to 'Workshop' (ADO 504" in str(error)
    assert "the form wasn't updated for MSBPC.f0, MSBPC.f1" in str(error)
    assert sum("Could not create group 'New group'" in m for m in events) == 1


def _extra_page() -> dict[str, Any]:
    return {
        "id": "page-extra",
        "label": "Extra",
        "sections": [{"id": "Section1", "groups": [{"id": "group-extra", "label": "Existing", "controls": []}]}],
    }


def test_page_create_timeout_reuses_the_page_for_the_next_field() -> None:
    client = LayoutClient()
    _post_times_out(client, "/pages", _extra_page())
    stats, _ = _run(client, _ir(_new_fields(2, group_name="Existing", page_name="Extra")))

    assert len(client.sent("POST", "/pages")) == 1
    assert client.sent("POST", "/groups") == []
    assert all("/groups/group-extra/controls" in path for path, _, _ in client.sent("POST", "/controls"))
    assert stats["controls"] == 2 and stats["warnings"] == 0


def test_page_create_timeout_not_on_the_form_is_not_sent_again() -> None:
    client = LayoutClient()
    _post_times_out(client, "/pages", None)
    error, _ = _run_failing(client, _ir(_new_fields(2, group_name="Existing", page_name="Extra")))

    assert len(client.sent("POST", "/pages")) == 1
    assert client.sent("POST", "/controls") == []
    assert "Azure DevOps didn't confirm page 'Extra' was added to 'Workshop' (ADO 504" in str(error)
    assert "the form wasn't updated for MSBPC.f0, MSBPC.f1" in str(error)


def test_page_created_but_form_unreadable_afterwards_is_not_added_twice() -> None:
    client = LayoutClient()
    original_get, original_post = client.get, client.post

    def post(path: str, json_body: Any = None, **kwargs: Any) -> Any:
        if path.endswith("/pages"):
            client.calls.append(("POST", path, json_body, kwargs))
            return _extra_page()
        return original_post(path, json_body, **kwargs)

    def get(path: str, **kwargs: Any) -> Any:
        if path.endswith("/layout") and client.sent("POST", "/pages"):
            raise AdoError("ADO 503 Service Unavailable", status=503, transient=True)
        return original_get(path, **kwargs)

    client.post, client.get = post, get  # type: ignore[method-assign]
    stats, _ = _run(client, _ir(_new_fields(2, group_name="Existing", page_name="Extra")))

    assert len(client.sent("POST", "/pages")) == 1
    assert all("/groups/group-extra/controls" in path for path, _, _ in client.sent("POST", "/controls"))
    assert stats["controls"] == 2


def test_html_control_timeout_not_on_the_form_fails_the_step() -> None:
    field = {"label": "Notes", "reference_name": "MSBPC.notes", "field_type": "html", "is_html": True,
             "group_name": "Notes"}
    client = LayoutClient()
    _post_times_out(client, "/groups", None)
    error, _ = _run_failing(client, _ir([field]))

    assert len(client.sent("POST", "/groups")) == 1
    assert "didn't confirm the HTML control for MSBPC.notes was added to 'Workshop'" in str(error)
