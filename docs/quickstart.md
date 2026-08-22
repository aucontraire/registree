# Quickstart

Five minutes from zero to an agent that verifies signatures instead of
guessing them. Works with any MCP client; Claude Code users should follow
this page first, then add the hooks from [claude-code.md](claude-code.md).

## 1. Prerequisite: uv

registree is launched with [`uvx`](https://docs.astral.sh/uv/), so the only
thing to install is uv itself:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Point your agent at your project

Add registree to your MCP client's configuration. The generic JSON form,
accepted (with minor variations) by Cursor, Windsurf, Cline, Zed, and
others:

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

For Claude Code it's one command — see [claude-code.md](claude-code.md).

No other setup: on the first tool call, registree scans your project
(auto-detecting `src/` or flat layouts) and caches the registry at
`.registree/registry.json`. Add `.registree/` to your `.gitignore`.

## 3. Try it

Ask your agent things like:

> What's the constructor signature of `InvoiceCreate`? Use registree.

> Before you write that code, verify the snippet with registree.

> List the duplicate class names in this project.

Or let it happen naturally — the tool descriptions tell the agent to check
signatures before instantiating and to enumerate usages before renaming.

## 4. The same engine from your terminal

```bash
uvx registree gen                  # build the registry, print stats
uvx registree conflicts            # duplicate names: accepted vs smells
uvx registree usages SomeClass     # every usage, alias-aware
```

`registree conflicts` exits non-zero only for genuine smells, so it can go
straight into CI.

## What the tools promise (and don't)

- A duplicated class name returns **every** definition — never a silent
  first match. Import the one you mean.
- An open-ended constructor (`**kwargs`, Pydantic `extra=`/`alias=`)
  reports its contract as **unknowable** rather than pretending to a
  complete answer.
- Checks are **advisory**: an empty findings list from `verify_snippet` is
  not a proof of correctness — classes outside the registry can't be
  checked.
- `get_usages` is AST-based: docstrings and comments that mention a name
  are invisible to it. Grep those separately before a rename.
