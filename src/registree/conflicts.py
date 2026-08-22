"""Duplicate class-name analysis.

Reports duplicate class names, split by whether the duplication is an
accepted architectural pattern or an actual smell.

Treating every duplicate as a problem is wrong, and measurement in the origin
project said so: 21 of its 27 duplicate names were the ORM/domain pair (the
SQLAlchemy model under ``db/`` and the Pydantic model under ``models/``
sharing a name), which is deliberate layering the codebase already
disambiguated at the import site with an ``X as XDB`` alias convention.
Advising renames for those would be noise at best and harmful at worst.

So conflicts are classified:

  LAYERED   orm_model under ``db/`` + pydantic_model under ``models/``.
            Intentional. Reported as a heads-up with the aliasing convention,
            never with a rename suggestion.

  SMELL     everything else — most importantly two classes in the SAME layer,
            an exception redefined outside the exceptions module, or an enum
            copied across modules. These get rename suggestions.

The layer buckets below encode a common layered-service layout (``db/``,
``api/``, ``services/``, ``repositories/``, ``models/``…). Projects with a
different shape still get correct duplicate *detection*; only the
layered-pair amnesty and the rename suggestions are convention-dependent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel


def layer(file_path: str) -> str:
    """Bucket a file into an architectural layer.

    Order matters: ``db/models.py`` must be tested before the generic
    ``/models/`` rule, and ``api/schemas/`` before it too, or everything
    collapses into "domain".
    """
    p = file_path.replace("\\", "/")
    if "/db/" in p:
        return "db"
    if "/api/schemas/" in p or "/api/routers/" in p:
        return "api"
    if "/exceptions" in p:
        return "exceptions"
    if "/repositories/" in p:
        return "repositories"
    if "/services/" in p:
        return "services"
    if "/cli/" in p:
        return "cli"
    if "/models/" in p:
        return "domain"
    return "other"


def is_layered_orm_pair(instances: list[dict[str, Any]]) -> bool:
    """True for the accepted ORM-vs-domain duplication.

    Requires exactly one ORM model under db/ and at least one Pydantic model
    under models/, and nothing else. A third definition in the API schema
    layer means three different field sets behind one name, which is worth
    flagging — so triples are deliberately NOT covered.
    """
    if len(instances) != 2:
        return False
    by_layer = {layer(i["file_path"]): i for i in instances}
    if set(by_layer) != {"db", "domain"}:
        return False
    return bool(
        by_layer["db"]["type"] == "orm_model"
        and by_layer["domain"]["type"] == "pydantic_model"
    )


def rename_suggestions(class_name: str, instance: dict[str, Any]) -> list[str]:
    """Candidate replacement names for one definition of a smell."""
    p = instance["file_path"].replace("\\", "/")
    lay = layer(p)
    out: list[str] = []

    if lay == "exceptions":
        out.append(f"{class_name}  (keep this one; import it rather than redefining)")
    elif lay == "services":
        module = (
            Path(p).stem.replace("_service", "").replace("_", " ").title()
        ).replace(" ", "")
        out.extend([f"{module}{class_name}", f"{class_name}Result"])
    elif lay == "cli":
        out.append(f"{class_name}Choice")
    elif lay == "api":
        out.extend([f"{class_name}Schema", f"{class_name}Response"])
    elif lay == "db":
        out.append(f"{class_name}ORM")
    elif lay == "domain":
        stem = Path(p).stem.replace("_", " ").title().replace(" ", "")
        out.append(f"{stem}{class_name}")

    out.extend(
        {
            "enum": [f"{class_name}Enum"],
            "typed_dict": [f"{class_name}Dict"],
            "dataclass": [f"{class_name}Data"],
            "protocol": [f"{class_name}Protocol"],
        }.get(str(instance["type"]), [])
    )

    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


class Conflict(BaseModel):
    name: str
    kind: str  # "layered" | "smell"
    same_layer: bool
    instances: list[dict[str, Any]]
    suggestions: dict[str, list[str]]  # "file:line" -> candidate names


class ConflictReport(BaseModel):
    total_duplicate_names: int
    smells: list[Conflict]
    layered: list[Conflict]
    alias_convention: str | None


def find_conflicts(document: dict[str, Any]) -> ConflictReport:
    """Classify every duplicated name in a registry document."""
    classes = document.get("classes") or {}
    metadata = document.get("metadata") or {}
    packages = metadata.get("root_packages") or []
    package = packages[0] if packages else None

    smells: list[Conflict] = []
    layered: list[Conflict] = []
    for name, instances in classes.items():
        if len(instances) <= 1:
            continue
        is_layered = is_layered_orm_pair(instances)
        layers = [layer(i["file_path"]) for i in instances]
        conflict = Conflict(
            name=name,
            kind="layered" if is_layered else "smell",
            same_layer=len(set(layers)) == 1,
            instances=instances,
            suggestions=(
                {}
                if is_layered
                else {
                    f"{i['file_path']}:{i['line_number']}": rename_suggestions(name, i)
                    for i in instances
                }
            ),
        )
        (layered if is_layered else smells).append(conflict)

    convention = (
        f"from {package}.db.models import X as XDB" if package else "import X as XDB"
    )
    return ConflictReport(
        total_duplicate_names=len(smells) + len(layered),
        smells=smells,
        layered=layered,
        alias_convention=convention if layered else None,
    )


def print_report(report: ConflictReport, smells_only: bool = False) -> None:
    if report.total_duplicate_names == 0:
        print("no duplicate class names")
        return

    if report.smells:
        print(f"SMELL — {len(report.smells)} duplicate name(s) worth resolving")
        print("=" * 68)
        for conflict in report.smells:
            note = "  <- SAME LAYER" if conflict.same_layer else ""
            print(f"\n  {conflict.name} ({len(conflict.instances)} definitions){note}")
            for inst in conflict.instances:
                _print_instance(inst, indent="    ")
    else:
        print("no duplicate names outside the accepted ORM/domain pattern")

    if report.layered and not smells_only:
        print()
        print(f"LAYERED — {len(report.layered)} accepted ORM/domain pair(s)")
        print("=" * 68)
        print("  Intentional: the ORM model under db/ and the Pydantic model")
        print("  under models/ describe the same entity at two layers,")
        print("  disambiguated at the import site:")
        print(f"      {report.alias_convention}")
        print("  Do not rename these. Do check you imported the one you")
        print("  meant — their field names differ.")
        print()
        print("  " + ", ".join(c.name for c in report.layered))

    print()
    print(
        f"totals: {report.total_duplicate_names} duplicate names — "
        f"{len(report.smells)} smell, {len(report.layered)} layered"
    )


def _print_instance(inst: dict[str, Any], indent: str = "  ") -> None:
    print(f"{indent}{inst['file_path']}:{inst['line_number']}")
    print(f"{indent}  type: {inst['type']}, layer: {layer(inst['file_path'])}")
    if inst.get("parent_classes"):
        print(f"{indent}  parents: {', '.join(inst['parent_classes'])}")
    doc = (inst.get("docstring") or "").strip().split("\n", 1)[0].strip()
    if doc:
        print(f"{indent}  doc: {doc[:70]}{'...' if len(doc) > 70 else ''}")
