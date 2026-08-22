"""Command-line interface.

One console script, subcommand-dispatched. With no arguments (the way an MCP
client launches it) it serves MCP over stdio; everything else is explicit:

    registree                       # serve MCP over stdio (default)
    registree serve
    registree gen [--include-tests] [--scan-dir D ...] [--output PATH]
    registree conflicts [--smells-only] [--stats]
    registree usages NAME [--include-docs] [--json]
    registree hook-check            # Claude Code PreToolUse adapter
    registree hook-regen            # Claude Code PostToolUse adapter

All subcommands accept ``--root`` (default: current directory). Commands
that read or write the registry file also accept ``--registry-path`` to
override its default location of ``<root>/.registree/registry.json``
(``gen`` spells it ``--output``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from registree.config import RegistreeConfig


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="project root (default: current directory)",
    )


def _add_registry_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--registry-path",
        type=Path,
        default=None,
        help=(
            "registry file location (default: <root>/.registree/registry.json; "
            "relative paths are anchored to the root)"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="registree",
        description="Anti-hallucination class registry, served over MCP",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="serve MCP over stdio (the default)")
    _add_root(serve)
    _add_registry_path(serve)

    gen = sub.add_parser("gen", help="generate the class registry")
    _add_root(gen)
    gen.add_argument("--include-tests", action="store_true")
    gen.add_argument("--scan-dir", action="append", default=None)
    gen.add_argument("--output", "-o", default=None)

    conflicts = sub.add_parser("conflicts", help="report duplicate class names")
    _add_root(conflicts)
    _add_registry_path(conflicts)
    conflicts.add_argument("--smells-only", action="store_true")
    conflicts.add_argument("--stats", action="store_true")

    usages = sub.add_parser("usages", help="enumerate every usage of a class")
    _add_root(usages)
    usages.add_argument("class_name")
    usages.add_argument("--include-tests", action="store_true")
    usages.add_argument("--include-docs", action="store_true")
    usages.add_argument("--json", action="store_true")

    for name in ("hook-check", "hook-regen"):
        hook = sub.add_parser(name, help=f"Claude Code {name} adapter")
        _add_root(hook)
        _add_registry_path(hook)

    return parser


def _config(args: argparse.Namespace, include_tests: bool = False) -> RegistreeConfig:
    return RegistreeConfig.discover(
        args.root or Path.cwd(),
        registry_path=getattr(args, "registry_path", None),
        include_tests=include_tests,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        from registree.server import main as serve_main

        # Bare `registree` (how MCP clients launch it) has no subparser
        # namespace, so these attributes may be absent entirely.
        serve_main(getattr(args, "root", None), getattr(args, "registry_path", None))
        return 0

    if command == "gen":
        from registree.generator import generate_registry, write_registry

        config = RegistreeConfig.discover(
            args.root or Path.cwd(),
            scan_dirs=args.scan_dir,
            include_tests=args.include_tests,
            registry_path=args.output,
        )
        registry = generate_registry(config)
        write_registry(config, registry)
        meta = registry["metadata"]
        print("class registry generated")
        print(f"  total classes:            {meta['total_classes']}")
        print(f"  distinct names:           {len(registry['classes'])}")
        print(f"  duplicate names:          {meta['duplicates_found']}")
        print(f"  typed via ancestor:       {meta['types_resolved_transitively']}")
        print(f"  scan dirs:                {', '.join(meta['scan_directories'])}")
        print(f"  git version:              {meta['git_version']}")
        print(f"  output:                   {config.registry_path}")
        return 0

    if command == "conflicts":
        from registree.conflicts import find_conflicts, print_report
        from registree.generator import load_registry_document

        config = _config(args)
        try:
            document = load_registry_document(config)
        except (OSError, ValueError) as exc:
            print(f"could not load registry ({exc})")
            print("run: registree gen")
            return 1

        if args.stats:
            _print_stats(document)
            return 0
        report = find_conflicts(document)
        print_report(report, smells_only=args.smells_only)
        # Layered duplicates alone exit 0, so this is safe to wire into a gate.
        return 1 if report.smells else 0

    if command == "usages":
        from registree.usages import ClassUsageAnalyzer, to_json
        from registree.usages import print_report as print_usages

        config = _config(args, include_tests=args.include_tests)
        grouped = ClassUsageAnalyzer(
            config, args.class_name, include_docs=args.include_docs
        ).analyze()
        if args.json:
            print(to_json(args.class_name, grouped))
        else:
            print_usages(args.class_name, grouped)
        return 0

    if command == "hook-check":
        from registree.hooks import hook_check

        return hook_check(args.root, args.registry_path)

    if command == "hook-regen":
        from registree.hooks import hook_regen

        return hook_regen(args.root, args.registry_path)

    return 2


def _print_stats(document: dict[str, object]) -> None:
    from registree.conflicts import find_conflicts, layer

    meta = document.get("metadata") or {}
    classes = document.get("classes") or {}
    assert isinstance(meta, dict) and isinstance(classes, dict)
    report = find_conflicts(document)

    print("Class Registry Statistics")
    print("=" * 40)
    print(f"generated:       {meta.get('generated_at')}")
    print(f"git version:     {meta.get('git_version')}")
    print(f"total classes:   {meta.get('total_classes')}")
    print(f"unique names:    {len(classes)}")
    print(
        f"duplicates:      {meta.get('duplicates_found')} "
        f"({len(report.layered)} layered, {len(report.smells)} smell)"
    )
    print(f"typed via base:  {meta.get('types_resolved_transitively')}")
    print(f"scan dirs:       {', '.join(meta.get('scan_directories', []))}")

    types: dict[str, int] = {}
    layers: dict[str, int] = {}
    for instances in classes.values():
        for inst in instances:
            types[inst["type"]] = types.get(inst["type"], 0) + 1
            lay = layer(inst["file_path"])
            layers[lay] = layers.get(lay, 0) + 1

    print("\nby class type:")
    for t, c in sorted(types.items(), key=lambda kv: -kv[1]):
        print(f"  {t:16} {c}")
    print("\nby layer:")
    for t, c in sorted(layers.items(), key=lambda kv: -kv[1]):
        print(f"  {t:16} {c}")


if __name__ == "__main__":
    sys.exit(main())
