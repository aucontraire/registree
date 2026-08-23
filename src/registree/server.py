"""registree MCP server.

Exposes the class registry as MCP tools so coding agents can verify a
constructor before writing the call instead of guessing kwargs and debugging
the ``TypeError`` later.

Design principle carried through every tool: a class name maps to a LIST of
definitions. An ambiguous name returns every definition, never a silent
first match. Honesty over confidence: when a constructor is open-ended
(``**kwargs``, Pydantic ``extra``/``alias``) or an ancestor is unknowable,
the response says "unknowable" rather than presenting a partial answer as
complete.

The registry itself is self-maintaining: generated on first use and
regenerated whenever a scanned source file is newer than the registry, so a
client never has to run ``registree gen`` by hand.
"""

from __future__ import annotations

import ast
import difflib
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from registree import __version__
from registree.checker import Registry, analyse
from registree.config import EXCLUDED_DIR_PARTS, RegistreeConfig
from registree.conflicts import find_conflicts, layer
from registree.generator import (
    generate_registry,
    load_registry_document,
    write_registry,
)
from registree.usages import ORDER, ClassUsageAnalyzer

mcp = MCPServer(
    name="registree",
    version=__version__,
    instructions=(
        "Query a static class registry before writing code that instantiates "
        "existing classes. Class names map to LISTS of definitions: an "
        "ambiguous name returns every definition, never a silent first "
        "match. Use get_signature before writing a constructor call OR a "
        "method call — it reports the class's methods, so a guessed method "
        "name can be checked instead of assumed. "
        "verify_snippet after drafting code, get_usages before renaming, "
        "and list_duplicates to see which names need an import to "
        "disambiguate."
    ),
)


# ── registry provider ────────────────────────────────────────────────────────

STALENESS_CHECK_INTERVAL_SECONDS = 5.0


class RegistryProvider:
    """Serves the registry document, regenerating it when sources change.

    Staleness is judged by mtime: any scanned ``.py`` newer than the registry
    file triggers regeneration. The sweep itself is debounced so a burst of
    tool calls does not re-stat the whole tree every time.
    """

    def __init__(self, config: RegistreeConfig) -> None:
        self.config = config
        self._doc: dict[str, Any] | None = None
        self._loaded_mtime: float | None = None
        self._last_check = 0.0

    def document(self) -> dict[str, Any]:
        now = time.monotonic()
        if (
            self._doc is not None
            and now - self._last_check < STALENESS_CHECK_INTERVAL_SECONDS
        ):
            return self._doc
        self._last_check = now

        path = self.config.registry_path
        if not path.is_file() or self._newest_source_mtime() > path.stat().st_mtime:
            doc = generate_registry(self.config)
            write_registry(self.config, doc)
            self._doc = doc
            self._loaded_mtime = path.stat().st_mtime
            return doc

        mtime = path.stat().st_mtime
        if self._doc is None or mtime != self._loaded_mtime:
            self._doc = load_registry_document(self.config)
            self._loaded_mtime = mtime
        return self._doc

    def registry(self) -> Registry:
        return Registry.from_document(self.document())

    def _newest_source_mtime(self) -> float:
        newest = 0.0
        for d in self.config.scan_dirs:
            base = self.config.root / d
            if not base.exists():
                continue
            for py in base.rglob("*.py"):
                if any(part in EXCLUDED_DIR_PARTS for part in py.parts):
                    continue
                try:
                    newest = max(newest, py.stat().st_mtime)
                except OSError:
                    continue
        return newest


_provider: RegistryProvider | None = None


def configure(
    root: Path | None = None, registry_path: Path | None = None
) -> RegistryProvider:
    """Point the server at a project. Called by main(); tests call it too."""
    global _provider
    _provider = RegistryProvider(
        RegistreeConfig.discover(root or Path.cwd(), registry_path=registry_path)
    )
    return _provider


def _require_provider() -> RegistryProvider:
    if _provider is None:
        raise RuntimeError("server not configured; call configure(root) first")
    return _provider


# ── response models ──────────────────────────────────────────────────────────


