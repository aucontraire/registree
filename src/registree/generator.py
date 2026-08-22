"""Class registry generation.

Emits a JSON index of every class under the configured scan directories,
derived by AST parsing — no imports, no runtime side effects, deterministic.
Output is keyed by class NAME with a LIST of definitions, so duplicate names
surface instead of silently collapsing to whichever one happened to be
scanned last.

Anti-hallucination use case: agents and the interactive operator can answer
"does this class exist, what are its fields, where is it defined" from a
lookup instead of guessing. This tooling exists because a constructor call
written from memory used a wrong keyword name and omitted a required
argument — and a prose coding rule did not prevent it.

Design notes carried over from the original implementations this package was
extracted from:

  - TRANSITIVE class typing (see ``resolve_types``). Classifying on direct
    parents only mistypes every model routed through a project-defined
    intermediate base (``class Video(BaseProjectModel)`` where
    ``BaseProjectModel(BaseModel)``); one origin project funneled 33 models
    through a single such base.
  - ``**kwargs`` / positional-only parameters are captured. A signature that
    accepts ``**kwargs`` must be distinguishable from one that does not —
    that is exactly the fact a constructor checker needs before it dares
    complain about an unknown keyword.
  - ORM bases include ``DeclarativeBase`` so SQLAlchemy 2.0 ``Base`` classes
    type as ORM models rather than plain ``class``.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from registree import __version__
from registree.config import EXCLUDED_DIR_PARTS, RegistreeConfig

# ── classification ───────────────────────────────────────────────────────────

PYDANTIC_BASES = {"BaseModel", "BaseSettings"}
ORM_BASES = {"Base", "DeclarativeBase"}
ENUM_BASES = {"Enum", "StrEnum", "IntEnum", "IntFlag", "Flag"}


def base_short_name(rendered: str) -> str:
    """``pydantic.BaseModel`` -> ``BaseModel``; ``Generic[T]`` -> ``Generic``."""
    return rendered.split("[")[0].split(".")[-1].strip()


def _direct_type(parents: list[str], decorators: list[str]) -> str:
    """Classify from decorators and direct parents only.

    Precedence: dataclass decorator > parent taxonomy > plain ``class``.
    """
    for dec in decorators:
        if "dataclass" in dec.lower():
            return "dataclass"

    for parent in parents:
        short = base_short_name(parent)
        if short == "TypedDict":
            return "typed_dict"
        if short in PYDANTIC_BASES:
            return "pydantic_model"
        if short in ORM_BASES:
            return "orm_model"
        if short in ENUM_BASES:
            return "enum"
        if short == "Protocol":
            return "protocol"

    return "class"


# ── per-class metadata ───────────────────────────────────────────────────────


class ClassInfo(BaseModel):
    name: str
    type: str  # class|pydantic_model|orm_model|typed_dict|dataclass|enum|protocol
    parent_classes: list[str]
    file_path: str  # relative to the project root, posix separators
    line_number: int
    module: str
    file_type: str  # main | test | factory | script
    methods: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    docstring: str | None
    decorators: list[str]
    # Set during the transitive pass; records which ancestor supplied the type,
    # so a surprising classification can be traced instead of argued about.
    type_source: str | None = None


# ── AST visitor ──────────────────────────────────────────────────────────────


class ClassExtractor(ast.NodeVisitor):
    """Walks ClassDef nodes and emits one ClassInfo per class."""

    def __init__(
        self, source_path: Path, relative_path: str, module_name: str, file_type: str
    ) -> None:
        self.source_path = source_path
        self.relative_path = relative_path
        self.module_name = module_name
        self.file_type = file_type
        self.classes: list[ClassInfo] = []

    def extract(self) -> list[ClassInfo]:
        try:
            content = self.source_path.read_text(encoding="utf-8")
            self.visit(ast.parse(content))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            print(f"warn: could not parse {self.source_path}: {exc}", file=sys.stderr)
        return self.classes

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(self._build_class_info(node))
        self.generic_visit(node)

    def _build_class_info(self, node: ast.ClassDef) -> ClassInfo:
        parents = [self._render(base) for base in node.bases]
        decorators = [self._render(dec) for dec in node.decorator_list]
        return ClassInfo(
            name=node.name,
            type=_direct_type(parents, decorators),
            parent_classes=parents,
            file_path=self.relative_path,
            line_number=node.lineno,
            module=self.module_name,
            file_type=self.file_type,
            methods=self._methods(node),
            fields=self._fields(node),
            docstring=ast.get_docstring(node),
            decorators=decorators,
        )

    # ── members ──────────────────────────────────────────────────────────────

    def _methods(self, node: ast.ClassDef) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in node.body:
            if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            params = self._parameters(item)
            out.append(
                {
                    "name": item.name,
                    "signature": self._signature(item),
                    "parameters": params,
                    "return_type": (
                        self._render(item.returns) if item.returns else None
                    ),
                    "is_property": self._has_decorator(item, "property"),
                    "is_classmethod": self._has_decorator(item, "classmethod"),
                    "is_staticmethod": self._has_decorator(item, "staticmethod"),
                    "is_async": isinstance(item, ast.AsyncFunctionDef),
                    # Precomputed so a consumer never has to re-derive it: a
                    # signature that soaks up **kwargs cannot be checked for
                    # unknown keyword arguments.
                    "accepts_kwargs": any(p["kind"] == "kwargs" for p in params),
                    "line_number": item.lineno,
                }
            )
        return out

    def _fields(self, node: ast.ClassDef) -> list[dict[str, Any]]:
        """Annotated and assigned class-level attributes.

        Covers SQLAlchemy ``Mapped[T]`` columns and Pydantic field
        declarations. ``is_required`` is a syntactic judgement — "no default
        is written here" — not a semantic one; a Pydantic
        ``Field(default_factory=...)`` counts as having a default, while
        inherited defaults are invisible from one class body. Consumers must
        treat it as a hint, never as a hard rule.
        """
        out: list[dict[str, Any]] = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                info: dict[str, Any] = {
                    "name": item.target.id,
                    "type": self._render(item.annotation),
                    "default_value": self._render(item.value) if item.value else None,
                    "is_required": item.value is None,
                }
                if (
                    isinstance(item.value, ast.Call)
                    and isinstance(item.value.func, ast.Name)
                    and item.value.func.id == "Field"
                ):
                    desc = self._field_description(item.value)
                    if desc:
                        info["description"] = desc
                out.append(info)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        out.append(
                            {
                                "name": target.id,
                                "type": "Any",
                                "default_value": self._render(item.value),
                                "is_required": False,
                            }
                        )
        return out

    # ── signature helpers ────────────────────────────────────────────────────

    def _signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        parts = [self._param_str(p) for p in self._parameters(node)]
        return f"({', '.join(parts)})"

    @staticmethod
    def _param_str(p: dict[str, Any]) -> str:
        prefix = {"varargs": "*", "kwargs": "**"}.get(str(p["kind"]), "")
        s = f"{prefix}{p['name']}"
        if p["has_type_annotation"]:
            s += f": {p['type']}"
        if p["has_default"]:
            s += f" = {p['default_value']}"
        return s

    def _parameters(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> list[dict[str, Any]]:
        """Every parameter, including positional-only and ``**kwargs``.

        Both are needed: ``**kwargs`` decides whether unknown-keyword checking
        is even sound, and a silently dropped positional-only parameter would
        read as a missing argument.
        """
        params: list[dict[str, Any]] = []
        args = node.args

        # Defaults right-align across posonly + regular positional args.
        positional = list(args.posonlyargs) + list(args.args)
        num_defaults = len(args.defaults)
        offset = len(positional) - num_defaults

        for i, arg in enumerate(positional):
            di = i - offset
            params.append(
                self._param(
                    arg,
                    kind=(
                        "positional_only" if i < len(args.posonlyargs) else "positional"
                    ),
                    has_default=di >= 0,
                    default=args.defaults[di] if di >= 0 else None,
                    position=len(params),
                )
            )

        if args.vararg:
            params.append(
                self._param(
                    args.vararg,
                    kind="varargs",
                    has_default=False,
                    default=None,
                    position=len(params),
                )
            )

        kw_defaults = args.kw_defaults or []
        for i, arg in enumerate(args.kwonlyargs):
            has_default = i < len(kw_defaults) and kw_defaults[i] is not None
            params.append(
                self._param(
                    arg,
                    kind="keyword_only",
                    has_default=has_default,
                    default=kw_defaults[i] if has_default else None,
                    position=len(params),
                )
            )

        if args.kwarg:
            params.append(
                self._param(
                    args.kwarg,
                    kind="kwargs",
                    has_default=False,
                    default=None,
                    position=len(params),
                )
            )

        return params

    def _param(
        self,
        arg: ast.arg,
        *,
        kind: str,
        has_default: bool,
        default: ast.expr | None,
        position: int,
    ) -> dict[str, Any]:
        return {
            "name": arg.arg,
            "type": self._render(arg.annotation) if arg.annotation else "Any",
            "has_type_annotation": arg.annotation is not None,
            "kind": kind,
            "has_default": has_default,
            "default_value": self._render(default) if default is not None else None,
            "position": position,
        }

    def _has_decorator(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, name: str
    ) -> bool:
        for dec in node.decorator_list:
            rendered = self._render(dec)
            if rendered == name or rendered.endswith(f".{name}"):
                return True
        return False

    @staticmethod
    def _field_description(call: ast.Call) -> str | None:
        for kw in call.keywords:
            if (
                kw.arg == "description"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                return kw.value.value
        return None

    @staticmethod
    def _render(node: ast.AST | None) -> str:
        if node is None:
            return "None"
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001 — any unparse failure degrades to a dump
            return ast.dump(node)


# ── transitive typing ────────────────────────────────────────────────────────


def resolve_types(classes: list[ClassInfo]) -> int:
    """Propagate a type through project-defined base classes.

    Direct-parent classification types ``class Video(BaseProjectModel)`` as
    plain ``class`` even though ``BaseProjectModel(BaseModel)`` makes it a
    Pydantic model. This walks up the inheritance chain over classes defined
    *within the scan*, and stops at the first ancestor with a concrete type.
    Ambiguity is resolved by the first parent in MRO order, which matches how
    Python itself would pick. Cycles and unresolvable names simply leave the
    class as ``class``.

    Returns the number of classes whose type was upgraded.
    """
    by_name: dict[str, list[ClassInfo]] = defaultdict(list)
    for c in classes:
        by_name[c.name].append(c)

    upgraded = 0
    for cls in classes:
        if cls.type != "class":
            continue

        seen: set[str] = {cls.name}
        queue: deque[tuple[str, str]] = deque(
            (base_short_name(p), p) for p in cls.parent_classes
        )

        while queue:
            short, original = queue.popleft()
            if short in seen:
                continue
            seen.add(short)

            candidates = by_name.get(short)
            if not candidates:
                continue
            # A duplicated base name is itself ambiguous; take the first
            # definition but record which one, so it can be audited.
            ancestor = candidates[0]
            if ancestor.type != "class":
                cls.type = ancestor.type
                cls.type_source = (
                    f"{original} -> {ancestor.file_path}:{ancestor.line_number}"
                )
                upgraded += 1
                break
            queue.extend((base_short_name(p), p) for p in ancestor.parent_classes)

    return upgraded


# ── git SHA ──────────────────────────────────────────────────────────────────


def _git_version(root: Path, default: str = "unknown") -> str:
    """Return the short git SHA of ``root``, or ``default`` outside a repo."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return sha or default
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return default


