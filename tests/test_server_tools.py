"""MCP tool behavior, called directly against the fixture project.

The ``@mcp.tool()`` decorator returns the function unchanged, so these tests
exercise the exact callables the server dispatches to — transport-level
wiring is covered by the subprocess handshake test.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from registree import server
from registree.server import (
    RegistryProvider,
    configure,
    get_signature,
    get_usages,
    list_duplicates,
    search_classes,
    server_info,
    verify_snippet,
)


@pytest.fixture
def provider(sample_project: Path) -> RegistryProvider:
    return configure(sample_project)


def test_server_info_reports_registry(provider: RegistryProvider) -> None:
    info = server_info()
    assert info.status == "ok"
    assert info.root == str(provider.config.root)
    assert info.registry_classes > 0
    assert info.registry_generated_at is not None


def test_get_signature_returns_every_definition(
    provider: RegistryProvider,
) -> None:
    result = get_signature("Widget")
    assert result.found and result.ambiguous
    assert len(result.definitions) == 2
    assert result.note is not None and "2 definitions" in result.note

    pydantic = next(d for d in result.definitions if d.type == "pydantic_model")
    assert pydantic.required_arguments == ["name"]
    assert pydantic.accepted_keywords == ["name", "size"]


def test_get_signature_reports_init_contract(provider: RegistryProvider) -> None:
    result = get_signature("Gadget")
    assert result.found and not result.ambiguous
    definition = result.definitions[0]
    assert definition.required_arguments == ["flag", "name"]
    assert definition.accepted_keywords == ["flag", "name", "size"]
    assert definition.init_signature is not None
    assert "name: str" in definition.init_signature


def test_get_signature_says_unknowable_not_empty(
    provider: RegistryProvider,
) -> None:
    result = get_signature("Flexible")
    definition = result.definitions[0]
    # **kwargs makes the contract open-ended: None, never [].
    assert definition.accepted_keywords is None
    assert definition.required_arguments is None


def test_get_signature_not_found_suggests_similar(
    provider: RegistryProvider,
) -> None:
    result = get_signature("Widgett")
    assert not result.found
    assert result.definitions == []
    assert result.note is not None and "Widget" in result.note


def test_search_classes_ranks_prefix_before_substring(
    provider: RegistryProvider,
) -> None:
    result = search_classes("wid")
    names = [h.name for h in result.hits]
    assert names[0] == "Widget"
    assert "BaseWidgetModel" in names
    widget = result.hits[0]
    assert widget.definition_count == 2
    assert len(widget.locations) == 2


def test_search_classes_falls_back_to_fuzzy(provider: RegistryProvider) -> None:
    result = search_classes("Sprocket")
    assert [h.name for h in result.hits] == ["Sprocket"]
    fuzzy = search_classes("Sproket")
    assert [h.name for h in fuzzy.hits] == ["Sprocket"]


def test_list_duplicates_splits_layered_from_smells(
    provider: RegistryProvider,
) -> None:
    result = list_duplicates()
    assert {d.name for d in result.layered} == {"Widget"}
    assert {d.name for d in result.smells} == {"Sprocket"}
    assert result.smells[0].same_layer is True
    assert result.smells[0].suggestions
    assert result.layered[0].suggestions == {}

    smells_only = list_duplicates(smells_only=True)
    assert smells_only.layered == []
    assert {d.name for d in smells_only.smells} == {"Sprocket"}


def test_verify_snippet_flags_bad_call(provider: RegistryProvider) -> None:
    result = verify_snippet(
        "from widgetlib.gadgets import Gadget\n"
        "Gadget(name='x', flag=True, colour='red')\n"
    )
    assert result.ok is False
    assert result.error is None
    assert len(result.findings) == 1
    assert "colour" in result.findings[0]


def test_verify_snippet_reports_syntax_errors_instead_of_silence(
    provider: RegistryProvider,
) -> None:
    result = verify_snippet("def broken(:\n")
    assert result.ok is False
    assert result.error is not None and "does not parse" in result.error
    assert result.findings == []


def test_verify_snippet_clean_code_is_ok_with_honest_note(
    provider: RegistryProvider,
) -> None:
    result = verify_snippet(
        "from widgetlib.gadgets import Gadget\nGadget(name='x', flag=True)\n"
    )
    assert result.ok is True
    assert "advisory" in result.note


def test_get_usages_tracks_aliases(provider: RegistryProvider) -> None:
    result = get_usages("Widget")
    assert result.aliases == ["WidgetDB"]
    assert result.total == len(result.usages)
    assert result.truncated is False
    instantiations = [u for u in result.usages if u.usage_type == "instantiation"]
    assert any(u.via_alias == "WidgetDB" for u in instantiations)
    assert "grep" in result.note


def test_registry_regenerates_when_sources_change(
    provider: RegistryProvider, sample_project: Path
) -> None:
    assert not get_signature("Doohickey").found

    new_file = sample_project / "src/widgetlib/doohickey.py"
    new_file.write_text(
        "class Doohickey:\n"
        "    def __init__(self, label: str) -> None:\n"
        "        self.label = label\n",
        encoding="utf-8",
    )
    # Defeat the staleness-check debounce and make the mtime unambiguous.
    provider._last_check = 0.0
    future = time.time() + 60
    import os

    os.utime(new_file, (future, future))

    result = get_signature("Doohickey")
    assert result.found
    assert result.definitions[0].required_arguments == ["label"]


def test_unconfigured_server_raises_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_provider", None)
    with pytest.raises(RuntimeError, match="not configured"):
        server_info()


def test_configure_honors_custom_registry_path(sample_project: Path) -> None:
    custom = sample_project / "elsewhere" / "reg.json"
    provider = configure(sample_project, registry_path=custom)
    assert provider.config.registry_path == custom
    assert get_signature("Gadget").found
    assert custom.is_file()


def test_second_provider_loads_registry_from_disk(
    provider: RegistryProvider, sample_project: Path
) -> None:
    first_doc = provider.document()

    fresh = RegistryProvider(provider.config)
    loaded = fresh.document()
    assert loaded["classes"].keys() == first_doc["classes"].keys()
    # Cached in memory: a second call inside the debounce window is a no-op.
    assert fresh.document() is loaded