class ServerInfo(BaseModel):
    name: str
    version: str
    status: str
    root: str
    registry_classes: int
    registry_generated_at: str | None


class FieldSummary(BaseModel):
    name: str
    type: str
    required: bool
    default: str | None
    description: str | None


class MethodSummary(BaseModel):
    name: str
    signature: str | None
    # instance | property | classmethod | staticmethod
    kind: str
    is_async: bool
    # True when the method soaks up **kwargs, so its keywords are unknowable.
    accepts_kwargs: bool
    returns: str | None
    # Which class in the MRO supplied it, so an inherited method is traceable
    # rather than mistaken for one defined here.
    defined_in: str


class Definition(BaseModel):
    file_path: str
    line_number: int
    type: str
    module: str
    file_type: str
    parent_classes: list[str]
    summary: str | None
    init_signature: str | None
    fields: list[FieldSummary]
    # Callable surface, inherited methods included, ``__init__`` excluded (it
    # is init_signature). A listed method exists; when methods_complete is
    # False an unlisted one may exist too — absence is not evidence.
    methods: list[MethodSummary]
    methods_complete: bool
    # None means "unknowable": an open-ended constructor (**kwargs, Pydantic
    # extra=/alias=) or an ancestor outside the registry. A None here is a
    # statement of honest uncertainty, not an empty list.
    required_arguments: list[str] | None
    accepted_keywords: list[str] | None


class SignatureResult(BaseModel):
    class_name: str
    found: bool
    ambiguous: bool
    definitions: list[Definition]
    note: str | None


class SearchHit(BaseModel):
    name: str
    definition_count: int
    types: list[str]
    locations: list[str]


class SearchResult(BaseModel):
    query: str
    total_matches: int
    truncated: bool
    hits: list[SearchHit]


class DuplicateInstance(BaseModel):
    file_path: str
    line_number: int
    type: str
    layer: str
    summary: str | None


class Duplicate(BaseModel):
    name: str
    kind: str  # "layered" | "smell"
    same_layer: bool
    instances: list[DuplicateInstance]
    suggestions: dict[str, list[str]]


class DuplicatesResult(BaseModel):
    total_duplicate_names: int
    smells: list[Duplicate]
    layered: list[Duplicate]
    alias_convention: str | None


class VerifyResult(BaseModel):
    ok: bool
    error: str | None
    findings: list[str]
    note: str


class UsageRow(BaseModel):
    file_path: str
    line_number: int
    usage_type: str
    line_content: str
    via_alias: str | None


class UsagesResult(BaseModel):
    class_name: str
    total: int
    files_affected: int
    aliases: list[str]
    truncated: bool
    usages: list[UsageRow]
    note: str


# ── helpers ──────────────────────────────────────────────────────────────────


def _first_line(docstring: str | None) -> str | None:
    if not docstring:
        return None
    return docstring.strip().split("\n", 1)[0].strip() or None


def _method_kind(m: dict[str, Any]) -> str:
    """Descriptor kind — how the method is reached, not just its name.

    A property is read (``obj.ready``) and a classmethod is called on the
    class; getting that wrong is its own failure, distinct from getting the
    name wrong, and the generator already records both.
    """
    if m.get("is_property"):
        return "property"
    if m.get("is_classmethod"):
        return "classmethod"
    if m.get("is_staticmethod"):
        return "staticmethod"
    return "instance"


