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
