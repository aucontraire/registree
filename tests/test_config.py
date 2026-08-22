"""Config discovery behavior."""

from __future__ import annotations

from pathlib import Path

from registree.config import REGISTRY_DIR_NAME, REGISTRY_FILE_NAME, RegistreeConfig


def test_discovers_src_layout_package(sample_project: Path) -> None:
    config = RegistreeConfig.discover(sample_project)
    assert config.scan_dirs == ("src/widgetlib",)
    assert config.root_packages == ("widgetlib",)
    assert config.registry_path == (
        sample_project / REGISTRY_DIR_NAME / REGISTRY_FILE_NAME
    )


def test_include_tests_appends_tests_dir(sample_project: Path) -> None:
    config = RegistreeConfig.discover(sample_project, include_tests=True)
    assert config.scan_dirs == ("src/widgetlib", "tests")


def test_explicit_scan_dirs_win(sample_project: Path) -> None:
    config = RegistreeConfig.discover(sample_project, scan_dirs=["src"])
    assert config.scan_dirs == ("src",)
    # src itself is not a package, so no package name is derived from it.
    assert config.root_packages == ()


def test_flat_layout_detects_top_level_package(tmp_path: Path) -> None:
    (tmp_path / "flatpkg").mkdir()
    (tmp_path / "flatpkg" / "__init__.py").write_text("", encoding="utf-8")
    config = RegistreeConfig.discover(tmp_path)
    assert config.scan_dirs == ("flatpkg",)
    assert config.root_packages == ("flatpkg",)


def test_bare_directory_falls_back_to_root(tmp_path: Path) -> None:
    (tmp_path / "loose.py").write_text("class Loose:\n    pass\n", encoding="utf-8")
    config = RegistreeConfig.discover(tmp_path)
    assert config.scan_dirs == (".",)


def test_relative_registry_path_is_anchored_to_root(sample_project: Path) -> None:
    config = RegistreeConfig.discover(
        sample_project, registry_path="custom/registry.json"
    )
    assert config.registry_path == sample_project / "custom" / "registry.json"