def _build_definition(entry: dict[str, Any], reg: Registry) -> Definition:
    init_signature: str | None = None
    for m in entry.get("methods") or []:
        if m.get("name") == "__init__":
            init_signature = m.get("signature")
            break

    spec = reg.required_params(entry)
    required = sorted(spec[1]) if spec is not None else None
    accepted = reg.accepted_keywords(entry)
    methods, methods_complete = reg.methods(entry)

    return Definition(
        file_path=str(entry.get("file_path", "")),
        line_number=int(entry.get("line_number", 0)),
        type=str(entry.get("type", "class")),
        module=str(entry.get("module", "")),
        file_type=str(entry.get("file_type", "main")),
        parent_classes=[str(p) for p in entry.get("parent_classes") or []],
        summary=_first_line(entry.get("docstring")),
        init_signature=init_signature,
        fields=[
            FieldSummary(
                name=str(f.get("name", "")),
                type=str(f.get("type", "Any")),
                required=bool(f.get("is_required")),
                default=f.get("default_value"),
                description=f.get("description"),
            )
            for f in entry.get("fields") or []
        ],
        required_arguments=required,
        accepted_keywords=sorted(accepted) if accepted is not None else None,
        methods=[
            MethodSummary(
                name=str(m.get("name", "")),
                signature=m.get("signature"),
                kind=_method_kind(m),
                is_async=bool(m.get("is_async")),
                accepts_kwargs=bool(m.get("accepts_kwargs")),
                returns=m.get("return_type"),
                defined_in=str(m.get("defined_in", "")),
            )
            for m in methods
        ],
        methods_complete=methods_complete,
    )


def _close_matches(name: str, doc: dict[str, Any], limit: int = 5) -> list[str]:
    names = list(doc.get("classes") or {})
    lowered = name.lower()
    substrings = [n for n in names if lowered in n.lower()]
    fuzzy = difflib.get_close_matches(name, names, n=limit, cutoff=0.6)
    seen: set[str] = set()
    out: list[str] = []
    for candidate in substrings + fuzzy:
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out[:limit]


# ── tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
def server_info() -> ServerInfo:
    """Report server status, the project it serves, and registry size."""
    provider = _require_provider()
    doc = provider.document()
    meta = doc.get("metadata") or {}
    return ServerInfo(
        name="registree",
        version=__version__,
        status="ok",
        root=str(provider.config.root),
        registry_classes=int(meta.get("total_classes", 0)),
        registry_generated_at=meta.get("generated_at"),
    )


@mcp.tool()
def get_signature(class_name: str) -> SignatureResult:
    """Constructor contract AND callable surface for a class — EVERY
    definition if the name is duplicated. Call this before writing an
    instantiation or a method call: it reports the required arguments, the
    accepted keywords, honest ``null`` when a constructor is open-ended or
    unknowable, and the class's ``methods`` (inherited included, each with its
    kind and defining class) so a method name can be verified rather than
    guessed. When ``methods_complete`` is false an ancestor was unresolvable,
    so a listed method exists but an unlisted one still might."""
    provider = _require_provider()
    doc = provider.document()
    reg = provider.registry()
    entries = reg.get(class_name)

    if not entries:
        similar = _close_matches(class_name, doc)
        return SignatureResult(
            class_name=class_name,
            found=False,
            ambiguous=False,
            definitions=[],
            note=f"'{class_name}' is not in the registry."
            + (f" Similar names: {', '.join(similar)}." if similar else ""),
        )

    definitions = [_build_definition(e, reg) for e in entries]
    note: str | None = None
    if len(entries) > 1:
        note = (
            f"'{class_name}' has {len(entries)} definitions with different "
            "contracts — import the one you mean explicitly."
        )
    return SignatureResult(
        class_name=class_name,
        found=True,
        ambiguous=len(entries) > 1,
        definitions=definitions,
        note=note,
    )


@mcp.tool()
def search_classes(query: str, limit: int = 20) -> SearchResult:
    """Find classes by name fragment (case-insensitive), falling back to
    fuzzy matching when nothing contains the fragment. Use this when unsure
    of the exact class name."""
    provider = _require_provider()
    doc = provider.document()
    classes: dict[str, list[dict[str, Any]]] = doc.get("classes") or {}

    lowered = query.lower()
    starts = sorted(n for n in classes if n.lower().startswith(lowered))
    contains = sorted(
        n for n in classes if lowered in n.lower() and not n.lower().startswith(lowered)
    )
    matches = starts + contains
    if not matches:
        matches = difflib.get_close_matches(query, list(classes), n=limit, cutoff=0.6)

    hits = [
        SearchHit(
            name=name,
            definition_count=len(classes[name]),
            types=sorted({str(e.get("type", "class")) for e in classes[name]}),
            locations=[
                f"{e.get('file_path')}:{e.get('line_number')}" for e in classes[name]
            ],
        )
        for name in matches[:limit]
    ]
    return SearchResult(
        query=query,
        total_matches=len(matches),
        truncated=len(matches) > limit,
        hits=hits,
    )


