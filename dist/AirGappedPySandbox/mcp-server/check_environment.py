"""Pre-flight check for the air-gapped sandbox.

Verifies that the interpreter chosen to run the kernel (config.KERNEL_PYTHON)
has every package the sandbox promises the LLM, and that a kernel can actually
be launched on it. Run this once after wiring up the portable Python:

    python check_environment.py

It exits non-zero if anything required is missing.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import config

# (import_name, human_label). Grouped by capability for a readable report.
REQUIRED = [
    ("pandas", "Excel/CSV dataframes"),
    ("numpy", "numerics"),
    ("scipy", "scientific computing"),
    ("sympy", "symbolic math"),
    ("matplotlib", "charts"),
    ("openpyxl", "xlsx read/write"),
    ("lxml", "XML"),
    ("bs4", "beautifulsoup4 / HTML"),
]
OPTIONAL = [
    ("seaborn", "statistical charts"),
    ("pdfplumber", "PDF tables"),
    ("pypdf", "PDF"),
    ("fitz", "PyMuPDF"),
    ("xlsxwriter", "xlsx writing"),
    ("xlrd", "legacy xls"),
    ("xmltodict", "XML<->dict"),
    ("docx", "python-docx / Word"),
    ("markdown", "Markdown"),
    ("pptx", "python-pptx / PowerPoint (.pptx)"),
    ("reportlab", "ReportLab / PDF generation"),
    ("jinja2", "Jinja2 / HTML & text templates"),
    ("plotly", "Plotly / Interactive HTML charts"),
    ("duckdb", "DuckDB / Fast in-memory SQL"),
    ("polars", "Polars / Fast DataFrame engine"),
    ("pyarrow", "PyArrow / Parquet format"),
    ("PIL", "Pillow / Image processing"),
    ("rapidfuzz", "RapidFuzz / Fuzzy string matching"),
]

_PROBE = textwrap.dedent(
    """
    import importlib.util, json, sys
    names = json.loads(sys.argv[1])
    print(json.dumps({n: importlib.util.find_spec(n) is not None for n in names}))
    """
)


def probe(python: str, names: list[str]) -> dict[str, bool]:
    import json

    out = subprocess.run(
        [python, "-c", _PROBE, json.dumps(names)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "probe failed")
    return json.loads(out.stdout.strip())


def main() -> int:
    py = config.KERNEL_PYTHON
    print(f"Kernel interpreter: {py}\n")

    try:
        ver = subprocess.run([py, "--version"], capture_output=True, text=True, timeout=30)
        print((ver.stdout or ver.stderr).strip())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot run interpreter '{py}': {exc}")
        return 2

    try:
        found = probe(py, [n for n, _ in REQUIRED + OPTIONAL] + ["ipykernel"])
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: package probe failed: {exc}")
        return 2

    missing_required = []
    print("\nRequired packages:")
    for name, label in REQUIRED:
        ok = found.get(name, False)
        print(f"  [{'OK ' if ok else 'MISS'}] {name:<12} {label}")
        if not ok:
            missing_required.append(name)

    print("\nOptional packages:")
    for name, label in OPTIONAL:
        ok = found.get(name, False)
        print(f"  [{'OK ' if ok else '-- '}] {name:<12} {label}")

    print("\nKernel runtime:")
    ipyk = found.get("ipykernel", False)
    print(f"  [{'OK ' if ipyk else 'MISS'}] ipykernel   (needed to launch the kernel)")
    if not ipyk:
        missing_required.append("ipykernel")

    # Package mirror. A warning, not a failure: the sandbox works without it,
    # but finding out mid-conversation that installs were never going to work
    # is exactly what this pre-flight exists to prevent.
    print("\nPackage mirror (used by install_python_packages):")
    try:
        import pip_installer

        url, origin = pip_installer.effective_index()
        print(f"  index: {url}")
        print(f"         from {origin}")
        reachable, detail = pip_installer.probe_index(url, 10)
        if reachable:
            print(f"  [OK ] reachable -- {detail}")
        else:
            print(f"  [WARN] NOT reachable -- {detail}")
            print("         install_python_packages will fail until this is fixed.")
            print("         Set SANDBOX_PIP_INDEX_URL to the intranet mirror")
            print("         (and SANDBOX_PIP_TRUSTED_HOST if it is plain http).")
    except Exception as exc:  # noqa: BLE001 - never fail the pre-flight on this
        print(f"  [WARN] could not check the mirror: {exc}")

    print()
    if missing_required:
        print("RESULT: FAIL -- missing required packages: " + ", ".join(missing_required))
        print("These belong in the portable Python itself. install_python_packages")
        print("can add packages from the mirror, but the base stack should not")
        print("depend on it -- the mirror may be unreachable when it matters.")
        return 1

    # Final live check: actually boot a kernel and run one statement.
    print("Booting a test kernel...", flush=True)
    try:
        from kernel_manager import KERNEL

        KERNEL.start()
        r = KERNEL.execute("print('kernel-ok')")
        KERNEL.shutdown()
        if "kernel-ok" in r.stdout:
            print("RESULT: PASS -- environment is ready.")
            return 0
        print(f"RESULT: FAIL -- kernel ran but returned unexpected output: {r.stdout!r}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: FAIL -- could not launch kernel: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
