# 09 — Namespace Routing (per-conversation isolation)

**Status: implemented and verified.** This page records the design, the
reasoning behind it, and its honest limits. Read alongside `03` (tool I/O).

## Problem it solves

AnythingLLM runs one shared MCP server process at the instance level (see the
research in the chat history: [AnythingLLM MCP docs](https://docs.anythingllm.com/mcp-compatibility/desktop)).
All of a single user's conversations funnel through that one process, which
originally shared ONE kernel and ONE global namespace. Two conversations that
happened to use the same variable name (`df`, `data`, ...) would silently clobber
each other — not a memory-level data race (executions are lock-serialized), but a
**cross-conversation state-collision** that returns wrong results silently.

## Why not "just use the conversation id"

MCP does not deliver a conversation/thread identifier to the server today, and
AnythingLLM does not inject one. stdio = a single MCP session for the whole
process, so the server cannot tell conversation A's calls from B's from the
protocol. This is an ecosystem-wide gap being worked on upstream
([Claude Code #41836](https://github.com/anthropics/claude-code/issues/41836),
[Codex #19937](https://github.com/openai/codex/issues/19937) /
[PR #18093](https://github.com/openai/codex/pull/18093) add `_meta.threadId`).
Relying on the LLM to invent + remember its own id is fragile (low-entropy
collisions, context-truncation loss, stochastic omission) and puts a
correctness/security invariant on a probabilistic component.

## The chosen design (mandatory tag + forward-compatible hook)

A **mandatory `namespace` argument** on the execution tools, with the id sourced
in priority order (`server._resolve_namespace`):

1. **`_meta` conversation id** — `_conversation_id_from_ctx(ctx)` reads the MCP
   request metadata. Returns None today (AnythingLLM sends nothing); if a future
   client propagates a thread id, isolation upgrades to true per-conversation
   automatically, no code change. This is the "auto-upgrade" hedge.
2. **Explicit tag** — the caller passes `namespace`. `"new"` (or empty/`auto`/
   `default`) → the server **mints** a short id `ns-XXXX` (server-minted, not
   LLM-invented, to avoid low-entropy collisions). Any other value is sanitized
   (`[A-Za-z0-9_-]`, ≤40 chars) and used as-is.

**Reinforcement so a weak model keeps the tag** (the user's key idea):
- Input side: `namespace` is a **required** parameter (the schema forces the
  model to confront it every call) with a description telling it to reuse the
  echoed value.
- Output side: every response is wrapped by `server._wrap` with a top banner
  `active_namespace: ns-XXXX` + a trailing `[reminder] your namespace = "..."`.
  Keeping the id in the freshest context maximizes the chance the model re-sends
  it even after earlier turns are truncated.

## Execution model — one kernel per namespace, with eviction

Implemented in `kernel_manager.KernelPool` (singleton `POOL`):

- `POOL.get(ns)` → the namespace's `StatefulKernel`, created on first use.
- **Warm reserve:** `POOL.prewarm()` (called from `server.main`) boots one spare
  kernel and starts a daemon maintainer thread. A new namespace **adopts the
  reserve** (first call ~0.06 s instead of ~2.5 s); the maintainer replenishes
  the spare in the background.
- **Idle eviction:** the maintainer sweeps every `_MAINT_INTERVAL` (30 s) and
  shuts down namespaces idle beyond `config.NS_IDLE_TIMEOUT` (default 1800 s) —
  mirrors a cloud sandbox's inactivity reclaim.
- **LRU cap:** at most `config.MAX_NAMESPACES` (default 8) live kernels; the
  least-recently-used is evicted when the cap would be exceeded. Bounds RAM,
  since each namespace holds its own heavy state (N× memory is the cost of
  isolation + continuity).
- Evicted/reset namespaces are rebuilt fresh on next use (state lost — expected
  after long idle or explicit reset).

Files (`./workspace`) remain **shared** across namespaces so uploads Just Work;
only in-memory variables/imports are isolated. Residual: output filename
collisions — mitigated by telling the model (rule 4) to use distinctive names.

## Honest limitations (carry these forward)

- **Continuity depends on the model re-sending the tag.** After context
  truncation a model may drop it → server mints a new namespace → its loaded
  dataframes "disappear". Reinforcement reduces but does not eliminate this.
- **The target enterprise LLM is weak** (the project's premise). Tag-copying
  reliability scales with model quality; this mitigation is weakest exactly where
  it is most needed. Accepted by the user as a known tradeoff.
- Real fix arrives when AnythingLLM propagates a conversation id via `_meta`
  (hook already in place).

## Config / API summary

- Env: `SANDBOX_NS_IDLE_TIMEOUT` (1800), `SANDBOX_MAX_NAMESPACES` (8).
- Tools now: `execute_python_code(code, namespace)`,
  `run_python_file(file_path, namespace)`, `reset_kernel_state(namespace)`,
  `list_workspace_files()`. `ctx: Context` is injected by FastMCP and hidden from
  the tool schema.

## Verification (last run, WinPython 3.13 — all PASS)

Schema exposes code+namespace and hides ctx; namespace required; mint returns
`ns-XXXX`; distinct mints; **two namespaces both using `df` stay isolated**;
state persists within a namespace; per-namespace reset (A cleared, B untouched);
LRU cap holds; warm reserve → first call 0.06 s; idle eviction reclaims; reserve
replenishes; `test_core.py` 14/14 unaffected. Ad-hoc driver:
`scratchpad/verify_namespaces.py` (not committed).
