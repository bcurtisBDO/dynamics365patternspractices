from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from decimal import Decimal

from app.catalog_import import _row_to_draft
from app.catalog_validate import _template_default
from app.excel_ir import _cell
from app.mapping import FieldDef, TemplateMapping
from app.richtext import normalize_field_value


def test_typed_cells_become_json_values() -> None:
    assert normalize_field_value(datetime(2025, 1, 15), "DateTime") == "2025-01-15"
    assert normalize_field_value(datetime(2025, 1, 15, 9, 30), "DateTime") == "2025-01-15T09:30:00"
    assert (
        normalize_field_value(datetime(2025, 1, 15, tzinfo=timezone.utc), "DateTime")
        == "2025-01-15T00:00:00+00:00"
    )
    assert normalize_field_value(date(2025, 1, 15), "String") == "2025-01-15"
    assert normalize_field_value(time(9, 30), "String") == "09:30:00"
    assert normalize_field_value(Decimal("3"), "Integer") == 3
    assert normalize_field_value(Decimal("2.5"), "Double") == 2.5
    assert normalize_field_value(datetime(2025, 1, 15), "HTML") == "2025-01-15"


def test_catalog_row_with_date_cells_serializes() -> None:
    mapping = TemplateMapping()
    mapping.aliases["go live"] = "Custom.GoLive"
    mapping.aliases["notes"] = "Custom.Notes"
    mapping.html_field_refs.add("Custom.Notes")
    draft = _row_to_draft(
        {
            "Title 1": "Order to cash",
            "Process sequence ID": "10",
            "Go live": datetime(2025, 3, 1),
            "Notes": datetime(2025, 3, 1, 8, 0),
            "_source_file": "bpc.xlsx",
            "_source_row": 3,
        },
        mapping,
    )
    json.dumps(draft.fields)
    assert draft.fields["Custom.GoLive"] == "2025-03-01"
    assert draft.fields["Custom.Notes"] == "2025-03-01T08:00:00"


def test_template_default_matches_catalog_value_formatting() -> None:
    assert _cell(datetime(2025, 1, 15)) == "2025-01-15"
    typed = FieldDef(label="Go live", reference_name="Custom.GoLive", field_type="DateTime",
                     default_value=datetime(2025, 1, 15))
    from_ir = FieldDef(label="Go live", reference_name="Custom.GoLive", field_type="DateTime",
                       default_value=_cell(datetime(2025, 1, 15)))
    assert _template_default(typed) == _template_default(from_ir) == "2025-01-15"
    assert normalize_field_value(datetime(2025, 1, 15), "DateTime") == _template_default(typed)
