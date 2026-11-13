# freebuff-mcp

MCP bridge that lets OMP (or any MCP client) use the locally installed
**Freebuff** CLI (`%USERPROFILE%\.config\manicode\freebuff.exe`, v0.2.36).

Setup instructions: [INSTALL.md](INSTALL.md).

Delegation policy: [skills/freebuff-mcp-default-coder/SKILL.md](skills/freebuff-mcp-default-coder/SKILL.md)
makes this bridge the default coding engine until the user explicitly disables
it. OMP loads it from `~/.omp/agent/managed-skills/freebuff-mcp-default-coder/SKILL.md`.

## Why a TUI bridge

Freebuff is a Bun-compiled, Codebuff-based coding agent. Its only front end is a
full-screen terminal UI — `freebuff --help` exposes no prompt/headless mode, and
its HTTP surface (`/api/v1/freebuff/session`, `/api/v1/agent-runs`) is a
client-driven agent protocol that expects a local tool runtime. Driving the real
CLI keeps auth, model catalog, free-session accounting and the file-editing tools
exactly as shipped.

How the bridge works:

1. Spawns the Freebuff binary inside a ConPTY (`pywinpty`), because the CLI falls
   back to `CON` and would otherwise steal a console.
2. Waits for the composer (`Enter a coding task or / for commands`), types the
   prompt in small chunks, and presses Enter.
3. Discovers the session folder Freebuff writes to
   `~/.config/manicode/projects/<basename(cwd)>/chats/<timestamp>/` and polls
   `log.jsonl` (`[send-message] …`, `Main prompt finished`) plus
   `chat-messages.json` (`isComplete`) until the run ends.
4. Returns the last assistant text block as `answer`; earlier text blocks are the
   agent's reasoning. Tool calls come from the persisted `tool` blocks, so the
   caller sees exactly what Freebuff read/ran.
5. Kills the PTY process afterwards; the transcript stays on disk for
   `freebuff_transcript` / `continue` (`chat_id`).

The prompt written into the composer is verified against the `content` of the
persisted user message; a mismatch fails loudly instead of silently running a
mangled instruction.

## Tools

| Tool | Purpose |
|---|---|
| `freebuff_status` | version, account, configured mode/model, live instances, projects |
| `freebuff_models` | live catalog: 12 models with keys, efforts, context windows, premium flags, current selection |
| `freebuff_set_model` | `{model, effort?}` → persists the CLI model in `settings.json`, validated against the catalog |
| `freebuff_run` | `{prompt, cwd?, chat_id?, model?, effort?, trust_agents?, timeout_seconds?, transcript_lines?}` → answer, reasoning, tool counts, credits, chat id, `session`, `model` |
| `freebuff_chats` | list persisted chats for a working directory |
| `freebuff_transcript` | read messages + tool calls of the newest or a given chat |

## Changing the model

- `freebuff_models` lists the keys (e.g. `m-096e75164d` = *DeepSeek V4.1 Flash Fast*),
  the reasoning efforts each model supports, and which one is active.
- `freebuff_set_model {"model": "flash fast", "effort": "low"}` resolves a key or a
  unique name fragment, validates the effort, and writes `freebuffModel` +
  `freebuffModelKey` (+ `freebuffReasoningEfforts`) into
  `~/.config/manicode/settings.json`. The next run/TUI start picks it up.
- `freebuff_run {"model": "SOLAR PRO"}` overrides the model for **one run only**:
  the bridge snapshots `settings.json` into `freebuff-mcp-settings-snapshot.json`,
  writes the override, runs the CLI, then restores the original bytes (or keeps a
  newer value if the file changed meanwhile). A run killed mid-flight is repaired
  on the next server start.
- `freebuffModelKey` is authoritative; the label can drift (the shipped config
  labels `m-8db1ccbba6` as `mimo/mimo-v2.5` but it is *DeepSeek V4.1 Flash*).

## Install / run

The venv already exists here:

```
C:\freebuff-mcp\.venv\Scripts\python.exe
```

