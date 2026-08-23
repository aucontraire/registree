"""The version is declared twice, and the two must not drift.

``pyproject.toml`` sets the distribution version. ``registree.__version__`` is
what the MCP server reports to clients over the wire and what
``generate_registry()`` stamps into every registry cache as
``registree_version``. Bumping one and not the other publishes a release that
introduces itself to clients under the previous number and writes that number
into every cache it generates — a disagreement nothing else in the project
would catch.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import registree
from registree.config import RegistreeConfig
from registree.generator import generate_registry

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_version() -> str:
    # Asserted rather than skipped: a skip here would pass silently in exactly
    # the situation the test exists to catch.
    assert PYPROJECT.is_file(), f"pyproject.toml not found at {PYPROJECT}"
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    version = data["project"]["version"]
    assert isinstance(version, str)
    return version


def test_package_version_matches_pyproject() -> None:
    assert registree.__version__ == _declared_version(), (
        "registree.__version__ and pyproject.toml disagree. Bump both: the "
        "first is what clients and registry caches see, the second is what "
        "gets published."
    )


def test_registry_is_stamped_with_the_declared_version(
    sample_config: RegistreeConfig,
) -> None:
    """The stamp is the reason the drift matters, so pin it to the real source
    rather than to __version__ — otherwise both could be wrong together."""
    document = generate_registry(sample_config)
    assert document["metadata"]["registree_version"] == _declared_version()
