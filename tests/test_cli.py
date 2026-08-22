"""CLI subcommand behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from registree.cli import main
from registree.config import RegistreeConfig


def test_gen_writes_registry(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["gen", "--root", str(sample_project)]) == 0
    out = capsys.readouterr().out
    assert "class registry generated" in out

    config = RegistreeConfig.discover(sample_project)
    assert config.registry_path.is_file()


def test_conflicts_exit_code_reflects_smells(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["gen", "--root", str(sample_project)]) == 0
    capsys.readouterr()

    # The fixture contains a same-layer Sprocket smell -> exit 1.
    assert main(["conflicts", "--root", str(sample_project)]) == 1
    out = capsys.readouterr().out
    assert "Sprocket" in out
    assert "Widget" in out  # reported as layered, not as a smell


def test_conflicts_without_registry_points_at_gen(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["conflicts", "--root", str(sample_project)]) == 1
    assert "registree gen" in capsys.readouterr().out


def test_usages_reports_alias(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["usages", "Widget", "--root", str(sample_project)]) == 0
    out = capsys.readouterr().out
    assert "imported under aliases: WidgetDB" in out


def test_custom_registry_path_threads_through_gen_and_conflicts(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    custom = "custom/registry.json"
    assert main(["gen", "--root", str(sample_project), "--output", custom]) == 0
    capsys.readouterr()
    assert (sample_project / custom).is_file()
    # Default location must NOT exist — the override was honored.
    assert not RegistreeConfig.discover(sample_project).registry_path.exists()

    assert (
        main(
            [
                "conflicts",
                "--root",
                str(sample_project),
                "--registry-path",
                custom,
            ]
        )
        == 1
    )
    assert "Sprocket" in capsys.readouterr().out


def test_conflicts_stats_prints_summary(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["gen", "--root", str(sample_project)]) == 0
    capsys.readouterr()
    assert main(["conflicts", "--root", str(sample_project), "--stats"]) == 0
    out = capsys.readouterr().out
    assert "Class Registry Statistics" in out
    assert "by class type:" in out
    assert "by layer:" in out


def test_usages_json_output_parses(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    assert main(["usages", "Widget", "--root", str(sample_project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["class_name"] == "Widget"
    assert payload["aliases"] == ["WidgetDB"]
    assert payload["total_usages"] > 0
