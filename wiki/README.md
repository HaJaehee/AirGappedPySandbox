# Project Wiki — Air-Gapped Python Sandbox MCP Server

> **Audience: the next AI session (and human maintainers).** This wiki is the
> handoff record. Read `00` and `10` first, then jump to whatever you need.
> Everything here reflects the state as of commit `229fb8a` (branch `main`).

## What this project is (one paragraph)

A **stateful, offline Python code-interpreter exposed to an LLM over MCP**. It
lets an enterprise LLM (through AnythingLLM's agent) run real Python for math,
data analysis, charting, and multi-format document parsing on the user's local
PC — with **no internet and no `pip install`** — backed by a pre-loaded portable
Python (WinPython 3.13). Implementation is **complete and verified**; the current
work is packaging/hardening/documentation, not greenfield build.

## How to read this wiki

| File | Read it when you need… |
|------|------------------------|
| [00-overview.md](00-overview.md) | The problem, the goal, and the intent behind the design. |
| [01-architecture.md](01-architecture.md) | The hybrid engine, the two-interpreter decoupling, and the data flow of one tool call. |
| [02-components.md](02-components.md) | A module-by-module reference (config, kernel_manager, artifacts, server). Start here before editing code. |
| [03-mcp-tools.md](03-mcp-tools.md) | The exact MCP tool interfaces the LLM sees, their I/O, and the embedded usage rules. |
| [04-build-and-package.md](04-build-and-package.md) | How the portable package is assembled and how to rebuild/refresh it. |
| [05-deployment-anythingllm.md](05-deployment-anythingllm.md) | Wiring the server into AnythingLLM on the air-gapped host. |
| [06-testing-verification.md](06-testing-verification.md) | Every test that exists, how to run it, and the last known-good results. |
| [07-decisions-and-gotchas.md](07-decisions-and-gotchas.md) | **Read before debugging.** Non-obvious decisions and Windows/offline landmines. |
| [08-roadmap-open-questions.md](08-roadmap-open-questions.md) | What is not done, backlog, and open questions for the user. |
| [09-namespace-routing.md](09-namespace-routing.md) | **Per-conversation isolation** design, rationale, and limits (implemented). |
| [10-environment-and-paths.md](10-environment-and-paths.md) | **Read first for orientation.** Where everything lives, versions, git state, and the C:/D: path caveat. |
| [project-facts.json](project-facts.json) | Machine-readable facts (paths, versions, env vars, tools, verification status). Parse this instead of scraping prose. |
| [tool-specs.xml](tool-specs.xml) | Structured catalog of the MCP tool interfaces (input/output schema). |

## Golden rules for the next session

1. **Canonical repo is `D:\AirGappedPySandbox`.** The `C:\Users\loves\AirGappedPySandbox`
   path seen in older logs was a junction to D: and is now disconnected. See `10`.
2. **Source exists in two places.** Edit the repo-root `*.py` first, then sync
   the copy under `dist/AirGappedPySandbox/mcp-server/`. See `02` and `04`.
3. **Never commit heavy artifacts.** WinPython (`WPy64-*`), `offline_wheels/`,
   and `*.zip` are git-ignored on purpose. Verify `git status` before committing.
4. **The target host has no internet and no pip.** Anything that needs a new
   package must be added on an online machine and re-packaged. See `04`.
5. **Verify on the real WinPython, not just the dev venv.** See `06`.