@mcp.tool()
def list_duplicates(smells_only: bool = False) -> DuplicatesResult:
    """Duplicate class names, split into accepted layered pairs (ORM model +
    domain model sharing a name across layers) and genuine smells. Names
    listed here always need an explicit import to disambiguate."""
    provider = _require_provider()
    report = find_conflicts(provider.document())

    def _slim(conflicts: list[Any]) -> list[Duplicate]:
        return [
            Duplicate(
                name=c.name,
                kind=c.kind,
                same_layer=c.same_layer,
                instances=[
                    DuplicateInstance(
                        file_path=str(i.get("file_path", "")),
                        line_number=int(i.get("line_number", 0)),
                        type=str(i.get("type", "class")),
                        layer=layer(str(i.get("file_path", ""))),
                        summary=_first_line(i.get("docstring")),
                    )
                    for i in c.instances
                ],
                suggestions=c.suggestions,
            )
            for c in conflicts
        ]

    return DuplicatesResult(
        total_duplicate_names=report.total_duplicate_names,
        smells=_slim(report.smells),
        layered=[] if smells_only else _slim(report.layered),
        alias_convention=report.alias_convention,
    )


@mcp.tool()
def verify_snippet(code: str, file_path: str | None = None) -> VerifyResult:
    """Check a draft snippet's constructor calls against the registry before
    writing it to a file. Pass the intended file_path when known — imports in
    the snippet and the target location both help disambiguate duplicated
    names."""
    provider = _require_provider()
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as exc:
        return VerifyResult(
            ok=False,
            error=f"snippet does not parse: {exc}",
            findings=[],
            note="Fix the syntax first; constructor checks need a parseable snippet.",
        )

    findings = analyse(
        tree,
        provider.registry(),
        file_path,
        root=provider.config.root,
        root_packages=provider.config.root_packages,
    )
    return VerifyResult(
        ok=not findings,
        error=None,
        findings=findings,
        note=(
            "Checks are advisory. Classes outside the registry and "
            "open-ended constructors (**kwargs, Pydantic extra=/alias=) are "
            "not checkable, so an empty findings list is not a proof of "
            "correctness."
        ),
    )


@mcp.tool()
def get_usages(
    class_name: str, include_tests: bool = False, limit: int = 200
) -> UsagesResult:
    """Every place a class name is used — imports, inheritance,
    instantiations, annotations, references — including usages through import
    aliases (``from pkg.db.models import X as XDB``). Run this BEFORE a
    rename to enumerate what must change."""
    provider = _require_provider()
    config = RegistreeConfig.discover(provider.config.root, include_tests=include_tests)
    grouped = ClassUsageAnalyzer(config, class_name).analyze()

    rows: list[UsageRow] = []
    for usage_type in ORDER:
        for u in grouped.get(usage_type, []):
            rows.append(
                UsageRow(
                    file_path=u.file_path,
                    line_number=u.line_number,
                    usage_type=u.usage_type,
                    line_content=u.line_content,
                    via_alias=u.via_alias,
                )
            )

    total = len(rows)
    return UsagesResult(
        class_name=class_name,
        total=total,
        files_affected=len({r.file_path for r in rows}),
        aliases=sorted({r.via_alias for r in rows if r.via_alias}),
        truncated=total > limit,
        usages=rows[:limit],
        note=(
            "AST-based: docstrings and comments that mention the name are "
            "invisible here — grep them separately before a rename."
        ),
    )


# ── entry point ──────────────────────────────────────────────────────────────


def main(root: Path | None = None, registry_path: Path | None = None) -> None:
    """Entry point for the ``registree`` console script (stdio transport)."""
    configure(root, registry_path)
    mcp.run(transport="stdio")
