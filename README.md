# registree

[![test](https://github.com/aucontraire/registree/actions/workflows/test.yml/badge.svg)](https://github.com/aucontraire/registree/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/registree?cacheSeconds=3600)](https://pypi.org/project/registree/)
[![Python](https://img.shields.io/pypi/pyversions/registree?cacheSeconds=3600)](https://pypi.org/project/registree/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An anti-hallucination class registry for coding agents, served over
[MCP](https://modelcontextprotocol.io).

Coding agents guess constructor signatures from memory — a keyword that
doesn't exist, a required argument left out — and you pay for the guess in a
`TypeError` and a debugging round-trip. registree removes the guess: it walks
your codebase with `ast` (never imports), builds a registry of every class
definition, and serves it as MCP tools so the agent can **verify the
signature before writing the call**.

Two principles run through every tool:

- **Names map to lists.** A duplicated class name returns *every* definition;
  the server never silently picks the first match.
- **Honesty over confidence.** An open-ended constructor (`**kwargs`,
  Pydantic `extra=`/`alias=`) reports its contract as *unknowable*, never as
  an empty list pretending to be an answer.

## How it works

1. **Scan** — an AST walk over your source tree extracts every class:
   constructor parameters (including `**kwargs` and positional-only), typed
   fields with defaults, inheritance, docstrings. Classification is
   transitive: a model routed through your project's own base class is still
   recognized as a Pydantic model.
2. **Serve** — the registry is cached as JSON and exposed over MCP stdio.
   It maintains itself: generated on first use, regenerated whenever a
   scanned file is newer than the cache.
3. **Answer** — the agent queries it at the moment of use, instead of
   guessing.

## MCP tools

| tool | use it |
|---|---|
| `get_signature` | before writing a constructor **or method** call — required args, accepted keywords, the class's methods (inherited included), every definition of a duplicated name |
| `verify_snippet` | after drafting code — checks constructor calls against the registry |
| `search_classes` | when unsure of the exact class name |
| `list_duplicates` | which names need an explicit import to disambiguate |
| `get_usages` | before a rename — every usage, including through import aliases (`X as XDB`) |
| `server_info` | server status and registry size |

## Install

No install needed with [uv](https://docs.astral.sh/uv/) — MCP clients launch
it with `uvx`. For direct CLI use:

```bash
uv tool install registree   # or: pip install registree
```

Requires Python 3.12+.

## Wire it into your agent

Any MCP client, JSON config form:

```json
{
  "mcpServers": {
    "registree": {
      "command": "uvx",
      "args": ["registree", "serve", "--root", "/path/to/your/project"]
    }
  }
}
```

Omitting `--root` serves the directory the client launches the server in,
which for most MCP clients is the project root.

- **Quickstart** (any agent): [docs/quickstart.md](docs/quickstart.md)
- **Claude Code setup**, including the hook integration:
  [docs/claude-code.md](docs/claude-code.md)

## Agent compatibility

The **MCP tools work with any MCP client** — Claude Code, Claude Desktop,
Cursor, Windsurf, Cline, Zed, VS Code Copilot agent mode, Gemini CLI, and
anything else that speaks MCP over stdio. Structured tool output degrades
gracefully for clients that only read text content.

The **hook adapters** (`hook-check`, `hook-regen`) target Claude Code's hook
protocol, which can intercept a pending file edit — or, opt-in, Python
about to be run through a quoted shell heredoc — and hand the model
advisory feedback *before* it lands, a deterministic checkpoint the MCP
layer alone can't provide. See [docs/claude-code.md](docs/claude-code.md).

That protocol is spreading: VS Code Copilot agent mode (Preview) reads the
same format — same events, same stdin JSON, even `.claude/settings.json` —
and the adapters tolerate its camelCase field names. Codex CLI and Gemini
CLI use close-enough hook contracts that ports are straightforward. Agents
whose hook systems can't intercept file edits pre-application (Cursor,
Windsurf without model feedback, Zed with no hooks yet) still get the full
MCP toolset — the hooks just add a deterministic layer where the platform
supports one.

## CLI

The same engine is available directly:

```bash
registree gen                 # build the registry
registree conflicts           # duplicate names: accepted layering vs smells
registree usages SomeClass    # every usage, alias-aware — run before renames
registree hook-check          # Claude Code PreToolUse adapter (advisory)
registree hook-regen          # Claude Code PostToolUse adapter (debounced)
```

`registree conflicts` exits non-zero only for duplicate names that are
genuine smells — the accepted ORM/domain layered pair passes — so it is safe
to wire into CI from day one.

## The registry cache

Lives at `.registree/registry.json` by default — add `.registree/` to your
`.gitignore`. Every command that touches it accepts `--registry-path` to put
it anywhere else; relative paths are anchored to the project root.

## Development

```bash
uv sync            # creates .venv, installs deps + dev tools
uv run pytest      # includes a real stdio JSON-RPC handshake test
uv run mypy src tests
uv run ruff check .
uv run black --check .
```

Run the server directly (speaks MCP over stdio; exits on EOF):

```bash
uv run registree
```

## License

[MIT](LICENSE)
