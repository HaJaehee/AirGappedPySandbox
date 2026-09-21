# 07 — Key Decisions & Gotchas

**Read this before debugging.** These are the non-obvious things that cost time.

## Decisions (and why)

- **Two decoupled interpreters** (server vs kernel) via a `KernelSpec` `argv`
  override on `KernelManager._kernel_spec`. Lets the lightweight server drive a
  heavy portable-Python kernel. See `01`.
- **Snapshot/diff artifact detection**, not a filesystem watcher — more
  predictable and dependency-free inside an air-gapped host.
- **Text-block tool responses**, not typed objects — maximal compatibility with
  AnythingLLM, and the LLM only needs printed text + artifact links.
- **`last expression` shown only when nothing printed** — avoids double-output
  noise while still surfacing a bare-expression result. Rule #2 pushes the LLM to
  `print()` anyway.
- **`run_python_file` runs as `__main__` then merges names** — matches user
  intuition ("run this script") while preserving statefulness for follow-ups.
- **Warm-start on by default** — first call fast + config errors surface at
  startup. Opt out with `SANDBOX_LAZY_START`.
- **The kernel runs *inside* the workspace** (`cwd=WORKSPACE_DIR`), not at
  `PROJECT_ROOT` (v0.4.1). While the workspace was the project's own
  `./workspace`, both were equivalent; once `SANDBOX_WORKSPACE` pointed
  elsewhere — the whole point of that setting — code writing
  `./workspace/chart.png` landed under the *server's* directory while every
  tool kept reading `SANDBOX_WORKSPACE`, and files appeared to vanish. The
  tool rules now tell the model to use plain relative names.
- **A relative `SANDBOX_WORKSPACE` anchors at `PROJECT_ROOT`**, not at the
  process cwd. The host (AnythingLLM, a launcher script) picks its own cwd, so
  `./workspace` would otherwise mean a different folder per host — and a
  different one than a sibling filesystem MCP server was given.

### "It ran" is not "it worked" (v0.5.0)

Until v0.4.1 `execution_status` was `SUCCESS` whenever the cell finished without
an uncaught exception. That is a different claim from "the task succeeded", and
a small model reads the word SUCCESS as the latter and stops checking. Measured
examples that all reported SUCCESS: a swallowed traceback, `logging.error(...)`,
a background thread crashing, a child process exiting 3, `print('FAIL: ...')`,
and a cell that printed nothing at all. Meanwhile `sys.exit(0)` — a *normal*
script ending — was reported as ERROR.

So the vocabulary now grades the run instead of the interpreter, the word
SUCCESS is gone, the verdict is the first line and a `next_action:` instruction
is the last, and failures that never raise are collected two ways: a kernel-side
incident ledger (`threading.excepthook`, `subprocess`, `os.system` hooks
installed by `_STARTUP_CODE`, drained by `_PROBE_CODE` after every call) and
pattern checks on stderr. **The sandbox can prove a failure; it cannot prove a
success** — an exception the model catches itself stays invisible. The only
optimistic verdict is therefore `RAN_UNVERIFIED`. Full rationale and the
unbuilt Phase 3 (`expect`) in `11-execution-verdict-plan.md`.

Cost: one extra kernel round-trip per execution, to drain the ledger. Risk to
watch: over-alarming. `RAN_WITH_ERRORS` is worded as "check before reporting",
not "failed", so a stray `logging.error` cannot push a model into a retry loop.

## Windows / offline gotchas

- **Blocking `time.sleep` is NOT interruptible on Windows.** The interrupt event
  breaks busy loops fine (fires in ~0.2 s) but a single long C-level sleep ignores
  it. That is exactly why `_handle_timeout_locked` falls back to a **kernel
  restart** after a 5 s grace. A timeout that restarts sets `ExecResult.state_reset
  = True` and the response says in-memory state was cleared. Do not "fix" this by
  removing the restart — it is the safety net that keeps the kernel responsive.
- **Loopback TCP is fine air-gapped.** The kernel uses ZeroMQ on `127.0.0.1`; the
  "running over TCP without encryption" warning is local, not external. Loopback
  works with no network adapter.
- **Console encoding is cp949 (Korean Windows).** Printing `—` (em-dash) or
  Korean text through a piped Python `-c` can raise `UnicodeEncodeError` or show
  mojibake in captured shell output. The kernel/tool path itself handles UTF-8
  correctly (files are written with `encoding='utf-8'`); this only bites when
  eyeballing piped stdout in the Bash tool. Keep diagnostic prints ASCII.
- **matplotlib first-run font cache** builds locally (no network) on first chart —
  slightly slow once, fast after. `_STARTUP_CODE` sets the `Agg` backend so no GUI
  is needed.
- **`PyMuPDF` import name is `fitz`** (not `pymupdf`). `check_environment.py`
  probes `fitz`.

## The C:/D: path trap (important!)

Early in development the working dir resolved as
`C:\Users\loves\AirGappedPySandbox`; it was a **junction to
`D:\AirGappedPySandbox`** and later disconnected. The **canonical, real repo is
`D:\AirGappedPySandbox`** and holds all commits. If a tool reports the C: path as
missing, it is not data loss — use the D: path. `pwd` in the Bash tool is
`/d/AirGappedPySandbox`.

## Source duplication

Every server module exists **twice**: repo root (dev copy) and
`dist/AirGappedPySandbox/mcp-server/` (package copy). They must stay identical.
**Always edit root first, then `cp` into the package.** A future cleanup could
make `dist/` build-generated and git-ignored, but that is not done yet (see `08`).

## Git hygiene (what must never be committed)

`.gitignore` excludes: `.devvenv/`, `offline_wheels/`, `WPy64-*/`, `WPy32-*/`,
`dist/**/python/`, `*.zip`, `__pycache__/`, `workspace/*` (except `.gitkeep`),
`.claude/settings.local.json`. Before any commit, run `git status` and confirm no
`WPy64`, wheel, or `.zip` path is staged (the WinPython + zip are multi-GB).

## Known minor loose end

`dist/AirGappedPySandbox/mcp-server/README.md` has shown as modified (unstaged) —
a line-ending (CRLF) artifact carried from before, not a content change. Harmless;
normalize if it bothers you.
