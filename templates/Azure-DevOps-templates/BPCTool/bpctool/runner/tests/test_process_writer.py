"""The Process step fails when something the workbook defines can't be created."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from app.ado_client import AdoError
from app.run_outcome import describe_step
from app.writers import process as process_writer
from app.writers.process import (
    FIELD_DESCRIPTION_MAX,
    TEST_CASE_REFERENCE,
    ProcessSetupError,
    apply_process,
    shorten_description,
    states_added_by_process,
    write_shortened_descriptions_csv,
)


def _ir() -> dict[str, Any]:
    return {
        "targets": {"process_name": "Contoso BPC"},
        "counts": {"work_item_types": 1, "fields": 1, "html_fields": 0},
        "process": {
            "work_item_types": [
                {"name": "Business Process", "custom": True, "reference_name": "ContosoBPC.BusinessProcess"},
            ],
            "picklists": {"Industry": ["Retail", "Finance"]},
            "fields": [
                {
                    "custom_field": True,
                    "reference_name": "Custom.Industry",
                    "field_type": "PicklistString",
                    "label": "Industry",
                    "field_name": "Industry",
                },
            ],
        },
    }


STATE_CATEGORIES = {"New": "Proposed", "Design": "Proposed", "Ready": "InProgress", "Closed": "Completed"}


class FakeClient:
    def __init__(
        self,
        *,
        fail: dict[str, AdoError] | None = None,
        fields: list[str] | None = None,
        max_description: int | None = None,
        wits: list[dict[str, Any]] | None = None,
        states: list[str] | None = None,
        list_lag: int = 0,
        order: str | None = None,
    ) -> None:
        # order: None = states come back without order / category; "honour", "ignore" or
        # "reject" = what Azure DevOps does with a new state's order.
        self.order = order
        self.fail = fail or {}
        self.fields = fields or []
        self.lists: list[dict[str, Any]] = []
        # A created list is left out of this many later list reads (Azure DevOps read lag).
        self.list_lag = list_lag
        self.hidden: dict[str, int] = {}
        self.list_reads = 0
        self.max_description = max_description
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.wits = wits or []
        self.states = states if states is not None else ["Design", "Ready", "Closed"]

    def create_process(self, _name: str) -> str:
        return "proc-1"

    def list_process_wits(self, _process_id: str) -> list[dict[str, Any]]:
        return self.wits

    def get(self, path: str, **_kw: Any) -> dict[str, Any]:
        if path.endswith("/states"):
            self._maybe_fail("GET", path)
            if self.order is None:
                return {"value": [{"name": name} for name in self.states]}
            return {
                "value": [
                    {"name": name, "stateCategory": STATE_CATEGORIES[name], "order": index + 1}
                    for index, name in enumerate(self.states)
                ]
            }
        if path.endswith("/lists"):
            self.list_reads += 1
            visible = [item for item in self.lists if not self.hidden.get(item["name"])]
            for name in self.hidden:
                self.hidden[name] = max(0, self.hidden[name] - 1)
            return {"value": visible}
        if path.endswith("/wit/fields"):
            return {"value": [{"referenceName": ref} for ref in self.fields]}
        return {}

    def _maybe_fail(self, method: str, path: str) -> None:
        for key, exc in self.fail.items():
            if key == f"{method} {path.split('/_apis/')[-1].split('/')[-1]}":
                raise exc

    def _check_description(self, json_body: dict[str, Any]) -> None:
        if self.max_description is not None and len(json_body.get("description") or "") > self.max_description:
            raise AdoError("ADO 400 Bad Request: description is too long", status=400)

    def post(self, path: str, *, json_body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self._maybe_fail("POST", path)
        self.sent.append((path, json_body))
        self._check_description(json_body)
        if path.endswith("/lists"):
            self.lists.append({"name": json_body["name"], "id": "list-1"})
            self.hidden[json_body["name"]] = self.list_lag
            return {"name": json_body["name"], "id": "list-1"}
        if path.endswith("/states"):
            if "order" in json_body and self.order == "reject":
                raise AdoError("ADO 400 Bad Request: invalid order", status=400)
            if "order" in json_body and self.order == "honour":
                self.states.insert(json_body["order"] - 1, json_body["name"])
            else:
                self.states.append(json_body["name"])
        return {"name": json_body.get("name"), "referenceName": json_body.get("referenceName")}

    def patch(self, path: str, **_kw: Any) -> dict[str, Any]:
        self._maybe_fail("PATCH", path)
        return {}

    def put(self, path: str, **_kw: Any) -> dict[str, Any]:
        self._maybe_fail("PUT", path)
        return {}


def _run(client: FakeClient, ir: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    messages: list[str] = []
    result = apply_process(client, ir, dry_run=False, emit=lambda _step, msg: messages.append(msg))
    return result, messages


def test_everything_created_is_ok() -> None:
    result, _ = _run(FakeClient(), _ir())
    assert result["wits_created"] == 1 and result["picklists"] == 1 and result["fields_created"] == 1
    assert describe_step("process", result, dry_run=False)[0] == "ok"


def test_new_picklist_field_uses_the_id_from_create_despite_read_lag(monkeypatch) -> None:
    # Issue #372: a list created moments ago isn't in the next list read yet.
    monkeypatch.setattr(process_writer.time, "sleep", lambda _s: pytest.fail("no wait needed"))
    client = FakeClient(list_lag=10)
    result, _ = _run(client, _ir())
    assert result["fields_created"] == 1
    field = next(body for path, body in client.sent if path.endswith("/wit/fields"))
    assert field["picklistId"] == "list-1"
    assert client.list_reads == 1


def test_existing_picklist_missing_from_the_first_read_is_looked_up_again(monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(process_writer.time, "sleep", waits.append)
    client = FakeClient(fail={"POST lists": AdoError("409 Conflict: already exists", status=409)})
    client.lists.append({"name": "Industry", "id": "list-7"})
    client.hidden["Industry"] = 2
    result, _ = _run(client, _ir())
    assert result["fields_created"] == 1
    field = next(body for path, body in client.sent if path.endswith("/wit/fields"))
    assert field["picklistId"] == "list-7"
    assert waits == [process_writer.PICKLIST_LOOKUP_DELAY_S]


def test_picklist_that_never_appears_fails_the_field(monkeypatch) -> None:
    monkeypatch.setattr(process_writer.time, "sleep", lambda _s: None)
    client = FakeClient(fail={"POST lists": AdoError("409 Conflict: already exists", status=409)})
    with pytest.raises(ProcessSetupError, match="picklist 'Industry' wasn't found"):
        _run(client, _ir())
    assert client.list_reads == 1 + process_writer.PICKLIST_LOOKUP_ATTEMPTS


def test_a_work_item_type_that_cannot_be_created_fails_the_step() -> None:
    client = FakeClient(fail={"POST workItemTypes": AdoError("403 Forbidden: insufficient permissions", status=403)})
    with pytest.raises(ProcessSetupError) as caught:
        _run(client, _ir())
    message = str(caught.value)
    assert "1 work item type(s) / picklist(s) / field(s)" in message
    assert "Business Process" in message and "403" in message


def test_a_field_that_cannot_be_created_fails_the_step_after_trying_the_rest() -> None:
    client = FakeClient(fail={"POST fields": AdoError("400 Bad Request", status=400)})
    messages: list[str] = []
    with pytest.raises(ProcessSetupError):
        apply_process(client, _ir(), dry_run=False, emit=lambda _s, m: messages.append(m))
    assert any(m.startswith("FAILED: Field 'Custom.Industry'") for m in messages)
    assert any(m.startswith("Process apply finished") for m in messages)


def test_already_existing_items_are_not_failures() -> None:
    client = FakeClient(fail={"POST workItemTypes": AdoError("409 Conflict", status=409)})
    result, _ = _run(client, _ir())
    assert result["wits_skipped"] == 1


def test_an_existing_field_is_used_as_is_without_warnings() -> None:
    client = FakeClient(
        fields=["Custom.Industry"],
        fail={"PATCH Custom.Industry": AdoError("404 Not Found", status=404)},
    )
    result, _ = _run(client, _ir())
    assert result["fields_existing"] == 1 and result["fields_created"] == 0
    assert result["warnings"] == 0
    status, text = describe_step("process", result, dry_run=False)
    assert status == "ok" and "(1 already existed)" in text


def test_a_picklist_that_cannot_be_created_is_reported_once() -> None:
    client = FakeClient(fail={"POST lists": AdoError("403 Forbidden", status=403)})
    with pytest.raises(ProcessSetupError) as caught:
        _run(client, _ir())
    assert "1 work item type(s) / picklist(s) / field(s)" in str(caught.value)
    assert "Picklist 'Industry'" in str(caught.value)


def test_update_failures_are_warnings() -> None:
    client = FakeClient(fail={"PUT list-1": AdoError("400 Bad Request", status=400)})
    client.lists.append({"name": "Industry", "id": "list-1"})
    result, messages = _run(client, _ir())
    assert result["warnings"] == 1
    assert any(m.startswith("WARNING: Picklist 'Industry' exists") for m in messages)
    status, text = describe_step("process", result, dry_run=False)
    assert status == "warning" and "1 warning" in text


LONG = ("Describes how the business process is measured and reported across regions. " * 5).strip()


def _ir_with(field_description: str = "", wit_description: str = "") -> dict[str, Any]:
    ir = _ir()
    ir["process"]["fields"][0]["description"] = field_description
    ir["process"]["work_item_types"][0]["help_text"] = wit_description
    return ir


def _field_posts(client: FakeClient) -> list[dict[str, Any]]:
    return [body for path, body in client.sent if path.endswith("/wit/fields")]


def test_shorten_description_cuts_at_a_word_and_fits() -> None:
    text = shorten_description(LONG)
    assert len(LONG) > FIELD_DESCRIPTION_MAX >= len(text)
    assert text.endswith("…") and not text.endswith(" …")
    assert LONG.startswith(text[:-1])
    assert shorten_description("  short  ") == "short"


def test_long_field_description_is_shortened_before_create() -> None:
    client = FakeClient()
    result, messages = _run(client, _ir_with(field_description=LONG))
    assert result["fields_created"] == 1
    assert len(_field_posts(client)) == 1
    assert _field_posts(client)[0]["description"] == shorten_description(LONG)
    [row] = result["descriptions_shortened"]
    assert row["kind"] == "field" and row["reference_name"] == "Custom.Industry"
    assert row["original"] == LONG and row["original_length"] == len(LONG)
    assert any("to fit Azure DevOps' 255-character limit" in m for m in messages)
    status, text = describe_step("process", result, dry_run=False)
    assert status == "warning" and "1 description shortened" in text


def test_rejected_field_description_is_dropped_and_field_still_created() -> None:
    client = FakeClient(max_description=100)
    result, messages = _run(client, _ir_with(field_description=LONG))
    assert result["fields_created"] == 1
    assert [body["description"] for body in _field_posts(client)] == [shorten_description(LONG), ""]
    [row] = result["descriptions_shortened"]
    assert row["sent"] == "" and row["sent_length"] == 0
    assert any("left empty because Azure DevOps rejected it" in m for m in messages)


def test_rejected_work_item_type_description_is_shortened_then_retried() -> None:
    client = FakeClient(max_description=FIELD_DESCRIPTION_MAX)
    result, _ = _run(client, _ir_with(wit_description=LONG))
    assert result["wits_created"] == 1
    wit_posts = [body for path, body in client.sent if path.endswith("/workItemTypes")]
    assert [body["description"] for body in wit_posts] == [LONG, shorten_description(LONG)]
    assert result["descriptions_shortened"][0]["kind"] == "work item type"


def test_short_descriptions_are_sent_unchanged() -> None:
    client = FakeClient()
    result, _ = _run(client, _ir_with(field_description="Industry of the customer.", wit_description="A process."))
    assert result["descriptions_shortened"] == [] and result["warnings"] == 0
    assert _field_posts(client)[0]["description"] == "Industry of the customer."


def test_non_description_400_still_fails_after_retries() -> None:
    client = FakeClient(fail={"POST fields": AdoError("ADO 400 Bad Request: bad name", status=400)})
    with pytest.raises(ProcessSetupError) as caught:
        _run(client, _ir_with(field_description="Industry of the customer."))
    assert "bad name" in str(caught.value)


def test_shortened_descriptions_csv(tmp_path: Path) -> None:
    result, _ = _run(FakeClient(), _ir_with(field_description=LONG))
    path = tmp_path / "descriptions-shortened.csv"
    write_shortened_descriptions_csv(result["descriptions_shortened"], path)
    with path.open(encoding="utf-8") as handle:
        [row] = list(csv.DictReader(handle))
    assert row["reference_name"] == "Custom.Industry" and row["original"] == LONG


TEST_CASE_LOCAL = {"name": "Test Case", "referenceName": "ContosoBPC.TestCase", "customization": "inherited"}


def _ir_with_test_case(*, disabled: bool = False) -> dict[str, Any]:
    ir = _ir()
    ir["process"]["work_item_types"].append(
        {"name": "Test case", "custom": False, "disabled": disabled, "reference_name": TEST_CASE_REFERENCE}
    )
    return ir


def _state_posts(client: FakeClient) -> list[dict[str, Any]]:
    return [body for path, body in client.sent if path.endswith("/states")]


def test_process_adds_new_state_to_test_case() -> None:
    client = FakeClient(wits=[TEST_CASE_LOCAL])
    result, messages = _run(client, _ir_with_test_case())
    assert _state_posts(client) == [{"name": "New", "color": "b2b2b2", "stateCategory": "Proposed"}]
    assert any(path.endswith("/workItemTypes/ContosoBPC.TestCase/states") for path, _ in client.sent)
    assert result["states_added"] == 1 and result["warnings"] == 0
    assert any("Added state 'New' (Proposed) to 'Test case'" in m for m in messages)


def test_new_state_is_placed_first_so_test_cases_start_in_it() -> None:
    client = FakeClient(wits=[TEST_CASE_LOCAL], order="honour")
    result, _ = _run(client, _ir_with_test_case())
    assert _state_posts(client) == [{"name": "New", "color": "b2b2b2", "stateCategory": "Proposed", "order": 1}]
    assert client.states == ["New", "Design", "Ready", "Closed"]
    assert result["states_added"] == 1 and result["warnings"] == 0


def test_new_state_azure_devops_did_not_place_first_is_a_warning() -> None:
    client = FakeClient(wits=[TEST_CASE_LOCAL], order="ignore")
    result, messages = _run(client, _ir_with_test_case())
    assert result["states_added"] == 1 and result["warnings"] == 1
    assert any("'Design' is still its first state" in m and "move 'New' up" in m for m in messages)


def test_refused_order_still_adds_the_state() -> None:
    client = FakeClient(wits=[TEST_CASE_LOCAL], order="reject")
    result, messages = _run(client, _ir_with_test_case())
    assert [body.get("order") for body in _state_posts(client)] == [1, None]
    assert client.states == ["Design", "Ready", "Closed", "New"]
    assert result["states_added"] == 1 and result["warnings"] == 1
    assert any("not placed first: ADO 400" in m for m in messages)


def test_state_goes_to_the_process_copy_when_the_system_entry_shares_its_name() -> None:
    system = {"name": "Test Case", "referenceName": TEST_CASE_REFERENCE, "customization": "system"}
    local = {**TEST_CASE_LOCAL, "inherits": TEST_CASE_REFERENCE}
    client = FakeClient(wits=[local, system])
    result, _ = _run(client, _ir_with_test_case())
    assert result["states_added"] == 1 and result["warnings"] == 0
    assert any(path.endswith("/workItemTypes/ContosoBPC.TestCase/states") for path, _ in client.sent)


def test_failed_step_keeps_its_shortened_descriptions() -> None:
    client = FakeClient(fail={"POST lists": AdoError("403 Forbidden", status=403)})
    ir = _ir_with(field_description=LONG)
    ir["process"]["fields"].append(
        {"custom_field": True, "reference_name": "Custom.Notes", "field_type": "String",
         "label": "Notes", "field_name": "Notes", "description": LONG}
    )
    with pytest.raises(ProcessSetupError) as caught:
        _run(client, ir)
    [row] = caught.value.stats["descriptions_shortened"]
    assert row["reference_name"] == "Custom.Notes"


def test_existing_new_state_is_not_added_again() -> None:
    client = FakeClient(wits=[TEST_CASE_LOCAL], states=["New", "Design", "Ready", "Closed"])
    result, _ = _run(client, _ir_with_test_case())
    assert _state_posts(client) == [] and result["states_added"] == 0


def test_state_that_cannot_be_added_is_a_warning() -> None:
    client = FakeClient(
        wits=[TEST_CASE_LOCAL], fail={"POST states": AdoError("ADO 403 Forbidden", status=403)}
    )
    result, messages = _run(client, _ir_with_test_case())
    assert result["warnings"] == 1 and result["states_added"] == 0
    assert any("State 'New' couldn't be added to 'Test case'" in m for m in messages)


def test_state_that_already_exists_is_not_a_warning() -> None:
    client = FakeClient(wits=[TEST_CASE_LOCAL], fail={"POST states": AdoError("409 Conflict", status=409)})
    result, _ = _run(client, _ir_with_test_case())
    assert result["warnings"] == 0 and result["states_added"] == 0


def test_test_case_not_in_process_is_a_warning() -> None:
    result, messages = _run(FakeClient(), _ir_with_test_case())
    assert result["warnings"] == 1
    assert any("it isn't customized in this process" in m for m in messages)


def test_states_added_by_process() -> None:
    assert states_added_by_process(_ir_with_test_case()) == {"Test case": ["New"]}
    assert states_added_by_process(_ir_with_test_case(disabled=True)) == {}
    assert states_added_by_process(_ir()) == {}


class PatchRecordingClient(FakeClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.patches: list[tuple[str, dict[str, Any]]] = []

    def patch(self, path: str, **kw: Any) -> dict[str, Any]:
        self._maybe_fail("PATCH", path)
        self.patches.append((path, kw.get("json_body") or {}))
        return {}


BUG_REF = "Microsoft.VSTS.WorkItemTypes.Bug"


def _ir_with_bug(*, disabled: bool = False) -> dict[str, Any]:
    ir = _ir()
    ir["process"]["work_item_types"] = [
        {"name": "Bug", "custom": False, "disabled": disabled, "reference_name": BUG_REF}
    ]
    return ir


def _local_bug(**extra: Any) -> dict[str, Any]:
    return {"name": "Bug", "referenceName": "ContosoBPC.Bug", "customization": "inherited", "inherits": BUG_REF, **extra}


def test_wit_disabled_earlier_is_enabled_again_when_the_workbook_enables_it() -> None:
    client = PatchRecordingClient(wits=[_local_bug(isDisabled=True)])
    result, messages = _run(client, _ir_with_bug())
    [(path, body)] = [(p, b) for p, b in client.patches if "/workItemTypes/" in p]
    assert path.endswith("/workItemTypes/ContosoBPC.Bug")
    assert body["isDisabled"] is False
    assert result["wits_updated"] == 1
    assert any("Enabled WIT 'Bug' again" in m for m in messages)


def test_enabled_wit_update_does_not_send_is_disabled() -> None:
    client = PatchRecordingClient(wits=[_local_bug(isDisabled=False)])
    _run(client, _ir_with_bug())
    [(_, body)] = [(p, b) for p, b in client.patches if "/workItemTypes/" in p]
    assert "isDisabled" not in body


def test_wit_that_cannot_be_enabled_again_fails_the_step() -> None:
    client = PatchRecordingClient(
        wits=[_local_bug(isDisabled=True)],
        fail={"PATCH ContosoBPC.Bug": AdoError("ADO 403 Forbidden", status=403)},
    )
    with pytest.raises(ProcessSetupError, match="WIT 'Bug' is disabled in Azure DevOps and couldn't be enabled"):
        _run(client, _ir_with_bug())


def test_process_copy_is_updated_when_the_system_entry_is_listed_last() -> None:
    system = {"name": "Bug", "referenceName": BUG_REF, "customization": "system"}
    client = PatchRecordingClient(wits=[_local_bug(), system])
    result, _ = _run(client, _ir_with_bug())
    assert not [path for path, _ in client.sent if path.endswith("/workItemTypes")]
    assert [p for p, _ in client.patches if "/workItemTypes/" in p] == [
        "/_apis/work/processes/proc-1/workItemTypes/ContosoBPC.Bug"
    ]
    assert result["wits_updated"] == 1 and result["wits_created"] == 0


def test_picklist_create_is_not_retried_and_an_unknown_outcome_is_looked_up(monkeypatch) -> None:
    monkeypatch.setattr(process_writer.time, "sleep", lambda _s: None)

    class UnknownOutcome(FakeClient):
        def post(self, path: str, *, json_body: dict[str, Any], **kw: Any) -> dict[str, Any]:
            if path.endswith("/lists"):
                assert kw.get("idempotent") is False
                self.lists.append({"name": json_body["name"], "id": "list-9"})
                raise AdoError("Network error (outcome unknown, not retried)", transient=True, outcome_unknown=True)
            return super().post(path, json_body=json_body, **kw)

    client = UnknownOutcome()
    result, messages = _run(client, _ir())
    assert result["fields_created"] == 1
    assert _field_posts(client)[0]["picklistId"] == "list-9"
    assert any("checking whether Azure DevOps created it" in m for m in messages)


def test_picklist_with_unknown_outcome_that_was_not_created_fails_its_field(monkeypatch) -> None:
    monkeypatch.setattr(process_writer.time, "sleep", lambda _s: None)
    client = FakeClient(
        fail={"POST lists": AdoError("Network error (outcome unknown)", transient=True, outcome_unknown=True)}
    )
    with pytest.raises(ProcessSetupError, match="picklist 'Industry' wasn't found"):
        _run(client, _ir())


def _list_posts(client: FakeClient) -> list[dict[str, Any]]:
    return [body for path, body in client.sent if path.endswith("/lists")]


def test_integer_picklist_creates_an_integer_list_and_field_whatever_the_type_casing() -> None:
    ir = _ir()
    ir["process"]["picklists"]["Severity"] = ["1", "2", "3"]
    ir["process"]["fields"][0]["field_type"] = "pickliststring"
    ir["process"]["fields"].append(
        {
            "custom_field": True,
            "reference_name": "Custom.Severity",
            "field_type": "picklistInteger",
            "label": "Severity",
            "field_name": "Severity",
        }
    )
    client = FakeClient()
    result, _ = _run(client, ir)
    assert result["fields_created"] == 2
    lists = {body["name"]: body for body in _list_posts(client)}
    assert lists["Industry"]["type"] == "String" and lists["Severity"]["type"] == "Integer"
    assert lists["Severity"]["items"] == ["1", "2", "3"]
    fields = {body["referenceName"]: body for body in _field_posts(client)}
    assert fields["Custom.Industry"]["type"] == "String" and fields["Custom.Industry"]["isPicklist"]
    assert fields["Custom.Severity"]["type"] == "integer" and fields["Custom.Severity"]["isPicklist"]


def test_existing_picklist_differing_only_in_case_is_updated_not_duplicated() -> None:
    class RecordingPuts(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.puts: list[str] = []

        def put(self, path: str, **kw: Any) -> dict[str, Any]:
            self.puts.append(path)
            return super().put(path, **kw)

    client = RecordingPuts()
    client.lists.append({"name": "industry", "id": "list-3"})
    result, _ = _run(client, _ir())
    assert _list_posts(client) == []
    assert client.puts == ["/_apis/work/processes/lists/list-3"]
    assert _field_posts(client)[0]["picklistId"] == "list-3"
    assert result["fields_created"] == 1


def test_picklist_found_after_409_matches_its_name_ignoring_case(monkeypatch) -> None:
    monkeypatch.setattr(process_writer.time, "sleep", lambda _s: None)
    client = FakeClient(fail={"POST lists": AdoError("409 Conflict: already exists", status=409)})
    client.lists.append({"name": "INDUSTRY", "id": "list-8"})
    client.hidden["INDUSTRY"] = 1
    result, _ = _run(client, _ir())
    assert result["fields_created"] == 1
    assert _field_posts(client)[0]["picklistId"] == "list-8"
