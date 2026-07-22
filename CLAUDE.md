🚀 Starter Prompt: Air-Gapped Python Sandbox MCP Server Project
1. Executive Summary & Intent Context
Problem Statement & Background
Environment: The target environment is a strict enterprise air-gapped network (closed intranet with zero internet connectivity).
Core Limitation: The internal enterprise LLM relies purely on model weight memorization, leading to hallucinations or poor reasoning when handling complex mathematics, unit conversions, data analysis, and multi-format document parsing.
Administrative Constraint: The user lacks administrative OS/SSH access to the remote server hosting the internal LLM. Requesting the internal infrastructure team to modify the server environment would take an excessively long time and likely result in an inflexible solution.
The Enabling Bridge:
The user's local PC runs AnythingLLM, connected to the internal LLM via an OpenAPI-compatible endpoint.
AnythingLLM’s Agent capability supports local MCP (Model Context Protocol) servers.
The local PC can host a customized Python MCP server, empowering the internal LLM with full Python Code Execution (Code Interpreter) capabilities locally.
Primary Goal
Build a Stateful, Air-Gapped Python Sandbox MCP Server running locally on the user's PC. This server leverages a 40GB pre-packaged Portable Python environment to provide the LLM with advanced math calculations, data analysis, chart generation, and deep file parsing (PDF, Excel, CSV, XML, JSON, Word, Markdown, Text) without requiring any internet access or pip install commands.

2. Core Architecture: The Hybrid Engine Approach
The system must follow a Hybrid Architecture combining an open-source execution core with a custom safety/artifact wrapper:



+-----------------------------------------------------------------------+
|                         AnythingLLM Agent                             |
+-----------------------------------------------------------------------+
                                   | (MCP Protocol via OpenAPI)
                                   v
+-----------------------------------------------------------------------+
|                Custom MCP Safety & Artifact Layer                     |
|  - Workspace Isolation (./workspace)                                  |
|  - File Watcher & Image/Artifact Link Generator                       |
|  - Strict Air-Gapped Rules (No pip, No network calls)                 |
+-----------------------------------------------------------------------+
                                   | (ZeroMQ / IPython Client Protocol)
                                   v
+-----------------------------------------------------------------------+
|             Stateful Execution Core (Jupyter IPython Kernel)          |
|  - Memory-Persistent Execution (Variables persist across turns)       |
|  - Powered by the 40GB Bundled Portable Python Runtime                |
+-----------------------------------------------------------------------+
Key Architectural Pillars
Stateful Execution via Jupyter IPython Kernel:

Why: Standard subprocess.run(["python", ...]) resets state on every tool call, forcing large files (e.g., a 100MB Excel or multi-page PDF) to be re-read every time the user asks a follow-up question.
Solution: Use an embedded jupyter_client IPython Kernel backend. Dataframes, parsed documents, and calculated variables remain in memory across conversation turns for high performance and low latency.
Artifact Interceptor & File Watcher:

Automatically monitors the dedicated ./workspace directory before and after code execution.
If new files (e.g., .png charts created by matplotlib, extracted .csv files, or .txt summaries) are detected, the MCP server automatically intercepts them and appends Markdown-formatted file/image links to the LLM response.
Workspace Isolation & Execution Safeguards:

Restricts code execution strictly to a designated ./workspace directory.
Enforces process execution timeouts (e.g., 30 to 60 seconds) to prevent infinite loops or hangs.
3. Detailed Technical Requirements & Package Environment
Pre-Installed Package Specification (40GB Portable Python Runtime)
Since pip install is completely disabled due to air-gap isolation, the MCP server will interact with a pre-configured Portable Python distribution containing the following libraries:

Document & File Parsing:
PDF: pdfplumber (preferred for tabular data), pypdf, PyMuPDF (fitz)
Excel & CSV: pandas, openpyxl (xlsx read/write), xlsxwriter, xlrd
XML & JSON: lxml, xmltodict, standard json, xml.etree.ElementTree
Word, Text, MD: python-docx, markdown, beautifulsoup4
Mathematics, Science & Statistics:
numpy, scipy, sympy (symbolic algebra & calculus)
Data Visualization:
matplotlib, seaborn
4. MCP Tool Specifications & Interfaces
The Python MCP Server (implemented using FastMCP or official Python mcp SDK) must expose the following primary tool interfaces to the LLM:

Tool 1: execute_python_code (Primary Tool)
Description: Executes Python code inside the stateful IPython kernel environment. Keeps variables in memory across calls. Automatically captures printed outputs and generated files in the workspace.
Input Schema:
code (string, required): The Python code snippet to be executed.
Output Schema:
stdout (string): Standard output produced by print() statements.
stderr (string): Error tracebacks or warnings, if any.
artifacts (array of strings): Markdown formatted paths/links to any newly generated files (e.g., ![Chart](./workspace/chart.png)).
execution_status (string): "SUCCESS", "ERROR", or "TIMEOUT".
Tool 2: list_workspace_files (Helper Tool)
Description: Lists all user-uploaded or generated files currently residing in the ./workspace directory (e.g., target PDFs, Excels, CSVs).
Input Schema: None.
Output Schema: Array of filenames, sizes, and extensions.
5. Prompt Engineering & System Rule Injection (Tool Description)
To guarantee that the LLM utilizes the sandbox effectively without raising errors, the MCP tool description and system instructions injected into the LLM must strictly enforce the following rules:

Air-Gap Strictness:
"Do NOT attempt to install packages using pip, apt, or conda. The environment is air-gapped. All necessary libraries (pandas, numpy, sympy, pdfplumber, openpyxl, matplotlib, seaborn, lxml, etc.) are pre-installed."
Mandatory Explicit Printing:
"The IPython kernel returns only what is explicitly printed. ALWAYS wrap final results, data summaries, and calculated values inside print() statements."
Non-Interactive Graphics Rule:
"Do NOT use interactive plot displays such as plt.show(). ALWAYS save plots directly to the workspace folder using plt.savefig('./workspace/filename.png') and call plt.close() immediately after."
Workspace Relative Path Rule:
"All input data files (PDFs, Excels, CSVs) and output artifacts MUST be accessed or written relative to the ./workspace/ directory."
Self-Correction Protocol:
"If execution yields an error (stderr), analyze the error stack trace, correct your script logic, and invoke execute_python_code again with the fixed code."
6. Implementation Roadmap & Milestones
The agent/developer executing this specification should follow these incremental steps:

Phase 1: Environment Setup: Configure the local directory layout, setting up ./workspace and verifying access to the 40GB Portable Python executable.
Phase 2: Stateful Core Integration: Wire up the jupyter_client kernel manager to handle stateful Python execution sessions.
Phase 3: MCP Protocol Wrapper: Implement the MCP Server using Python FastMCP, mapping execute_python_code to the kernel session.
Phase 4: Artifact & File Interceptor: Implement directory snapshotting/watching around code execution to capture newly created charts and files.
Phase 5: Integration & Verification with AnythingLLM: Register the local MCP server in AnythingLLM Agent settings, testing end-to-end mathematical solving, PDF/Excel data analysis, and chart rendering.
Instructions for the Executing AI / Developer
"Using the detailed specification above, design and build the Python MCP Server step-by-step. Prioritize robustness, clean error handling, strict workspace isolation, and stateful memory persistence across tool calls."