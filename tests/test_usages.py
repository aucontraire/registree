"""Usage-analysis behavior, especially import-alias tracking."""

from __future__ import annotations

from registree.config import RegistreeConfig
from registree.usages import ClassUsageAnalyzer


def test_alias_usages_are_tracked(sample_config: RegistreeConfig) -> None:
    grouped = ClassUsageAnalyzer(sample_config, "Widget").analyze()

    service_rows = [
        u
        for rows in grouped.values()
        for u in rows
        if u.file_path == "src/widgetlib/services/widget_service.py"
    ]
    aliases = {u.via_alias for u in service_rows if u.via_alias}
    assert aliases == {"WidgetDB"}

    # The aliased instantiation `WidgetDB()` must be found — it is the exact
    # site a rename has to touch.
    instantiations = {
        (u.via_alias, u.line_content.strip()) for u in grouped.get("instantiation", [])
    }
    assert ("WidgetDB", "row = WidgetDB()") in instantiations
    assert (None, 'return Widget(name="w")') in instantiations


def test_annotations_are_classified_as_annotations(
    sample_config: RegistreeConfig,
) -> None:
    grouped = ClassUsageAnalyzer(sample_config, "Widget").analyze()
    annotation_lines = {
        u.line_content.strip() for u in grouped.get("type_annotation", [])
    }
    assert "def load(widget_id: int) -> WidgetDB:" in annotation_lines
    assert "def to_domain(row: WidgetDB) -> Widget:" in annotation_lines


def test_inheritance_and_imports_are_classified(
    sample_config: RegistreeConfig,
) -> None:
    grouped = ClassUsageAnalyzer(sample_config, "BaseWidgetModel").analyze()
    assert len(grouped.get("inheritance", [])) == 1
    assert len(grouped.get("import", [])) == 1


def test_unknown_class_yields_nothing(sample_config: RegistreeConfig) -> None:
    grouped = ClassUsageAnalyzer(sample_config, "Nonexistent").analyze()
    assert grouped == {}