# ── orchestration ────────────────────────────────────────────────────────────


def generate_registry(config: RegistreeConfig) -> dict[str, Any]:
    """Scan the configured directories and build the registry document."""
    all_classes: list[ClassInfo] = []
    for d in config.scan_dirs:
        all_classes.extend(_scan_directory(config, d))

    upgraded = resolve_types(all_classes)

    by_name: dict[str, list[ClassInfo]] = defaultdict(list)
    for c in all_classes:
        by_name[c.name].append(c)

    duplicates = sum(1 for v in by_name.values() if len(v) > 1)
    now = datetime.now(UTC).isoformat()

    return {
        "metadata": {
            "generated_at": now,
            "updated_at": now,
            "registree_version": __version__,
            "git_version": _git_version(config.root),
            "root": str(config.root),
            "total_classes": len(all_classes),
            "duplicates_found": duplicates,
            "types_resolved_transitively": upgraded,
            "scan_directories": list(config.scan_dirs),
            "root_packages": list(config.root_packages),
            "excluded_patterns": list(EXCLUDED_DIR_PARTS),
        },
        "classes": {
            name: [c.model_dump() for c in instances]
            for name, instances in sorted(by_name.items())
        },
    }


def write_registry(config: RegistreeConfig, registry: dict[str, Any]) -> None:
    config.registry_path.parent.mkdir(parents=True, exist_ok=True)
    config.registry_path.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_registry_document(config: RegistreeConfig) -> dict[str, Any]:
    """Load the raw registry JSON. Raises OSError/ValueError when unusable."""
    data = json.loads(config.registry_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        # ValueError, not TypeError: callers already catch ValueError for the
        # sibling failure (json.JSONDecodeError subclasses it).
        raise ValueError("registry root is not an object")  # noqa: TRY004
    return data


def _scan_directory(config: RegistreeConfig, directory: str) -> list[ClassInfo]:
    base = (config.root / directory).resolve()
    if not base.exists():
        print(f"warn: directory does not exist: {base}", file=sys.stderr)
        return []

    results: list[ClassInfo] = []
    for py in sorted(base.rglob("*.py")):
        if any(part in EXCLUDED_DIR_PARTS for part in py.parts):
            continue
        file_type = _file_type(py)
        if file_type == "test" and not config.include_tests:
            continue
        try:
            relative = py.relative_to(config.root).as_posix()
        except ValueError:
            relative = str(py)
        results.extend(
            ClassExtractor(py, relative, _module_name(py, base), file_type).extract()
        )
    return results


def _file_type(path: Path) -> str:
    s = path.as_posix().lower()
    if "/factories/" in s or "factory" in path.name:
        return "factory"
    if (
        "/tests/" in s
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    ):
        return "test"
    if path.name in {"__main__.py", "main.py", "cli.py"}:
        return "script"
    return "main"


def _module_name(path: Path, base: Path) -> str:
    try:
        rel = path.relative_to(base)
    except ValueError:
        return path.stem
    parts = list(rel.parts[:-1])
    if rel.stem != "__init__":
        parts.append(rel.stem)
    return ".".join(p for p in parts if p)
