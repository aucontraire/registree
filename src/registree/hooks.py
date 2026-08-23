"""Claude Code hook adapters.

Two thin entry points around the core library, matching the hook protocol:
tool-call JSON arrives on stdin, and stdout is a control channel.

``hook_check`` (PreToolUse): advise on constructor calls before a write
lands. Reads the cached registry (milliseconds) and inspects the *pending*
file content. Advisory only — it emits ``additionalContext`` and never
denies. A checker that blocks wrongly gets switched off, and the registry
can be briefly stale (a class created moments ago reads as unknown).

``hook_regen`` (PostToolUse): regenerate the registry after a Python file
inside the scanned tree changes, with an mtime debounce so a burst of edits
does not re-scan repeatedly. Regeneration lives HERE and not in PreToolUse
for a measured reason: a full AST scan of a mid-sized project costs
hundreds of milliseconds, while loading the cached registry costs
single-digit milliseconds. Paying the scan before every edit would get the
hook switched off within a day.

Safety contract, both hooks:
  * ALWAYS exit 0. A broken registry tool must never interfere with editing.
  * stdout carries hook JSON only (check) or nothing (regen).
  * Failures are swallowed; staleness is self-correcting on the next edit.

Both discover their project from the working directory Claude Code runs
hooks in (the project root), so one hook command works in every project.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from registree.checker import Registry, check_source
from registree.config import RegistreeConfig
from registree.generator import generate_registry, write_registry

DEBOUNCE_SECONDS = 15


# ── shared stdin parsing ─────────────────────────────────────────────────────


def _tool_input(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    return tool_input if isinstance(tool_input, dict) else {}


def pending_content(tool_input: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (file_path, full new file text) for the pending edit.

    ``Write`` carries the whole file. ``Edit``/``MultiEdit`` carry fragments,
    which do not parse on their own — so the file is read from disk and the
    replacement applied in memory to produce something parseable. Field names
    are read defensively; they have drifted across Claude Code versions.
    """
    # camelCase variants: VS Code Copilot agent mode speaks Claude Code's
    # hook protocol but camelCases the tool input.
    path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("filePath")
    )
    if not isinstance(path, str):
        return None, None

    content = (
        tool_input.get("content")
        or tool_input.get("file_text")
        or tool_input.get("file_content")
        or tool_input.get("fileContent")
    )
    if isinstance(content, str):
        return path, content

    try:
        current = Path(path).read_text(encoding="utf-8")
    except OSError:
        return path, None

    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                return path, None
            old = edit.get("old_string") or edit.get("old_str")
            new = edit.get("new_string") or edit.get("new_str")
            if not isinstance(old, str) or not isinstance(new, str):
                return path, None
            current = current.replace(old, new, -1 if edit.get("replace_all") else 1)
        return path, current

    old = (
        tool_input.get("old_string")
        or tool_input.get("old_str")
        or tool_input.get("oldString")
    )
    new = (
        tool_input.get("new_string")
        or tool_input.get("new_str")
        or tool_input.get("newString")
    )
    if isinstance(old, str) and isinstance(new, str):
        count = -1 if tool_input.get("replace_all") else 1
        return path, current.replace(old, new, count)

    return path, None


# ── shell-executed Python ────────────────────────────────────────────────────

# A heredoc whose delimiter is QUOTED (<<'PY', <<"PY", <<-'PY'). The quoting is
# the whole point: it tells the shell to pass the body through literally, with
# no $variable, `backtick` or $(...) expansion — so what the hook reads is
# exactly what Python will receive. An UNQUOTED <<PY is deliberately not
# matched; its body is a shell template, and checking the pre-expansion text
# would mean checking source that never runs.
_HEREDOC_START = re.compile(
    r"<<-?\s*(?P<q>['\"])(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)"
)

# `python`, `python3`, `python3.12` — as a word, so `mypython` does not match.
_PYTHON_INVOCATION = re.compile(r"(?:^|[\s|;&(])python[0-9.]*(?:\s|$)")


