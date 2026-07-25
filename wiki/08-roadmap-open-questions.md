# 08 — Roadmap & Open Questions

## Status: shippable

The project is complete and verified for its stated goal. Items below are
optional improvements and things to confirm with the user — not blockers.

## Backlog / candidate improvements

1. **Committed MCP-tool regression tests.** Add `test_server.py` driving
   `mcp.call_tool` for all four tools (happy path + isolation + errors). Keep it
   separate from `test_core.py` so the latter stays MCP-free. The `run_python_file`
   and offline-capability checks currently exist only as ad-hoc scripts.
2. **De-duplicate source.** The `dist/.../mcp-server/` copies duplicate the
   repo-root modules. Consider a small build step (copy on package) and git-ignore
   the generated `dist/` code, so there is one source of truth. Weigh against the
   convenience of a ready-to-zip tree.
3. **Per-language docs parity.** When code changes, four docs can drift: root
   `README.md` (KO), `BUILD_PORTABLE_PACKAGE.md` (KO), `PACKAGE_INFO.txt` (KO),
   and this wiki (EN). A checklist or generator would help.
4. **Configurable interrupt grace.** `_INTERRUPT_GRACE = 5` is a constant in
   `kernel_manager.py`; could be an env var if users hit false restarts.
5. **Optional per-file `run_name` control.** `run_python_file` always uses
   `__main__`; a flag to import-as-module (no `__main__` block) could be useful.
6. **Structured tool output.** If a future MCP client supports typed/structured
   content, `_format_response` could emit JSON fields alongside the text.
7. **Manual version note.** `BUILD_PORTABLE_PACKAGE.md` mentions Python 3.12 as an
   example while the shipped portable is 3.13 — harmless but worth aligning if
   editing that file.

## Open questions for the user (ask before assuming)

- **Additional libraries?** Anything like `scikit-learn`, `statsmodels`, `nltk`,
  `Pillow`-based OCR, etc. must be bundled **now on an online PC** — it cannot be
  added on the air-gapped host. Confirm the final library list before the package
  is frozen.
- **Target Python version lock?** The offline wheels cover 3.11–3.13 win_amd64 and
  the portable is 3.13. Confirm the host architecture is win_amd64.
- **Commit policy for `dist/`?** Currently the `mcp-server/` code copies are
  committed but the heavy WinPython/wheels/zip are ignored. Confirm the user wants
  the code copies tracked (vs. build-generated).
- **Do they want `wiki/` committed?** This wiki was created on request; ask before
  committing if unsure (previous sessions committed only when explicitly told).

## Quick "resume work" checklist for the next AI

1. Read `10-environment-and-paths.md` and `project-facts.json`.
2. `cd /d/AirGappedPySandbox`; `git log --oneline` (expect `229fb8a` on top,
   clean tree apart from the known README CRLF note).
3. To run/verify anything, use the WinPython at
   `dist/AirGappedPySandbox/WPy64-313130/python/python.exe` (add repo root to
   `PYTHONPATH` when running scratch scripts that import the modules).
4. Edit root `*.py` first; sync to `dist/.../mcp-server/`.
5. Never stage `WPy64-*`, `offline_wheels`, or `*.zip`.
