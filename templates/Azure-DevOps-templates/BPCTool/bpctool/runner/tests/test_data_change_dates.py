from __future__ import annotations

from app.catalog_changes import _comparable


def _value(text: str):
    return _comparable("Custom.Notes", text, rich=False, multi=False)


def test_text_starting_with_a_date_is_compared_in_full() -> None:
    assert _value("2025-01-15 Added step 3") != _value("2025-01-15 Removed step")
    assert _value("2025-01-15 Added step 3") == "2025-01-15 Added step 3"


def test_whole_date_values_still_compare_by_day() -> None:
    assert _value("2025-01-15") == _value("2025-01-15T00:00:00Z") == "2025-01-15"
    assert _value("2025-01-15 00:00:00") == _value("2025-01-15T00:00:00.000+00:00")
    assert _value("2025-01-15") != _value("2025-01-16T00:00:00Z")
