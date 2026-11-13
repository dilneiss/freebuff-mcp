---
name: freebuff-mcp-default-coder
description: Default to the freebuff MCP for coding work on this machine unless the user explicitly says to stop using it or names another engine.
---

# Freebuff MCP as the default coding engine

Policy for this machine (bridge at `C:/freebuff-mcp`, registered as the `freebuff` MCP server). Freebuff is
a free coding agent backed by the user's own free session, so its work costs no API tokens in the harness.

Install/registration lives in [../../INSTALL.md](../../INSTALL.md); the harness-agnostic wiring is in the
[README](../../README.md#any-mcp-harness-universal).

## Policy

- Coding work MUST go through the `freebuff` MCP first: implementing features, fixing bugs, writing or
  refactoring tests, generating project docs/scripts, multi-file edits inside a repository.
  "Coding work" means the change is in a repo, not a one-line answer.
- Continue using your own tools for: reading the repo to write the brief, verifying the result,
  answering questions, and edits so small that a full run costs more than doing it (single line, typo,
  one config value already in context).
- Explicit stop triggers — after any of these, stop delegating until the user reverses it:
  "pare de usar o freebuff", "não use mais o freebuff", "desative o freebuff", "use <outro modelo/engine>",
  "não quero o MCP", or the MCP being removed/disabled.
- If the MCP is not available in the session, say so once and continue with your own tools.

## Invoking

- `freebuff_status` to confirm the install/account and the current session lease.
- `freebuff_run { prompt, cwd, timeout_seconds, chat_id?, model?, effort?, trust_agents? }`:
  - `cwd` is the repository Freebuff works in; it re-reads files itself and never sees this conversation.
  - Write the brief as **one line** (newlines are flattened): goal, files allowed, acceptance test/command
    to run, and what must not change. Example:
    `Crie N testes para App\Services\Foo em tests/Unit/FooTest.php; rode 'php vendor/bin/phpunit tests/Unit';
     não altere app/ nem testes existentes; responda com arquivos criados e resultado.`
  - Reuse `chat_id` only for a follow-up on the same task; omit it for unrelated work.
  - `trust_agents: true` when the repo ships `.agents`/`mcp.json`, otherwise the CLI stalls on a trust prompt.
  - Runs are serialized and slow (8-25 s trivial, minutes for real work): set `timeout_seconds` accordingly
    (600-1800 for real tasks) and do not fire a second run in parallel.
- Keep `transcript_lines` at 0 by default; ask for a short final answer — every character returned is
  billed in the harness.

## Choosing the model

- `freebuff_models` returns the live catalog (keys like `m-096e75164d`, display names, supported
  efforts, context windows, premium flags, and which model is active). Call it before naming a model.
- `freebuff_set_model {"model": "flash fast", "effort": "low"}` changes the persistent CLI model. It
  validates the key/name and the effort against the catalog and writes `settings.json`.
- `freebuff_run { model: "SOLAR PRO", effort: ... }` overrides the model for one run only; the bridge
  restores `settings.json` afterwards (snapshot file repairs an interrupted run on next start).
- Pick cheap/fast models (`DeepSeek V4.1 Flash Fast`, `Solar Mini 4`) for mechanical work and
  stronger ones (`Beam 1.0`, `Muse Spark 1.3`, `GPT-6 Luna` when premium is allowed) for design-heavy
  tasks. The CLI's `freebuffModelKey` is authoritative; labels may be stale.

## Sessions

- Each run preflights live session leases: dead records are removed; `session.preflight_action` is `reuse`
  when an unexpired lease with a live owner exists, `new` when there is none or it expired.
- Never restart the CLI, delete `freebuff-live-*.json` records, or manage tokens by hand; the CLI renews
  its own session on the next run.

## Verification (mandatory)

- After each run check `git status --short` / `git diff --stat` for scope creep, run the tests or command
  that proves the change, and only then report. The returned `answer` is not evidence.
- If Freebuff fails twice on the same task (or hits "session limit"/"already running elsewhere"), report it
  and finish with your own tools instead of looping.
