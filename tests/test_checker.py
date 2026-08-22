"""Constructor-checking behavior against the generated fixture registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from registree.checker import Registry, check_source


@pytest.fixture
def registry(sample_registry_doc: dict[str, Any]) -> Registry:
    return Registry.from_document(sample_registry_doc)


def _check(
    source: str,
    registry: Registry,
    sample_project: Path,
    file_path: str | None = None,
) -> list[str]:
    return check_source(
        source,
        registry,
        file_path,
        root=sample_project,
        root_packages=("widgetlib",),
    )


def test_unknown_keyword_is_reported(registry: Registry, sample_project: Path) -> None:
    source = (
        "from widgetlib.gadgets import Gadget\n"
        "Gadget(name='x', flag=True, colour='red')\n"
    )
    findings = _check(source, registry, sample_project)
    assert len(findings) == 1
    assert "unknown keyword(s) ['colour']" in findings[0]
    assert "gadgets.py" in findings[0]


def test_missing_required_arguments_are_reported(
    registry: Registry, sample_project: Path
) -> None:
    source = "from widgetlib.gadgets import Gadget\nGadget(size=3)\n"
    findings = _check(source, registry, sample_project)
    assert len(findings) == 1
    assert "missing required argument(s) ['flag', 'name']" in findings[0]


def test_ambiguous_name_without_import_is_flagged_once(
    registry: Registry, sample_project: Path
) -> None:
    source = "Widget(name='x')\nWidget(name='y')\n"
    findings = _check(source, registry, sample_project)
    assert len(findings) == 1
    assert "defined 2 times" in findings[0]


def test_import_disambiguates_and_enables_field_checking(
    registry: Registry, sample_project: Path
) -> None:
    source = "from widgetlib.models.widget import Widget\nWidget()\n"
    findings = _check(source, registry, sample_project)
    # Not an ambiguity note — the import resolved it, and the resolved
    # Pydantic model is then actually checked.
    assert len(findings) == 1
    assert "missing required argument(s) ['name']" in findings[0]


def test_definition_in_the_edited_file_wins(
    registry: Registry, sample_project: Path
) -> None:
    file_path = str(sample_project / "src/widgetlib/models/widget.py")
    findings = _check("Widget()\n", registry, sample_project, file_path)
    assert len(findings) == 1
    assert "missing required argument(s) ['name']" in findings[0]


def test_calls_under_pytest_raises_are_exempt(
    registry: Registry, sample_project: Path
) -> None:
    source = (
        "import pytest\n"
        "from widgetlib.gadgets import Gadget\n"
        "with pytest.raises(TypeError):\n"
        "    Gadget()\n"
    )
    assert _check(source, registry, sample_project) == []


def test_kwargs_constructor_silences_all_checks(
    registry: Registry, sample_project: Path
) -> None:
    source = "from widgetlib.gadgets import Flexible\nFlexible(anything=1)\n"
    assert _check(source, registry, sample_project) == []


def test_unknown_class_is_silent(registry: Registry, sample_project: Path) -> None:
    assert _check("Nonexistent(x=1)\n", registry, sample_project) == []


def test_unparseable_source_is_silent(registry: Registry, sample_project: Path) -> None:
    assert _check("def broken(:\n", registry, sample_project) == []


def test_starred_arguments_are_never_flagged(
    registry: Registry, sample_project: Path
) -> None:
    source = "from widgetlib.gadgets import Gadget\n" "args = {}\n" "Gadget(**args)\n"
    assert _check(source, registry, sample_project) == []
