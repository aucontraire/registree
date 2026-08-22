"""Duplicate-name classification behavior."""

from __future__ import annotations

from typing import Any

from registree.conflicts import find_conflicts


def test_layered_orm_pair_is_amnestied(sample_registry_doc: dict[str, Any]) -> None:
    report = find_conflicts(sample_registry_doc)
    layered_names = {c.name for c in report.layered}
    assert layered_names == {"Widget"}
    widget = report.layered[0]
    assert widget.suggestions == {}
    assert report.alias_convention == "from widgetlib.db.models import X as XDB"


def test_same_layer_duplicate_is_a_smell_with_suggestions(
    sample_registry_doc: dict[str, Any],
) -> None:
    report = find_conflicts(sample_registry_doc)
    smell_names = {c.name for c in report.smells}
    assert smell_names == {"Sprocket"}
    sprocket = report.smells[0]
    assert sprocket.same_layer is True
    assert len(sprocket.suggestions) == 2
    assert all(names for names in sprocket.suggestions.values())


def test_totals_add_up(sample_registry_doc: dict[str, Any]) -> None:
    report = find_conflicts(sample_registry_doc)
    assert report.total_duplicate_names == len(report.smells) + len(report.layered)
    assert report.total_duplicate_names == 2