def shell_python_blocks(command: str) -> list[str]:
    """Python source fed to an interpreter through a quoted heredoc.

    Covers the idiom that dominates agent shell use::

        python - <<'PY'      docker exec -i box python - <<'PY'
        ...                  uv run python - <<'PY' 2>&1 | tail

    and deliberately covers nothing else. ``python -c "..."`` needs shell
    quote-parsing to recover, and an unquoted heredoc is a template rather
    than source; both are left to the caller and are documented as uncovered.
    See issue #4 for why the wider Bash-parsing option was not taken.

    Every failure mode here is safe: a body extracted wrongly either fails to
    parse, and ``check_source`` is silent on unparseable input, or parses as a
    subset, whose findings are still true of the code being run.
    """
    # Cheap rejection first. This runs on EVERY Bash call, and the overwhelming
    # majority embed no Python at all; nothing below should touch the registry
    # or the filesystem before these two substring tests fail.
    if "<<" not in command or "python" not in command:
        return []

    out: list[str] = []
    pos = 0
    while (match := _HEREDOC_START.search(command, pos)) is not None:
        pos = match.end()

        # The interpreter has to be named on the same line, before the marker.
        line_start = command.rfind("\n", 0, match.start()) + 1
        if not _PYTHON_INVOCATION.search(command[line_start : match.start()]):
            continue

        # The body opens on the next line — anything else on this one is
        # redirection or a pipeline, not source.
        newline = command.find("\n", match.end())
        if newline == -1:
            continue
        body_start = newline + 1

        terminator = re.compile(
            rf"^[ \t]*{re.escape(match.group('delim'))}[ \t]*$", re.MULTILINE
        )
        end = terminator.search(command, body_start)
        if end is None:
            # Unterminated: the heredoc is still being written, or the payload
            # was truncated. Guessing where it ends would invent source.
            continue

        body = command[body_start : end.start()]
        if body.strip():
            out.append(body)
        pos = end.end()

    return out


def pending_shell_python(tool_input: dict[str, Any]) -> list[str]:
    """Python blocks from a pending Bash command, or []."""
    command = tool_input.get("command") or tool_input.get("cmd")
    if not isinstance(command, str):
        return []
    return shell_python_blocks(command)


# ── PreToolUse: constructor check ────────────────────────────────────────────


def hook_check(root: Path | None = None, registry_path: Path | None = None) -> int:
    try:
        tool_input = _tool_input(sys.stdin.read())

        path, content = pending_content(tool_input)
        if path and path.endswith(".py") and content is not None:
            # A file write: the checker gets the path too, which lets it prefer
            # a class defined in this very file over a same-named one elsewhere.
            sources: list[tuple[str | None, str]] = [(path, content)]
            subject = "pending edit"
        else:
            # A shell command running Python that will never touch a file.
            # Deliberately after the write branch, and gated on a substring
            # test, so the common Bash call costs two `in` checks and no I/O.
            sources = [(None, block) for block in pending_shell_python(tool_input)]
            subject = "pending command"

        if not sources:
            return 0

        config = RegistreeConfig.discover(
            root or Path.cwd(), registry_path=registry_path
        )
        data = json.loads(config.registry_path.read_text(encoding="utf-8"))
        reg = Registry.from_document(data if isinstance(data, dict) else {})

        findings: list[str] = []
        for source_path, source in sources:
            findings.extend(
                check_source(
                    source,
                    reg,
                    source_path,
                    root=config.root,
                    root_packages=config.root_packages,
                )
            )
        if not findings:
            return 0

        body = f"Class registry check on the {subject}:\n" + "\n".join(
            f"  - {f}" for f in findings
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": body,
                    }
                }
            )
        )
    except Exception:
        return 0
    return 0


# ── PostToolUse: registry regeneration ───────────────────────────────────────


def _file_paths(tool_input: dict[str, Any]) -> list[str]:
    out: list[str] = []
    single = tool_input.get("file_path") or tool_input.get("path")
    if isinstance(single, str):
        out.append(single)
    many = tool_input.get("file_paths")
    if isinstance(many, list):
        out.extend(p for p in many if isinstance(p, str))
    return out


def _in_scope(config: RegistreeConfig, file_path: str) -> bool:
    if not file_path.endswith(".py"):
        return False
    try:
        rel = Path(file_path).resolve().relative_to(config.root)
    except (ValueError, OSError):
        return False
    return any(rel.is_relative_to(Path(d)) for d in config.scan_dirs)


def _debounced(config: RegistreeConfig) -> bool:
    """True when the registry was regenerated very recently."""
    try:
        age = time.time() - config.registry_path.stat().st_mtime
        return age < DEBOUNCE_SECONDS
    except OSError:
        return False


def hook_regen(root: Path | None = None, registry_path: Path | None = None) -> int:
    try:
        config = RegistreeConfig.discover(
            root or Path.cwd(), registry_path=registry_path
        )
        paths = _file_paths(_tool_input(sys.stdin.read()))
        if not any(_in_scope(config, p) for p in paths):
            return 0
        if _debounced(config):
            return 0
        write_registry(config, generate_registry(config))
    except Exception:
        pass
    return 0
