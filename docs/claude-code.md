# Claude Code setup

Claude Code gets the most complete registree experience: the MCP tools for
querying, plus two **hooks** that make the checking deterministic — the
model doesn't have to remember to verify, because verification runs on every
edit whether it remembers or not.

## 1. The MCP server

From your project directory:

```bash
claude mcp add --transport stdio --scope project registree -- uvx registree serve --root "$(pwd)"
```

`--scope project` writes the entry into `.mcp.json`, which you can commit so
every collaborator (and every Claude Code session) gets the server
automatically:

```json
{
  "mcpServers": {
    "registree": {
      "type": "stdio",
      "command": "uvx",
      "args": ["registree", "serve", "--root", "."]
    }
  }
}
```

Verify with `claude mcp list` — registree should show as connected. Then ask
Claude to call `server_info`; it reports the project root and how many
classes the registry holds.

## 2. The hooks

The MCP tools are *pull* — the agent has to think to call them. The hooks
are *push*: they run on every file edit — and on shell commands that pipe
Python through a quoted heredoc — deterministically.

- **`hook-check`** (PreToolUse) reads the *pending* content of a
  Write/Edit/MultiEdit call — before it lands — checks every constructor
  call against the registry, and hands findings back to the model as
  advisory context. It never blocks an edit. It also reads a **Bash**
  command for Python passed through a **quoted heredoc**
  (`python - <<'PY' … PY`), which is how agents run throwaway code that
  never becomes a file. See *What the hook does not see* below.
- **`hook-regen`** (PostToolUse) regenerates the registry after a Python
  file inside the scanned tree changes, debounced so bursts of edits don't
  re-scan repeatedly.

Add to `.claude/settings.json` (or `settings.local.json` for just yourself):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "uvx registree hook-check --root \"$CLAUDE_PROJECT_DIR\"",
            "timeout": 10
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "uvx registree hook-regen --root \"$CLAUDE_PROJECT_DIR\"",
            "timeout": 35
          }
        ]
      }
    ]
  }
}
```

`$CLAUDE_PROJECT_DIR` matters: Claude Code runs hook commands in whatever
directory the session happens to be in, which is not always your project —
anchoring to the project directory is the documented, portable form.

### Why the hooks never block

`hook-check` always exits 0 and only ever *advises*. This is deliberate: a
checker that wrongly blocks gets switched off within a day, and the registry
can be briefly stale (a class created seconds ago reads as unknown). Silence
is the default — anything uncertain resolves to saying nothing, because a
wrong claim trains the reader to ignore the hook.

### Verify the hooks work

```bash
echo '{"tool_name":"Write","tool_input":{"file_path":"/tmp/t.py","content":"SomeRealClassFromYourProject(bogus_kwarg=1)"}}' \
  | uvx registree hook-check --root .
```

With a class name from your project, this prints a JSON payload with
`additionalContext` describing the bad keyword. With correct code it prints
nothing and exits 0.

The shell path is checkable the same way:

```bash
printf '{"tool_name":"Bash","tool_input":{"command":"python - <<%s\nSomeRealClassFromYourProject(bogus_kwarg=1)\nPY\n"}}' "'PY'" \
  | uvx registree hook-check --root .
```

### What the hook does not see

Coverage is deliberately narrow, because guessing wrong about what a shell
command will run is worse than staying quiet:

| written as | checked |
|---|---|
| `Write` / `Edit` / `MultiEdit` on a `.py` file | yes |
| `python - <<'PY' … PY` (quoted heredoc, any wrapper — `docker exec`, `uv run`, pipes, redirects) | yes |
| `python - <<PY … PY` (**unquoted**) | no — the body is a shell template, and the text the hook sees is not the text Python receives |
| `python -c "…"` | no — recovering it means parsing shell quoting |
| a heredoc not fed to a Python interpreter | no |
| code written to a file by `cat > x.py`, then run | no as a command; yes when that file is later edited by a tool |

For anything in the "no" rows, ask directly — `verify_snippet` checks a
snippet you paste, and `get_signature` reports a class's constructor and
methods before you write the call.

## 3. Housekeeping

Add the registry cache to your `.gitignore`:

```
.registree/
```

That's it. The registry builds itself on first use and keeps itself fresh.
