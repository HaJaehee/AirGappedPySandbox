# 00 — Overview, Problem & Intent

## The problem being solved

- **Environment:** a strict enterprise **air-gapped network** (closed intranet,
  zero internet).
- **Core limitation:** the internal enterprise LLM relies purely on weight
  memorization, so it hallucinates or reasons poorly on complex math, unit
  conversions, data analysis, and multi-format document parsing.
- **Administrative constraint:** the user has **no admin/SSH access** to the
  server hosting the internal LLM, and asking the infra team to change that
  server would be slow and inflexible.

## The enabling bridge

- The user's **local PC runs AnythingLLM**, connected to the internal LLM via an
  OpenAI-compatible endpoint.
- AnythingLLM's **agent supports local MCP servers**.
- Therefore the local PC can host a **custom Python MCP server** that gives the
  internal LLM a full local **code interpreter** — without touching the remote
  LLM server at all.

## The goal

Build a **stateful, air-gapped Python sandbox MCP server** running locally,
backed by a pre-packaged portable Python, providing:

- advanced math (numpy/scipy/sympy),
- data analysis (pandas/polars/duckdb),
- chart generation (matplotlib/seaborn/plotly),
- deep file parsing (PDF, Excel, CSV, XML, JSON, Word, PowerPoint, Markdown,
  Text),

with **no internet access and no `pip install` at runtime**.

## Current status (as of commit 229fb8a)

**The build is DONE and verified.** All five roadmap phases from the original
spec are implemented:

1. ✅ Environment/layout + workspace isolation.
2. ✅ Stateful core (jupyter_client IPython kernel).
3. ✅ MCP protocol wrapper (FastMCP) with the tool set.
4. ✅ Artifact interceptor (workspace snapshot/diff → Markdown links).
5. ✅ Integration path with AnythingLLM (config + docs); portable package built
   on WinPython 3.13 and verified offline.

Work since the core build has been: adding `run_python_file`, packaging into a
portable distribution, `.gitignore` hygiene, Korean user docs, and this wiki.

## Source of truth for the original spec

The full original specification is committed verbatim at repo root as
[`CLAUDE.md`](../CLAUDE.md). This wiki supersedes it for *current state*; the spec
remains the reference for *original intent*.

## Language note

User-facing docs (`README.md`, `BUILD_PORTABLE_PACKAGE.md`, `PACKAGE_INFO.txt`)
are written in **Korean** because the user is Korean-speaking. This `wiki/` is in
**English** by explicit request (it is the AI-to-AI handoff channel).
