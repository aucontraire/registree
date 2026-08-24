"""Constructor and method-call checking against the class registry.

Inspects Python source for:

  1. **Unknown keyword argument** — ``Foo(bar=...)`` where ``bar`` is not a
     field of ``Foo`` nor a parameter of its ``__init__``, anywhere up its
     ancestor chain.
  2. **Missing required argument** — ``Foo()`` when the constructor demands
     arguments the call does not supply.
  3. **Ambiguous class name** — a call to a name that is defined multiple
     times with differing fields, and no import in the file disambiguates.
  4. **Unknown method on ``self``** — ``self.get_items()`` inside a class
     whose registry entry (with a whole method list) has no such method and
     no field of that name. The receiver's type is the enclosing class, the
     one case where it is known without inference.
  5. **Property invoked as a method** — ``self.ready()`` where ``ready`` is a
     ``@property``; the parentheses call the property's *result*, which is a
     bug unless that result is itself callable.

Silence is the default. Every uncertainty resolves to "say nothing":

  * class not in the registry -> silent
  * class name is duplicated AND kwargs would need checking -> only the
    ambiguity note, never a field complaint
  * any ancestor is unknown and not a known-inert external base -> silent,
    because its fields are unknowable from here
  * ``__init__`` accepts ``**kwargs`` anywhere up the chain -> silent
  * Pydantic ``extra="allow"`` or any ``alias=`` in a field -> silent

A wrong claim is far more costly than a missed one: it trains the reader to
ignore the checker.

The :class:`Registry` works on the raw registry dicts on purpose — this is
the hot path (a hook budget of single-digit milliseconds), and validating
2+ MB of JSON into models on every invocation would blow it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

# External bases that contribute no user-defined constructor keywords. Meeting
# one of these while walking ancestors is safe; meeting anything else unknown
# is not, and forces silence.
INERT_BASES = {
    "BaseModel",
    "BaseSettings",
    "DeclarativeBase",
    "Generic",
    "Protocol",
    "ABC",
    "ABCMeta",
    "object",
    "TypedDict",
    "Enum",
    "StrEnum",
    "IntEnum",
    "IntFlag",
    "Flag",
    "Exception",
    "BaseException",
    "ValueError",
    "TypeError",
    "RuntimeError",
    "NamedTuple",
}

MAX_FINDINGS = 6

# A class that intercepts attribute access has an open-ended surface: a name
# absent from its method and field lists may still resolve at runtime. Meeting
# one forces silence on the unknown-method check.
_DYNAMIC_ATTR = {"__getattr__", "__getattribute__"}


def _short(name: str) -> str:
    return name.split("[")[0].split(".")[-1].strip()


# ── registry lookups ─────────────────────────────────────────────────────────


class Registry:
    def __init__(self, classes: dict[str, list[dict[str, Any]]]) -> None:
        self.classes = classes
        # Names that appear as some class's parent, built once on first need.
        # A base class with subclasses cannot have its `self.<name>()` calls
        # judged for unknown names: `self` may be a more-derived instance that
        # defines the name (the template-method pattern), so absence from the
        # base is not absence at runtime.
        self._subclassed_cache: set[str] | None = None

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Registry:
        classes = document.get("classes")
        return cls(classes if isinstance(classes, dict) else {})

    def get(self, name: str) -> list[dict[str, Any]]:
        return self.classes.get(name, [])

    def has_subclass(self, name: str) -> bool:
        """Whether any class in the registry lists ``name`` as a parent."""
        if self._subclassed_cache is None:
            acc: set[str] = set()
            for entries in self.classes.values():
                for entry in entries:
                    for parent in entry.get("parent_classes") or []:
                        acc.add(_short(parent))
            self._subclassed_cache = acc
        return name in self._subclassed_cache

    def accepted_keywords(self, entry: dict[str, Any]) -> set[str] | None:
        """Every keyword ``Name(...)`` may accept, or None if unknowable.

        None is returned generously. A wrong "unknown keyword" claim is far
        more costly than a missed one.
        """
        accepted: set[str] = set()
        seen: set[str] = set()
        queue: list[dict[str, Any]] = [entry]

        while queue:
            cls = queue.pop()
            if cls["name"] in seen:
                continue
            seen.add(cls["name"])

            for f in cls.get("fields") or []:
                fname = f.get("name")
                if not isinstance(fname, str) or fname.startswith("__"):
                    continue
                # Pydantic escape hatches make the field set open-ended.
                default = f.get("default_value") or ""
                if fname == "model_config" and "extra" in default:
                    return None
                if "alias=" in default:
                    return None
                accepted.add(fname)

            for m in cls.get("methods") or []:
                if m.get("name") != "__init__":
                    continue
                if m.get("accepts_kwargs"):
                    return None
                for p in m.get("parameters") or []:
                    if p.get("name") != "self" and p.get("kind") in {
                        "positional",
                        "keyword_only",
                    }:
                        accepted.add(str(p["name"]))

            for parent in cls.get("parent_classes") or []:
                short = _short(parent)
                if short in INERT_BASES:
                    continue
                candidates = self.get(short)
                if len(candidates) != 1:
                    # Unknown or ambiguous ancestor: its fields are unknowable.
                    return None
                queue.append(candidates[0])

        return accepted

    def methods(self, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """Every method callable on the class, and whether that list is whole.

        ``accepted_keywords`` answers "may the constructor take this name";
        this answers "does this class have this method" — the question behind
        a hallucinated ``client.get_items()`` when the method is
        ``fetch_items()``.

        Inherited methods are included, because a list of only the class's own
        body would report a real inherited method as absent — manufacturing
        exactly the false negative this tool exists to prevent. Ancestors are
        walked breadth-first and the most-derived definition of a name wins,
        which approximates MRO and matches how an override actually resolves.

        Returns ``(methods, complete)``. ``complete`` is False when an ancestor
        could not be resolved — unknown, ambiguous, or a framework base outside
        the scan such as ``BaseModel``, whose own methods can never appear here.
        **A listed method exists; absence from an incomplete list is not
        evidence that a method does not.** The list is never None, because the
        names that were found stay true regardless of the ones that were not.

        ``__init__`` is omitted: it is already reported verbatim as
        ``init_signature``, and repeating it would pad every response.
        """
        out: list[dict[str, Any]] = []
        by_name: set[str] = set()
        seen: set[str] = set()
        complete = True
        queue: list[dict[str, Any]] = [entry]

        while queue:
            cls = queue.pop(0)
            name = cls.get("name")
            if not isinstance(name, str) or name in seen:
                continue
            seen.add(name)

            for m in cls.get("methods") or []:
                mname = m.get("name")
                if not isinstance(mname, str) or mname == "__init__":
                    continue
                if mname in by_name:
                    # A more-derived class already defined it; that one wins.
                    continue
                by_name.add(mname)
                out.append({**m, "defined_in": name})

            for parent in cls.get("parent_classes") or []:
                short = _short(parent)
                if short in INERT_BASES:
                    # Skipped for keyword purposes, but a framework base does
                    # carry methods we cannot see, so the list is not whole.
                    complete = False
                    continue
                candidates = self.get(short)
                if len(candidates) != 1:
                    complete = False
                    continue
                queue.append(candidates[0])

        out.sort(key=lambda m: str(m.get("name", "")))
        return out, complete

    def required_params(
        self, entry: dict[str, Any]
    ) -> tuple[list[str], set[str]] | None:
        """Return (positional order, required names), or None if unknowable.

        ``accepted_keywords`` answers "is this a name the constructor knows";
        this answers "did the caller supply everything it must". Those are
        different failures, and a checker implementing only the first lets a
        bare ``Thing()`` against a three-argument ``__init__`` pass silently.

        None on anything uncertain, for the same reason as above.
        """
        own_init: dict[str, Any] | None = None
        for m in entry.get("methods") or []:
            if m.get("name") == "__init__":
                own_init = m
                break

        if own_init is not None:
            if own_init.get("accepts_kwargs"):
                return None
            order: list[str] = []
            required: set[str] = set()
            for p in own_init.get("parameters") or []:
                pname = p.get("name")
                if not isinstance(pname, str) or pname == "self":
                    continue
                kind = p.get("kind")
                if kind == "positional":
                    order.append(pname)
                elif kind != "keyword_only":
                    continue
                if not p.get("has_default"):
                    required.add(pname)
            return order, required

        fields = entry.get("fields")
        if fields:
            names = [
                str(f["name"])
                for f in fields
                if not str(f.get("name", "")).startswith("__")
            ]
            required = {
                str(f["name"])
                for f in fields
                if f.get("is_required") and not str(f.get("name", "")).startswith("__")
            }
            # Whether positional construction is allowed depends on what built
            # the constructor. BaseModel rejects positional arguments outright;
            # a dataclass accepts them in field order. Treating the two alike
            # reports every positionally-built dataclass as missing every
            # argument it had in fact supplied.
            if entry.get("type") == "pydantic_model":
                return [], required
            return names, required

        # No constructor and no fields of its own. It may inherit one, and
        # resolving that correctly means replaying MRO — not worth guessing.
        if entry.get("parent_classes"):
            return None
        return [], set()


# ── analysis ─────────────────────────────────────────────────────────────────


def _import_map(tree: ast.AST) -> dict[str, str]:
    """Map locally-bound class name -> source module, from ``from X import Y``.

    This is what makes duplicated names tractable. Measured over 581
    known-good files in the origin project, a name-only ambiguity check fired
    96 times — all of them on correct code whose import already said exactly
    which class was meant. Resolving through the import removes that noise
    AND lets the keyword check run on duplicated names instead of skipping
    them.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        # `level` is deliberately ignored: for a relative import like
        # `from ..models.recovery import X` the module attribute is already
        # "models.recovery", which suffix-matches the registry the same way
        # an absolute import does.
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