Recreate it elsewhere with:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install pywinpty
```

Registered in `~/.omp/agent/mcp.json`:

```json
"freebuff": {
  "type": "stdio",
  "command": "C:/freebuff-mcp/.venv/Scripts/python.exe",
  "args": ["C:/freebuff-mcp/server.py"],
  "timeout": 0
}
```

`timeout` is the client-side MCP request timeout in milliseconds, so it is `0`
(disabled): a Freebuff run lasts minutes, and `freebuff_run` enforces its own
`timeout_seconds` (default 900).

Environment overrides: `FREEBUFF_EXE` (binary path), `FREEBUFF_CONFIG_DIR`
(default `%USERPROFILE%\.config\manicode`).

## Any MCP harness (universal)

The bridge is a plain MCP **stdio** server: newline-delimited JSON-RPC 2.0,
`initialize` → `tools/list` → `tools/call`, tools only (no prompts, resources,
OAuth or HTTP transport). Any harness that can launch a local MCP server with a
command and arguments can use it — nothing here is OMP-specific except the
example config in [INSTALL.md](INSTALL.md).

- command: `C:/freebuff-mcp/.venv/Scripts/python.exe`
- args: `["C:/freebuff-mcp/server.py"]`
- environment: none required (`FREEBUFF_EXE`, `FREEBUFF_CONFIG_DIR`, `FREEBUFF_API_BASE` optional)

| Harness | Where to declare it |
|---|---|
| OMP | `~/.omp/agent/mcp.json` — verified end to end by this repo |
| Claude Code | `claude mcp add freebuff -- C:/freebuff-mcp/.venv/Scripts/python.exe C:/freebuff-mcp/server.py`, or `.mcp.json` |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| OpenAI Codex CLI | `~/.codex/config.toml` → `[mcp_servers.freebuff]` |
| Gemini CLI | `~/.gemini/settings.json` → `mcpServers` |
| Qwen Code | `~/.qwen/settings.json` → `mcpServers` |
| Cursor | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| VS Code (Copilot agent mode) | `.vscode/mcp.json` |
| Cline | extension MCP settings (stdio server) |
| Roo Code | extension MCP settings (stdio server) |
| Continue | `config.yaml` → `mcpServers` |
| Zed | `settings.json` → `context_servers` |
| Goose | `~/.config/goose/config.yaml` (stdio extension) |
| OpenCode | `opencode.json` → `mcp` |
| OpenHands | `config.toml` |
| Kiro, Trae, Qoder | app-managed MCP settings (stdio command + args) |
| MCP Inspector | `npx @modelcontextprotocol/inspector` (debugging) |

Check the exact key names in each harness' documentation; the server itself only
needs the command, the argument and permission to run tools.

Two settings matter everywhere:

- **Allow long-running tool calls.** A run lasts minutes; a 60 s per-request
  timeout aborts it mid-flight. On OMP set `"timeout": 0`; elsewhere raise or
  disable the tool-call timeout.
- **One run at a time.** The bridge serializes calls inside one server process,
  but two harnesses each spawning their own server would compete for the same
  Freebuff session.

The delegation skill is versioned here as
[`skills/freebuff-mcp-default-coder/SKILL.md`](skills/freebuff-mcp-default-coder/SKILL.md).
Its body is harness-agnostic; copy it into the skill directory your loader reads
(OMP managed skills, Claude Code `.claude/skills`, or your own prompt library).

## Token economy (measured)

The bridge exists to move work off the harness' token budget. Numbers below come
from real runs logged by the CLI on this machine: `data.contextTokenCount` in
each chat's `log.jsonl`, and prompt/answer sizes from `chat-messages.json`
(tokens estimated as chars / 4).

| Delegated task | Harness round trip | Freebuff-side context | Harness pays | Savings vs in-harness |
|---|---|---|---|---|
| Repo Q&A (3 facts, 2 steps) | 272 + 304 chars ≈ 144 tok | 24,952 tok | 0.6% | ≈ 99% |
| 20 unit tests, 181 lines, 17 steps | 844 + 449 chars ≈ 323 tok | 43,106 tok | 0.75% | ≈ 99% |
| Small single-file change (needs a read) | ≈ 150-250 tok | ≈ 20-25k tok (mostly fixed overhead) | ≈ 1% | ≈ 90-95% vs writing it yourself |

**Expected: ~95-99% fewer harness tokens per delegated task, and ~0% for a
one-line edit** (there the round trip costs more than doing it directly).

The in-harness baseline uses the agent's own context as a conservative proxy: a
real in-harness run re-sends that context on every step, so it normally costs
more, not less.

How the number is computed:

```
savings ≈ 1 - (prompt + answer + verification) / (context the harness would rebuild itself)
```

- The agent-side context — system prompt, tool schemas, file reads, iterations —
  is paid by Freebuff's free session: every run reports `credits: 0`. It never
  reaches your API bill.
- The harness pays only the one-line brief, the short answer, and verification.
  Budget ≈ 1-3k tokens per task to read the diff and run the tests; that is the
  honest price of trusting the result.
- `transcript_lines` puts the agent's whole transcript back into harness context:
  keep it at 0 unless debugging.

Caveats: the free tier is session-limited (minutes, not tokens), premium models
need full mode, and delegating an edit you already have in context is a net loss.
Savings are per delegated task, not a reason to send everything through the
bridge.

## Context model (what Freebuff sees)

- Freebuff never sees the MCP client's conversation. It receives one prompt string,
  then builds its own context from the **working directory** (`cwd`): file tree,
  file reads, shell commands, and its own persisted chat when `chat_id` is
  supplied. Verified: asked about files in a Laravel project, it read them with
  `read_files`/`run_terminal_command` and answered correctly, while a fresh chat
  asked about the caller's conversation correctly answered that it has no access.
- The caller (OMP) pays tokens for the prompt plus whatever it reads back. Every
  step Freebuff takes (19.5k-43k tokens of context per run in testing) is billed
  to Freebuff's own free session (`credits: 0`), which is the point of the
  bridge — see [Token economy](#token-economy-measured).
- Use `trust_agents: true` for repos that ship `.agents` files or `mcp.json`,
  otherwise the CLI asks for trust and the run stalls.
- Anything Freebuff changes lands directly in `cwd`: review the diff and run the
  tests yourself before accepting the work.

## Sessions

Every `freebuff_run` preflights `freebuff-live-*.json` before spawning the CLI:

- a record whose owner process is dead is deleted (`removed_stale`);
- an unexpired lease with a live owner makes the run reuse that session
  (`preflight_action: "reuse"`);
- no usable lease (absent, or expired) starts a fresh session
  (`preflight_action: "new"`); expired-but-live records are reported under
  `ignored_stale` and never deleted, since their owner may renew them.

The CLI mints and renews its own lease, so no manual restart is required between
runs. `freebuff_status` returns the same data under `session`.

## Notes and limits

- One run at a time per server process; a second concurrent `freebuff_run` fails
  fast instead of racing the CLI.
- Prompts are flattened to a single line (a newline would submit early in the
  composer).
- Freebuff keys chats by the basename of the working directory, so two different
  projects that share a folder name share history.
- If another Freebuff window holds the live session, the CLI's "already running
  elsewhere" prompt makes the bridge fail with that message instead of hijacking
  the session.
- Free mode is session-limited (the TUI shows `N hours remaining`); the bridge
  surfaces the CLI's own screen when a session cannot start.
