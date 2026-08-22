"""Registry generation behavior on the fixture project."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from registree.config import RegistreeConfig
from registree.generator import generate_registry, write_registry


def _single(doc: dict[str, Any], name: str) -> dict[str, Any]:
    entries = doc["classes"][name]
    assert len(entries) == 1, f"{name}: expected 1 definition, got {len(entries)}"
    entry: dict[str, Any] = entries[0]
    return entry


def test_names_map_to_lists_and_duplicates_are_counted(
    sample_registry_doc: dict[str, Any],
) -> None:
    classes = sample_registry_doc["classes"]
    assert len(classes["Widget"]) == 2
    assert len(classes["Sprocket"]) == 2
    assert sample_registry_doc["metadata"]["duplicates_found"] == 2


def test_transitive_typing_upgrades_through_project_base(
    sample_registry_doc: dict[str, Any],
) -> None:
    widgets = sample_registry_doc["classes"]["Widget"]
    pydantic = [w for w in widgets if w["type"] == "pydantic_model"]
    assert len(pydantic) == 1
    # The upgrade is traceable to the ancestor that supplied the type.
    assert pydantic[0]["type_source"] is not None
    assert "base.py" in pydantic[0]["type_source"]
    assert sample_registry_doc["metadata"]["types_resolved_transitively"] >= 1


def test_orm_model_classified_via_declarative_base(
    sample_registry_doc: dict[str, Any],
) -> None:
    widgets = sample_registry_doc["classes"]["Widget"]
    orm = [w for w in widgets if w["type"] == "orm_model"]
    assert len(orm) == 1
    assert orm[0]["file_path"] == "src/widgetlib/db/models.py"


def test_kwargs_and_required_fields_are_captured(
    sample_registry_doc: dict[str, Any],
) -> None:
    flexible = _single(sample_registry_doc, "Flexible")
    init = next(m for m in flexible["methods"] if m["name"] == "__init__")
    assert init["accepts_kwargs"] is True

    widget = next(
        w
        for w in sample_registry_doc["classes"]["Widget"]
        if w["type"] == "pydantic_model"
    )
    by_name = {f["name"]: f for f in widget["fields"]}
    assert by_name["name"]["is_required"] is True
    assert by_name["size"]["is_required"] is False


def test_modules_are_relative_to_scan_dir(
    sample_registry_doc: dict[str, Any],
) -> None:
    widget = next(
        w
        for w in sample_registry_doc["classes"]["Widget"]
        if w["type"] == "pydantic_model"
    )
    assert widget["module"] == "models.widget"
    assert widget["file_path"] == "src/widgetlib/models/widget.py"


def test_tests_dir_excluded_by_default(
    sample_project: Path, sample_registry_doc: dict[str, Any]
) -> None:
    assert "SampleCase" not in sample_registry_doc["classes"]

    with_tests = generate_registry(
        RegistreeConfig.discover(sample_project, include_tests=True)
    )
    assert "SampleCase" in with_tests["classes"]
    assert with_tests["classes"]["SampleCase"][0]["file_type"] == "test"


def test_write_creates_registry_file(
    sample_config: RegistreeConfig, sample_registry_doc: dict[str, Any]
) -> None:
    write_registry(sample_config, sample_registry_doc)
    assert sample_config.registry_path.is_file()
