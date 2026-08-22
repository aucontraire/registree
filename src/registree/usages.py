"""Class usage analysis: every place a class name is used, classified.

  import          ``from x import Foo`` / ``import Foo`` / ``... as Bar``
  inheritance     ``class Bar(Foo):``
  instantiation   ``Foo(...)``
  type_annotation ``Foo`` inside any annotation, including ``list[Foo] | None``
  reference       any other bare-name occurrence
  documentation   textual hit in docs (regex only, opt-in)

Run it BEFORE a rename to enumerate every file and line that must change.

Import-alias tracking is the load-bearing feature. Projects that give an ORM
model and a domain model the same name disambiguate at the import site
(``from pkg.db.models import Video as VideoDB``); matching the bare class
name alone records the import and then misses every aliased usage — the
exact sites a rename must touch. Measured in the origin project on one such
class: 469 usages across 29 files, of which 78% were only reachable through
the alias.

KNOWN LIMITATIONS (verified against a word-boundary grep in the origin
project — every difference accounted for):

  * **Docstrings are invisible.** NumPy-style docstrings that name types are
    string content, not Name nodes, so AST cannot see them. A rename must
    grep docstrings separately. This is the one gap that can actually bite.
  * Comments are excluded — correct, but same caveat if a comment names the
    type.
  * Multi-line imports report the ``from`` line, not the line the alias sits
    on. Not a miss, just a different line number than grep gives.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from registree.config import EXCLUDED_DIR_PARTS, RegistreeConfig


class ClassUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_path: str
    line_number: int
    usage_type: str
    line_content: str
    # Local binding when the name was imported under an alias ("VideoDB"),
    # else None. Carried through so a rename can see which spelling to change.
    via_alias: str | None = None


class _UsageVisitor(ast.NodeVisitor):
    """Classifies every occurrence of ``class_name`` (and its local aliases).

    A pre-pass records (a) the id() of every node inside an annotation
    subtree and (b) any local alias bound to the class by an import.
    Specialized visitors claim Names that are imports, bases and call
    callees; ``visit_Name`` fires last and skips anything already claimed.
    """

    def __init__(self, class_name: str, file_path: str, lines: list[str]) -> None:
        self.class_name = class_name
        self.file_path = file_path
        self.lines = lines
        self.usages: list[ClassUsage] = []
        self._claimed: set[int] = set()
        self._annotation_node_ids: set[int] = set()
        # local binding -> alias name (None means the canonical spelling)
        self._local_names: dict[str, str | None] = {class_name: None}

    def run(self, tree: ast.AST) -> list[ClassUsage]:
        self._collect_aliases(tree)
        self._collect_annotation_nodes(tree)
        self.visit(tree)
        return self.usages

    # ── pre-passes ───────────────────────────────────────────────────────────

    def _collect_aliases(self, tree: ast.AST) -> None:
        """Record ``from ... import Foo as Bar`` bindings for the target."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name == self.class_name and alias.asname:
                    self._local_names[alias.asname] = alias.asname

    def _collect_annotation_nodes(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            annotations: list[ast.AST] = []
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.returns is not None:
                    annotations.append(node.returns)
                for arg in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ):
                    if arg.annotation is not None:
                        annotations.append(arg.annotation)
                if node.args.vararg and node.args.vararg.annotation:
                    annotations.append(node.args.vararg.annotation)
                if node.args.kwarg and node.args.kwarg.annotation:
                    annotations.append(node.args.kwarg.annotation)
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)

            for ann in annotations:
                for descendant in ast.walk(ann):
                    self._annotation_node_ids.add(id(descendant))

    # ── specialized visitors ─────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if self.class_name in (alias.name, alias.name.split(".")[-1]):
                self._record(node.lineno, "import", alias.asname)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == self.class_name:
                self._record(node.lineno, "import", alias.asname)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in self._local_names:
                self._record(node.lineno, "inheritance", self._local_names[base.id])
                self._claimed.add(id(base))
            elif isinstance(base, ast.Attribute) and base.attr == self.class_name:
                self._record(node.lineno, "inheritance", None)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self._local_names:
            self._record(node.lineno, "instantiation", self._local_names[node.func.id])
            self._claimed.add(id(node.func))
        elif isinstance(node.func, ast.Attribute) and node.func.attr == self.class_name:
            self._record(node.lineno, "instantiation", None)
        self.generic_visit(node)

    # ── fallback ─────────────────────────────────────────────────────────────

    def visit_Name(self, node: ast.Name) -> None:
        if node.id not in self._local_names or id(node) in self._claimed:
            return
        usage_type = (
            "type_annotation" if id(node) in self._annotation_node_ids else "reference"
        )
        self._record(node.lineno, usage_type, self._local_names[node.id])

    def _record(self, lineno: int, usage_type: str, via_alias: str | None) -> None:
        line = self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else ""
        self.usages.append(
            ClassUsage(
                file_path=self.file_path,
                line_number=lineno,
                usage_type=usage_type,
                line_content=line.rstrip(),
                via_alias=via_alias,
            )
        )


