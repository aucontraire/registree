"""Hook adapter behavior: pending-content reconstruction and the check hook."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from registree.config import RegistreeConfig
from registree.generator import generate_registry, write_registry
from registree.hooks import (
    hook_check,
    pending_content,
    pending_shell_python,
    shell_python_blocks,
)


def test_write_payload_carries_full_content() -> None:
    path, content = pending_content(
        {"file_path": "/tmp/x.py", "content": "class A:\n    pass\n"}
    )
    assert path == "/tmp/x.py"
    assert content == "class A:\n    pass\n"


def test_edit_payload_is_applied_to_disk_content(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("value = 1\n", encoding="utf-8")
    path, content = pending_content(
        {
            "file_path": str(target),
            "old_string": "value = 1",
            "new_string": "value = 2",
        }
    )
    assert path == str(target)
    assert content == "value = 2\n"


BAD_CALL = (
    "from widgetlib.gadgets import Gadget\nGadget(name='x', flag=True, colour='red')\n"
)


def test_quoted_heredoc_after_python_is_extracted() -> None:
    blocks = shell_python_blocks(f"python - <<'PY'\n{BAD_CALL}PY\n")
    assert blocks == [BAD_CALL]


def test_extraction_survives_redirection_and_pipes_after_the_marker() -> None:
    """The marker is rarely the end of the line in practice."""
    command = f"uv run python - <<'PY' 2>&1 | tail -5\n{BAD_CALL}PY\n"
    assert shell_python_blocks(command) == [BAD_CALL]


def test_extraction_handles_an_interpreter_behind_a_wrapper() -> None:
    command = f"docker exec -i box python - <<'PY'\n{BAD_CALL}PY\n"
    assert shell_python_blocks(command) == [BAD_CALL]


def test_double_quoted_and_dash_forms_are_extracted() -> None:
    assert shell_python_blocks(f'python3 - <<"PY"\n{BAD_CALL}PY\n') == [BAD_CALL]
    assert shell_python_blocks(f"python3.12 - <<-'PY'\n{BAD_CALL}\tPY\n") == [BAD_CALL]


def test_unquoted_heredoc_is_ignored() -> None:
    """Its body is a shell template. Checking pre-expansion text would mean
    reporting on source that never runs."""
    assert shell_python_blocks(f"python - <<PY\n{BAD_CALL}PY\n") == []


def test_heredoc_not_fed_to_python_is_ignored() -> None:
    assert (
        shell_python_blocks("cat <<'EOF' > notes.txt\nGadget(colour='red')\nEOF\n")
        == []
    )


def test_python_dash_c_is_not_covered() -> None:
    """A documented limitation, asserted so it cannot regress silently."""
    assert shell_python_blocks("python -c \"Gadget(colour='red')\"") == []


def test_unterminated_heredoc_yields_nothing() -> None:
    """Guessing where an unfinished body ends would invent source."""
    assert shell_python_blocks(f"python - <<'PY'\n{BAD_CALL}") == []


def test_multiple_heredocs_are_all_extracted() -> None:
    command = (
        f"python - <<'PY'\n{BAD_CALL}PY\n"
        "echo between\n"
        f"python - <<'EOF'\nx = 1\nEOF\n"
    )
    assert shell_python_blocks(command) == [BAD_CALL, "x = 1\n"]


def test_a_marker_inside_a_body_does_not_start_a_second_block() -> None:
    """Scanning must resume past the terminator, not inside the body."""
    body = "text = \"python - <<'INNER'\"\n"
    assert shell_python_blocks(f"python - <<'PY'\n{body}PY\n") == [body]


def test_command_without_python_or_heredoc_exits_on_a_substring_test() -> None:
    """The cheap rejection that keeps this off the critical path of every
    unrelated Bash call."""
    assert shell_python_blocks("ls -la && grep -rn 'python' README.md") == []
    assert shell_python_blocks("cat <<'EOF'\nhello\nEOF\n") == []


def test_pending_shell_python_reads_the_command_field() -> None:
    assert pending_shell_python({"command": f"python - <<'PY'\n{BAD_CALL}PY\n"}) == [
        BAD_CALL
    ]
    assert pending_shell_python({"file_path": "/tmp/x.py"}) == []


def test_hook_check_flags_a_constructor_call_run_through_a_heredoc(
    sample_project: Path,
    sample_config: RegistreeConfig,
    sample_registry_doc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The end-to-end gap this closes: code executed by shell, never written."""
    write_registry(sample_config, sample_registry_doc)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"python - <<'PY'\n{BAD_CALL}PY\n"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert hook_check(root=sample_project) == 0
    emitted = json.loads(capsys.readouterr().out)
    context = emitted["hookSpecificOutput"]["additionalContext"]
    assert "pending command" in context
    assert "colour" in context


