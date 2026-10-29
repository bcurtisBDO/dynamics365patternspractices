from app.writers.process import (
    _is_system_reference,
    _resolve_existing_wit,
    _short_reference_name,
    index_process_wits,
)


def test_system_reference_helpers():
    assert _is_system_reference("Microsoft.VSTS.WorkItemTypes.Bug")
    assert not _is_system_reference("MSBPCTemplate.Process")
    assert _short_reference_name("Microsoft.VSTS.WorkItemTypes.TestCase") == "TestCase"


def test_index_prefers_lookup_by_name_and_short_ref():
    index = index_process_wits(
        [
            {
                "name": "Bug",
                "referenceName": "MSBPCTemplate.Bug",
                "customization": "inherited",
            }
        ]
    )
    assert _resolve_existing_wit(index, "Bug", "Microsoft.VSTS.WorkItemTypes.Bug")["name"] == "Bug"
    assert index["bug"]["referenceName"] == "MSBPCTemplate.Bug"


def test_index_and_resolver_prefer_the_process_copy_over_a_later_system_entry():
    from app.writers.process import resolve_process_wit

    local = {
        "name": "Bug",
        "referenceName": "MSBPCTemplate.Bug",
        "customization": "inherited",
        "inherits": "Microsoft.VSTS.WorkItemTypes.Bug",
    }
    system = {"name": "Bug", "referenceName": "Microsoft.VSTS.WorkItemTypes.Bug", "customization": "system"}
    index = index_process_wits([local, system])
    assert index["bug"] is local
    assert resolve_process_wit(index, "Bug", "Microsoft.VSTS.WorkItemTypes.Bug") is local
    assert resolve_process_wit(index, "Bug") is local
    assert resolve_process_wit(index_process_wits([system]), "Bug", "Microsoft.VSTS.WorkItemTypes.Bug") is system