class ClassUsageAnalyzer:
    def __init__(
        self,
        config: RegistreeConfig,
        class_name: str,
        include_docs: bool = False,
    ) -> None:
        self.config = config
        self.class_name = class_name
        self.doc_dirs = ["docs"] if include_docs else []
        self.usages: list[ClassUsage] = []

    def analyze(self) -> dict[str, list[ClassUsage]]:
        for py in self._python_files():
            self._analyze_python(py)
        for doc in self._doc_files():
            self._analyze_doc(doc)

        grouped: dict[str, list[ClassUsage]] = defaultdict(list)
        seen: set[tuple[str, int, str]] = set()
        for u in self.usages:
            key = (u.file_path, u.line_number, u.usage_type)
            if key in seen:
                continue
            seen.add(key)
            grouped[u.usage_type].append(u)
        for rows in grouped.values():
            rows.sort(key=lambda u: (u.file_path, u.line_number))
        return dict(grouped)

    def _python_files(self) -> list[Path]:
        out: list[Path] = []
        for d in self.config.scan_dirs:
            base = self.config.root / d
            if not base.exists():
                print(f"warn: scan dir does not exist: {base}", file=sys.stderr)
                continue
            out.extend(
                p
                for p in sorted(base.rglob("*.py"))
                if not any(part in EXCLUDED_DIR_PARTS for part in p.parts)
            )
        return out

    def _doc_files(self) -> list[Path]:
        out: list[Path] = []
        for d in self.doc_dirs:
            base = self.config.root / d
            if not base.exists():
                continue
            for ext in ("*.md", "*.rst", "*.txt"):
                out.extend(
                    p
                    for p in sorted(base.rglob(ext))
                    if not any(part in EXCLUDED_DIR_PARTS for part in p.parts)
                )
        return out

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.config.root).as_posix()
        except ValueError:
            return str(path)

    def _analyze_python(self, path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except (UnicodeDecodeError, SyntaxError) as exc:
            print(f"warn: could not analyze {path}: {exc}", file=sys.stderr)
            return
        self.usages.extend(
            _UsageVisitor(
                self.class_name, self._relative(path), content.splitlines()
            ).run(tree)
        )

    def _analyze_doc(self, path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        for i, line in enumerate(content.splitlines(), start=1):
            if self.class_name in line:
                self.usages.append(
                    ClassUsage(
                        file_path=self._relative(path),
                        line_number=i,
                        usage_type="documentation",
                        line_content=line.rstrip(),
                        via_alias=None,
                    )
                )


ORDER = [
    "import",
    "inheritance",
    "instantiation",
    "type_annotation",
    "reference",
    "documentation",
]


def print_report(class_name: str, grouped: dict[str, list[ClassUsage]]) -> None:
    total = sum(len(v) for v in grouped.values())
    files = {u.file_path for rows in grouped.values() for u in rows}
    aliases = sorted(
        {u.via_alias for rows in grouped.values() for u in rows if u.via_alias}
    )

    print(f"\nCLASS USAGE: {class_name}")
    print("=" * 78)
    print(f"total usages:   {total}")
    print(f"files affected: {len(files)}")
    if aliases:
        print(f"imported under aliases: {', '.join(aliases)}")
        print("  (a rename must change these spellings too)")

    if total == 0:
        print(f"\nno usages of '{class_name}' found")
        return

    for usage_type in ORDER:
        rows = grouped.get(usage_type)
        if not rows:
            continue
        print(f"\n[{usage_type}] ({len(rows)})")
        print("-" * 60)
        for u in rows:
            tag = f"  via {u.via_alias}" if u.via_alias else ""
            print(f"  {u.file_path}:{u.line_number}{tag}")
            print(f"    {u.line_content.strip()}")


def to_json(class_name: str, grouped: dict[str, list[ClassUsage]]) -> str:
    return json.dumps(
        {
            "class_name": class_name,
            "total_usages": sum(len(v) for v in grouped.values()),
            "aliases": sorted(
                {u.via_alias for rows in grouped.values() for u in rows if u.via_alias}
            ),
            "files_affected": sorted(
                {u.file_path for rows in grouped.values() for u in rows}
            ),
            "usages_by_type": {
                k: [u.model_dump() for u in v] for k, v in grouped.items()
            },
        },
        indent=2,
        ensure_ascii=False,
    )