def test_hook_check_is_silent_on_a_bash_call_with_no_python(
    sample_project: Path,
    sample_config: RegistreeConfig,
    sample_registry_doc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_registry(sample_config, sample_registry_doc)
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la | head"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert hook_check(root=sample_project) == 0
    assert capsys.readouterr().out == ""


def test_hook_check_does_not_touch_the_registry_for_an_ordinary_command(
    sample_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registry read, no config discovery — the substring test must reject
    before any I/O, because this now runs on every Bash call."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status --short"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("registry must not be read for an ordinary command")

    monkeypatch.setattr("registree.hooks.RegistreeConfig.discover", _explode)
    assert hook_check(root=sample_project) == 0


def test_hook_check_emits_additional_context_and_exits_zero(
    sample_project: Path,
    sample_config: RegistreeConfig,
    sample_registry_doc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_registry(sample_config, sample_registry_doc)

    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(sample_project / "src/widgetlib/new.py"),
            "content": (
                "from widgetlib.gadgets import Gadget\n"
                "Gadget(name='x', flag=True, colour='red')\n"
            ),
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert hook_check(root=sample_project) == 0
    out = capsys.readouterr().out
    body = json.loads(out)
    context = body["hookSpecificOutput"]["additionalContext"]
    assert "unknown keyword(s) ['colour']" in context


def test_hook_check_is_silent_without_registry(
    sample_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "tool_input": {
            "file_path": str(sample_project / "src/widgetlib/new.py"),
            "content": "Gadget(colour=1)\n",
        }
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    # No registry file exists — the safety contract is exit 0, no output.
    assert hook_check(root=sample_project) == 0
    assert capsys.readouterr().out == ""


def test_hook_regen_regenerates_only_in_scope(
    sample_project: Path,
    sample_config: RegistreeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from registree.hooks import hook_regen

    out_of_scope = {"tool_input": {"file_path": str(sample_project / "README.md")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(out_of_scope)))
    assert hook_regen(root=sample_project) == 0
    assert not sample_config.registry_path.exists()

    in_scope = {
        "tool_input": {"file_path": str(sample_project / "src/widgetlib/gadgets.py")}
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(in_scope)))
    assert hook_regen(root=sample_project) == 0
    assert sample_config.registry_path.is_file()

    doc = generate_registry(sample_config)
    assert (
        doc["classes"].keys()
        == json.loads(sample_config.registry_path.read_text(encoding="utf-8"))[
            "classes"
        ].keys()
    )


class TestDefensiveParsing:
    """The safety contract: garbage in, silence and exit 0 out."""

    def test_tool_input_swallows_invalid_json(self) -> None:
        from registree.hooks import _tool_input

        assert _tool_input("not json at all") == {}
        assert _tool_input("[1, 2, 3]") == {}
        assert _tool_input('{"tool_input": 5}') == {}
        assert _tool_input('{"toolInput": {"file_path": "x.py"}}') == {
            "file_path": "x.py"
        }

    def test_pending_content_rejects_non_string_path(self) -> None:
        assert pending_content({"file_path": 5}) == (None, None)
        assert pending_content({}) == (None, None)

    def test_pending_content_multiedit_payload(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text("a = 1\nb = 2\n", encoding="utf-8")
        path, content = pending_content(
            {
                "file_path": str(target),
                "edits": [
                    {"old_string": "a = 1", "new_string": "a = 10"},
                    {"old_string": "b = 2", "new_string": "b = 20"},
                ],
            }
        )
        assert path == str(target)
        assert content == "a = 10\nb = 20\n"

    def test_pending_content_malformed_edit_entry(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text("a = 1\n", encoding="utf-8")
        path, content = pending_content(
            {"file_path": str(target), "edits": ["not a dict"]}
        )
        assert path == str(target)
        assert content is None
        path, content = pending_content(
            {"file_path": str(target), "edits": [{"old_string": 1}]}
        )
        assert content is None

    def test_pending_content_unreadable_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.py"
        path, content = pending_content(
            {"file_path": str(missing), "old_string": "a", "new_string": "b"}
        )
        assert path == str(missing)
        assert content is None

    def test_hook_check_ignores_non_python_files(
        self,
        sample_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        payload = {"tool_input": {"file_path": "notes.md", "content": "# hi"}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook_check(root=sample_project) == 0
        assert capsys.readouterr().out == ""

    def test_hook_check_swallows_corrupt_registry(
        self,
        sample_project: Path,
        sample_config: RegistreeConfig,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        sample_config.registry_path.parent.mkdir(parents=True, exist_ok=True)
        sample_config.registry_path.write_text("{ not json", encoding="utf-8")
        payload = {
            "tool_input": {
                "file_path": str(sample_project / "src/widgetlib/x.py"),
                "content": "Gadget(colour=1)\n",
            }
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook_check(root=sample_project) == 0
        assert capsys.readouterr().out == ""

    def test_hook_regen_debounces_repeat_calls(
        self,
        sample_project: Path,
        sample_config: RegistreeConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from registree.hooks import hook_regen

        in_scope = json.dumps(
            {
                "tool_input": {
                    "file_path": str(sample_project / "src/widgetlib/gadgets.py")
                }
            }
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(in_scope))
        assert hook_regen(root=sample_project) == 0
        first_mtime = sample_config.registry_path.stat().st_mtime_ns

        monkeypatch.setattr("sys.stdin", io.StringIO(in_scope))
        assert hook_regen(root=sample_project) == 0
        assert sample_config.registry_path.stat().st_mtime_ns == first_mtime

    def test_hook_regen_swallows_garbage_stdin(
        self, sample_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from registree.hooks import hook_regen

        monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
        assert hook_regen(root=sample_project) == 0

    def test_pending_content_accepts_file_content_field(self) -> None:
        # code.claude.com documents `file_content` for Write payloads; older
        # probes observed `content`. Both spellings must work.
        path, content = pending_content(
            {"file_path": "/tmp/x.py", "file_content": "a = 1\n"}
        )
        assert (path, content) == ("/tmp/x.py", "a = 1\n")

    def test_pending_content_accepts_camelcase_fields(self, tmp_path: Path) -> None:
        # VS Code Copilot agent mode speaks Claude Code's hook protocol with
        # camelCase tool input.
        path, content = pending_content(
            {"filePath": "/tmp/x.py", "fileContent": "a = 1\n"}
        )
        assert (path, content) == ("/tmp/x.py", "a = 1\n")

        target = tmp_path / "mod.py"
        target.write_text("value = 1\n", encoding="utf-8")
        path, content = pending_content(
            {
                "filePath": str(target),
                "oldString": "value = 1",
                "newString": "value = 2",
            }
        )
        assert (path, content) == (str(target), "value = 2\n")