def _resolve(
    name: str,
    entries: list[dict[str, Any]],
    imports: dict[str, str],
    file_path: str | None,
    root: Path | None,
    root_packages: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Narrow candidates using the file's own definitions and imports.

    Registry ``module`` is relative to the scan root ("api.schemas.videos");
    an absolute import reads "<package>.api.schemas.videos". Compare on
    suffix so both spellings line up.

    A definition in the file being edited wins outright — Python resolves the
    local name first, so an identically-named class elsewhere is irrelevant.
    """
    if len(entries) <= 1:
        return entries

    if file_path and root is not None:
        try:
            here: Path | None = Path(file_path).resolve()
        except OSError:
            here = None
        if here is not None:
            local = [
                e
                for e in entries
                if (root / str(e.get("file_path", ""))).resolve() == here
            ]
            if len(local) == 1:
                return local

    module = imports.get(name)
    if not module:
        return entries

    # Registry modules are relative to the scan root, so an absolute import
    # carries one extra leading component. Strip exactly that — matching on a
    # bare tail component instead looks tempting and is wrong: `.models.` then
    # matches both `db.models` and `models.channel`, and an earlier attempt at
    # it turned 1 false positive into 78.
    norm = module
    for pkg in root_packages:
        if module.startswith(f"{pkg}."):
            norm = module[len(pkg) + 1 :]
            break

    def _matches(entry: dict[str, Any]) -> bool:
        em = str(entry.get("module") or "\0")
        # Exact module, or a re-export through a package __init__ (`from
        # ..models.takeout import RecoveryResult` where the class lives in
        # models.takeout.recovery). Two same-named classes under one package
        # stay ambiguous, which is the right answer.
        return em == norm or em.startswith(f"{norm}.")

    matched = [e for e in entries if _matches(e)]
    return matched or entries


def _calls_under_raises(tree: ast.AST) -> set[int]:
    """Calls inside ``with pytest.raises(...)``, by node id.

    A test that constructs a model with arguments deliberately missing, to
    assert the resulting ValidationError, is exercising the constructor
    rather than misusing it. Flagging those is how an advisory checker earns
    a reputation for noise — measured at 8 of 17 findings across one
    project's committed tests.
    """
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            name = (
                fn.attr
                if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name) else ""
            )
            if name != "raises":
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    exempt.add(id(child))
    return exempt


def _check_missing(
    node: ast.Call,
    name: str,
    entry: dict[str, Any],
    reg: Registry,
    findings: list[str],
    reported: set[str],
) -> None:
    """Report required constructor arguments the call does not supply."""
    spec = reg.required_params(entry)
    if spec is None:
        return
    order, required = spec
    if not required:
        return

    # `Thing(*args)` or `Thing(**kwargs)` can supply anything at runtime.
    if any(isinstance(a, ast.Starred) for a in node.args):
        return
    if any(kw.arg is None for kw in node.keywords):
        return

    supplied = {kw.arg for kw in node.keywords if kw.arg}
    # Positional arguments satisfy parameters in declaration order.
    supplied |= set(order[: len(node.args)])

    missing = sorted(required - supplied)
    if not missing:
        return

    key = f"{name}:missing:{','.join(missing)}"
    if key in reported:
        return
    reported.add(key)

    findings.append(
        f"`{name}(...)` is missing required argument(s) {missing}. "
        f"Defined at {entry['file_path']}:{entry['line_number']}; "
        f"requires {sorted(required)}."
    )


# ── self.<method>() checking ───────────────────────────────────────────────────


def _has_property_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether ``node`` is decorated ``@property`` (bare or dotted)."""
    for dec in node.decorator_list:
        rendered = ast.unparse(dec) if not isinstance(dec, ast.Call) else ""
        if rendered == "property" or rendered.endswith(".property"):
            return True
    return False


def _property_result_maybe_callable(return_type: str | None) -> bool:
    """Whether a property's annotated result might itself be callable.

    ``obj.handler()`` is *correct* when ``handler`` is a property returning a
    callable — the call lands on the returned object, not the property. Only a
    plainly non-callable annotation makes the call a bug, so an absent, ``Any``,
    or ``Callable`` annotation buys silence. A concrete class that happens to
    define ``__call__`` is a residual, rare miss accepted in exchange for not
    flagging correct code.
    """
    if not return_type:
        return True
    low = return_type.lower()
    return low == "any" or "callable" in low


def _self_attr_names(target: ast.expr) -> set[str]:
    """Names bound by a ``self.x = ...`` target, tuple-unpacking included."""
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        return {target.attr}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in target.elts:
            names |= _self_attr_names(elt)
        return names
    return set()


def _local_members(
    cls: ast.ClassDef,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Methods, class-level fields, and instance attributes seen in ``cls``.

    Local definitions are authoritative for *existence*: a member drafted in
    the very snippet being checked must never read as unknown just because the
    registry has not been regenerated yet. They only ever add to what is known
    — completeness is still decided by the registry, because an ``Edit`` may
    show a fragment of the class rather than the whole body.
    """
    methods: dict[str, dict[str, Any]] = {}
    fields: set[str] = set()
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods[item.name] = {
                "is_property": _has_property_decorator(item),
                "return_type": ast.unparse(item.returns) if item.returns else None,
            }
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            fields.add(item.target.id)
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    fields.add(target.id)

    # Instance attributes bound anywhere in the body (`self.x = ...`), covering
    # annotated and augmented assignment and tuple unpacking. These hold
    # arbitrary values — a constructor-injected callable invoked as
    # ``self._factory()`` is the common one — so a name bound this way is a
    # known attribute, never an unknown method. Over-collecting across nested
    # scopes only adds silence, the safe direction.
    for sub in ast.walk(cls):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                fields |= _self_attr_names(target)
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
            fields |= _self_attr_names(sub.target)

    return methods, fields


def _self_calls(tree: ast.AST) -> list[tuple[ast.Call, ast.ClassDef]]:
    """Every ``self.<attr>(...)`` written directly in a method of a class.

    Pairs each such call with the class whose instance ``self`` names. The
    association holds only inside a method (first parameter ``self``) written
    directly in the class body: a nested function or a nested class ends it,
    because there the binding of ``self`` is no longer guaranteed to be this
    class's instance, and guessing is the one thing this tool refuses to do.
    """
    out: list[tuple[ast.Call, ast.ClassDef]] = []

    def walk(node: ast.AST, owner: ast.ClassDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, None)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = child.args.posonlyargs + child.args.args
                first = positional[0].arg if positional else None
                if isinstance(node, ast.ClassDef) and first == "self":
                    walk(child, node)
                else:
                    walk(child, None)
            else:
                if (
                    owner is not None
                    and isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "self"
                ):
                    out.append((child, owner))
                walk(child, owner)

    walk(tree, None)
    return out


def _check_self_method(
    call: ast.Call,
    cls: ast.ClassDef,
    reg: Registry,
    imports: dict[str, str],
    file_path: str | None,
    root: Path | None,
    root_packages: tuple[str, ...],
    findings: list[str],
    reported: set[str],
) -> None:
    """Flag ``self.<attr>()`` that names a property or a nonexistent method."""
    func = call.func
    assert isinstance(func, ast.Attribute)  # guaranteed by _self_calls
    attr = func.attr
    # Dunders (``self.__class__()``, ``self.__post_init__()``) carry special
    # runtime semantics; leave them alone.
    if attr.startswith("__"):
        return

    local_methods, local_fields = _local_members(cls)

    def report_property(where: str) -> None:
        key = f"{cls.name}.{attr}:property"
        if key in reported:
            return
        reported.add(key)
        findings.append(
            f"`self.{attr}()` calls `{attr}`, a property on `{cls.name}`{where} "
            f"— a property is read as `self.{attr}` without parentheses."
        )

    # A member defined in the snippet itself is authoritative — but a property
    # is still a property.
    local = local_methods.get(attr)
    if local is not None:
        if local["is_property"] and not _property_result_maybe_callable(
            local["return_type"]
        ):
            report_property("")
        return

    entries = _resolve(
        cls.name, reg.get(cls.name), imports, file_path, root, root_packages
    )
    if len(entries) != 1:
        # The class is not in the registry, or is ambiguous. Local evidence was
        # already exhausted above; inherited methods are unknowable, so any
        # remaining name is uncertain -> silent.
        return
    entry = entries[0]

    methods, complete = reg.methods(entry)
    known = {str(m.get("name")) for m in methods}
    match = next((m for m in methods if m.get("name") == attr), None)
    if match is not None:
        # A property in the resolved list is a property even when the list is
        # incomplete: an unresolved ancestor is *less* derived and cannot
        # override the definition we can see.
        if match.get("is_property") and not _property_result_maybe_callable(
            match.get("return_type")
        ):
            where = f" ({entry['file_path']}:{entry['line_number']})"
            report_property(where)
        return

    # Not a known method. Every remaining path is a reason to stay silent
    # except the last: a whole method list on a class no subclass extends.
    accepted = reg.accepted_keywords(entry)
    if accepted is None or attr in accepted or attr in local_fields:
        return  # a field, or an open-ended (extra/alias/**kwargs) surface
    if not complete:
        return  # an ancestor was unresolvable; absence is not evidence
    if known & _DYNAMIC_ATTR or local_methods.keys() & _DYNAMIC_ATTR:
        return  # attribute access is intercepted
    if reg.has_subclass(cls.name):
        return  # `self` may be a subclass instance that defines `attr`

    # Only claim "unknown" when the snippet shows the whole class body. When the
    # registry knows own methods this snippet omits, it is a fragment (an Edit
    # of one method), and an instance attribute set in the unseen `__init__`
    # would read as an unknown method — the false positive found on real code.
    registry_own = {
        str(m.get("name")) for m in entry.get("methods") or [] if m.get("name")
    }
    if not registry_own <= set(local_methods):
        return

    key = f"{cls.name}.{attr}:unknownmethod"
    if key in reported:
        return
    reported.add(key)
    sample = sorted(n for n in known if not n.startswith("_"))[:8]
    findings.append(
        f"`self.{attr}()` is not a known method of `{cls.name}` "
        f"({entry['file_path']}:{entry['line_number']}). "
        f"Known methods include: {sample}."
    )


def analyse(
    tree: ast.AST,
    reg: Registry,
    file_path: str | None = None,
    *,
    root: Path | None = None,
    root_packages: tuple[str, ...] = (),
    max_findings: int = MAX_FINDINGS,
) -> list[str]:
    """Check constructor and ``self`` method calls in ``tree``."""
    findings: list[str] = []
    reported: set[str] = set()
    imports = _import_map(tree)
    exempt = _calls_under_raises(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        entries = _resolve(name, reg.get(name), imports, file_path, root, root_packages)
        if not entries:
            continue

        if len(entries) > 1:
            if name in reported:
                continue
            reported.add(name)
            where = "; ".join(
                f"{e['type']} at {e['file_path']}:{e['line_number']}" for e in entries
            )
            findings.append(
                f"`{name}` is defined {len(entries)} times with differing "
                f"fields and no import here disambiguates — {where}."
            )
            continue

        # Missing required arguments. Checked before the unknown-keyword pass
        # because a bare `Thing()` passes no keywords at all, so the pass
        # below has nothing to compare and returns silently.
        if id(node) not in exempt:
            _check_missing(node, name, entries[0], reg, findings, reported)

        accepted = reg.accepted_keywords(entries[0])
        if accepted is None:
            continue

        passed = {kw.arg for kw in node.keywords if kw.arg}
        unknown = sorted(passed - accepted)
        if not unknown:
            continue

        key = f"{name}:{','.join(unknown)}"
        if key in reported:
            continue
        reported.add(key)

        entry = entries[0]
        # Sourced from required_params so plain classes get a hint too.
        # Reading `fields` alone meant any non-Pydantic class listed what it
        # accepts without ever saying which of those it insists on.
        spec = reg.required_params(entry)
        required = sorted(spec[1]) if spec else []
        findings.append(
            f"`{name}(...)` got unknown keyword(s) {unknown}. "
            f"Defined at {entry['file_path']}:{entry['line_number']}; accepts "
            f"{sorted(accepted)}"
            + (f"; required: {required}" if required else "")
            + "."
        )

    for call, cls in _self_calls(tree):
        if id(call) in exempt:
            continue
        _check_self_method(
            call,
            cls,
            reg,
            imports,
            file_path,
            root,
            root_packages,
            findings,
            reported,
        )

    return findings[:max_findings]


def check_source(
    source: str,
    reg: Registry,
    file_path: str | None = None,
    *,
    root: Path | None = None,
    root_packages: tuple[str, ...] = (),
) -> list[str]:
    """Parse ``source`` and check it; unparseable source yields no findings.

    Silence on a syntax error is deliberate: mid-refactor content is normal,
    and "your file does not parse" is the one thing every other tool already
    says.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    return analyse(tree, reg, file_path, root=root, root_packages=root_packages)
