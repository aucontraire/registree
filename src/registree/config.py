"""Project configuration and discovery.

Everything project-specific flows in through :class:`RegistreeConfig` — the
project root, which directories to scan, the package names used to normalize
imports, and where the registry JSON lives. Nothing else in the package may
hardcode a project path.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

# Directory names never worth scanning, matched against any path component.
EXCLUDED_DIR_PARTS: tuple[str, ...] = (
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".ruff_cache",
    ".registree",
    "node_modules",
    "site",
    "dist",
    "build",
    "migrations",  # generated (e.g. alembic versions), never hand-constructed
)

REGISTRY_DIR_NAME = ".registree"
REGISTRY_FILE_NAME = "registry.json"


class RegistreeConfig(BaseModel):
    """Resolved per-project settings.

    ``scan_dirs`` are package roots relative to ``root``: registry ``module``
    values are recorded relative to each scan dir, so scanning ``src/mypkg``
    yields modules like ``api.schemas`` while an absolute import reads
    ``mypkg.api.schemas``. ``root_packages`` carries the package names needed
    to line those two spellings up.
    """

    model_config = ConfigDict(frozen=True)

    root: Path
    scan_dirs: tuple[str, ...]
    root_packages: tuple[str, ...]
    include_tests: bool = False
    registry_path: Path

    @classmethod
    def discover(
        cls,
        root: Path | str,
        *,
        scan_dirs: list[str] | None = None,
        include_tests: bool = False,
        registry_path: Path | str | None = None,
    ) -> RegistreeConfig:
        """Build a config for ``root``, detecting scan dirs when not given.

        Detection order: packages under ``src/`` (the src layout), then
        top-level packages, then the root itself as a last resort. Explicit
        ``scan_dirs`` always win.
        """
        resolved_root = Path(root).resolve()

        if scan_dirs is None:
            scan_dirs = _detect_scan_dirs(resolved_root)
        if (
            include_tests
            and (resolved_root / "tests").is_dir()
            and "tests" not in scan_dirs
        ):
            scan_dirs = [*scan_dirs, "tests"]

        packages = tuple(
            Path(d).name
            for d in scan_dirs
            if (resolved_root / d / "__init__.py").is_file()
        )

        if registry_path is None:
            resolved_registry = resolved_root / REGISTRY_DIR_NAME / REGISTRY_FILE_NAME
        else:
            resolved_registry = Path(registry_path)
            if not resolved_registry.is_absolute():
                resolved_registry = resolved_root / resolved_registry

        return cls(
            root=resolved_root,
            scan_dirs=tuple(scan_dirs),
            root_packages=packages,
            include_tests=include_tests,
            registry_path=resolved_registry,
        )


def _detect_scan_dirs(root: Path) -> list[str]:
    src = root / "src"
    if src.is_dir():
        packages = sorted(
            f"src/{child.name}"
            for child in src.iterdir()
            if child.is_dir() and (child / "__init__.py").is_file()
        )
        return packages or ["src"]

    top_level = sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and child.name not in EXCLUDED_DIR_PARTS
        and not child.name.startswith(".")
        and (child / "__init__.py").is_file()
        and child.name != "tests"
    )
    return top_level or ["."]
