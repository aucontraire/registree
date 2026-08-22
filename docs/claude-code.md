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
are *push*: they run on every file edit, deterministically.

- **`hook-check`** (PreToolUse) reads the *pending* content of a
  Write/Edit/MultiEdit call — before it lands — checks every constructor
  call against the registry, and hands findings back to the model as
  advisory context. It never blocks an edit.
- **`hook-regen`** (PostToolUse) regenerates the registry after a Python
  file inside the scanned tree changes, debounced so bursts of edits don't
  re-scan repeatedly.

Add to `.claude/settings.json` (or `settings.local.json` for just yourself):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
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

## 3. Housekeeping

Add the registry cache to your `.gitignore`:

```
.registree/
```

That's it. The registry builds itself on first use and keeps itself fresh.
