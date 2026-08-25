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


# ── self.<method>() checking ────────────────────────────────────────────────────


def _clients_path(sample_project: Path) -> str:
    return str(sample_project / "src/widgetlib/clients.py")


# The whole registered body of WidgetClient. The unknown-method check fires
# only when the snippet shows a class at least as complete as the registry
# knows it, so a "fires" test must present the full body, not a stub.
_WIDGET_CLIENT_BODY = (
    "class WidgetClient(BaseClient):\n"
    "    def __init__(self, api_key: str) -> None:\n"
    "        self.api_key = api_key\n"
    "    @property\n"
    "    def is_configured(self) -> bool:\n"
    "        return True\n"
    "    @classmethod\n"
    "    def from_env(cls) -> 'WidgetClient':\n"
    "        return cls(api_key='x')\n"
    "    @staticmethod\n"
    "    def normalize(value: str) -> str:\n"
    "        return value.strip()\n"
    "    async def refresh(self, **options: object) -> None:\n"
    "        return None\n"
    "    def close(self) -> None:\n"
    "        pass\n"
    "    def _fetch_batch(self, ids: list[str]) -> list[str]:\n"
    "        return []\n"
)


def test_unknown_self_method_is_reported(
    registry: Registry, sample_project: Path
) -> None:
    source = _WIDGET_CLIENT_BODY + (
        "    def run(self) -> None:\n"
        "        self.fetch_items([])\n"  # inherited, real — silent
        "        self.bogus()\n"  # unknown — flagged
    )
    findings = _check(source, registry, sample_project, _clients_path(sample_project))
    assert len(findings) == 1
    assert "`self.bogus()` is not a known method of `WidgetClient`" in findings[0]
    assert "fetch_items" in findings[0]  # sample of real methods is offered


def test_known_self_methods_are_silent(
    registry: Registry, sample_project: Path
) -> None:
    source = _WIDGET_CLIENT_BODY + (
        "    def run(self) -> None:\n"
        "        self.fetch_items([])\n"  # inherited instance method
        "        self.close()\n"  # overridden instance method
        "        self.from_env()\n"  # classmethod via self is legal
        "        self.normalize('x')\n"  # staticmethod via self is legal
        "        self.refresh()\n"  # **kwargs method
    )
    assert _check(source, registry, sample_project, _clients_path(sample_project)) == []


def test_property_called_as_method_is_reported(
    registry: Registry, sample_project: Path
) -> None:
    # Property-call is positive evidence, so it does not need the whole body.
    source = (
        "class WidgetClient(BaseClient):\n"
        "    def run(self) -> None:\n"
        "        self.is_configured()\n"  # is_configured is a @property
    )
    findings = _check(source, registry, sample_project, _clients_path(sample_project))
    assert len(findings) == 1
    assert "`self.is_configured()` calls `is_configured`, a property" in findings[0]


def test_instance_attribute_callable_is_silent(
    registry: Registry, sample_project: Path
) -> None:
    # A constructor-injected callable stored on self is a known attribute, not
    # an unknown method — the false positive found on real code.
    path = str(sample_project / "src/widgetlib/gadgets.py")
    source = (
        "class Gadget:\n"
        "    def __init__(self, name: str, size: int = 2, *, flag: bool) -> None:\n"
        "        self._factory = flag\n"
        "        self.name = name\n"
        "    def run(self) -> None:\n"
        "        self._factory()\n"
    )
    assert _check(source, registry, sample_project, path) == []


def test_fragment_edit_does_not_flag_unknown(
    registry: Registry, sample_project: Path
) -> None:
    # Only run() is shown; __init__ and its instance attributes are not, so the
    # snippet is too partial to claim any name is unknown.
    source = (
        "class WidgetClient(BaseClient):\n"
        "    def run(self) -> None:\n"
        "        self.some_injected_thing()\n"
    )
    assert _check(source, registry, sample_project, _clients_path(sample_project)) == []


def test_self_method_on_subclassed_base_is_silent(
    registry: Registry, sample_project: Path
) -> None:
    # BaseClient is subclassed by WidgetClient, so `self` may be a WidgetClient
    # that defines the name — the template-method pattern. Stay silent. The
    # full BaseClient body is shown so the check reaches the subclass guard
    # rather than stopping at the whole-body guard.
    source = (
        "class BaseClient:\n"
        "    def fetch_items(self, ids: list[str]) -> list[str]:\n"
        "        return []\n"
        "    def close(self) -> None:\n"
        "        pass\n"
        "    def run(self) -> None:\n"
        "        self.only_defined_in_subclass()\n"
    )
    assert _check(source, registry, sample_project, _clients_path(sample_project)) == []


def test_self_method_on_incomplete_class_is_silent(
    registry: Registry, sample_project: Path
) -> None:
    # Widget's ancestry runs into BaseModel (inert), so its method list is not
    # whole — absence is not evidence of absence.
    path = str(sample_project / "src/widgetlib/models/widget.py")
    source = (
        "from widgetlib.base import BaseWidgetModel\n"
        "class Widget(BaseWidgetModel):\n"
        "    name: str\n"
        "    def run(self) -> None:\n"
        "        self.no_such_method()\n"
    )
    assert _check(source, registry, sample_project, path) == []


def test_locally_defined_self_method_is_not_unknown(
    registry: Registry, sample_project: Path
) -> None:
    # `helper` is drafted right here; a stale registry must not make it unknown.
    source = (
        "class WidgetClient(BaseClient):\n"
        "    def helper(self) -> None:\n"
        "        pass\n"
        "    def run(self) -> None:\n"
        "        self.helper()\n"
    )
    assert _check(source, registry, sample_project, _clients_path(sample_project)) == []


def test_local_property_called_as_method_is_reported(
    registry: Registry, sample_project: Path
) -> None:
    # The class is unknown to the registry, but the snippet itself declares the
    # property — positive evidence is enough.
    source = (
        "class Thing:\n"
        "    @property\n"
        "    def ready(self) -> bool:\n"
        "        return True\n"
        "    def run(self) -> None:\n"
        "        self.ready()\n"
    )
    findings = _check(source, registry, sample_project)
    assert len(findings) == 1
    assert "`self.ready()` calls `ready`, a property on `Thing`" in findings[0]


def test_property_returning_callable_called_is_silent(
    registry: Registry, sample_project: Path
) -> None:
    source = (
        "from typing import Callable\n"
        "class Thing:\n"
        "    @property\n"
        "    def handler(self) -> Callable:\n"
        "        return self._f\n"
        "    def run(self) -> None:\n"
        "        self.handler()\n"
    )
    assert _check(source, registry, sample_project) == []


def test_self_method_in_nested_function_is_silent(
    registry: Registry, sample_project: Path
) -> None:
    # Inside a nested function `self` is a closure variable, not a guaranteed
    # WidgetClient instance — association ends, so no claim is made.
    source = (
        "class WidgetClient(BaseClient):\n"
        "    def run(self) -> None:\n"
        "        def helper() -> None:\n"
        "            self.bogus()\n"
        "        helper()\n"
    )
    assert _check(source, registry, sample_project, _clients_path(sample_project)) == []


def test_dunder_self_call_is_silent(registry: Registry, sample_project: Path) -> None:
    source = (
        "class WidgetClient(BaseClient):\n"
        "    def run(self) -> None:\n"
        "        self.__wrapped__()\n"
    )
    assert _check(source, registry, sample_project, _clients_path(sample_project)) == []


def test_unknown_class_self_call_is_silent(
    registry: Registry, sample_project: Path
) -> None:
    # No registry entry, no local definition -> inherited methods unknowable.
    source = (
        "class Freestanding:\n"
        "    def run(self) -> None:\n"
        "        self.mystery()\n"
    )
    assert _check(source, registry, sample_project) == []
