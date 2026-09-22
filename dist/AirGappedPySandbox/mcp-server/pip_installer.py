"""pip installs, contained in a side virtual environment.

The host network now carries an intranet PyPI mirror, so installing a missing
package is possible again -- but two things must stay true:

1. **The curated portable Python is never modified.** Packages land in
   ``config.VENV_DIR``, a venv created FROM ``config.KERNEL_PYTHON`` with
   ``--system-site-packages`` so the 4 GB pre-installed stack is still visible
   and pip does not try to re-download it. A bad install is undone by deleting
   one directory; the shipped distribution stays exactly as packaged.
2. **A failure is reported as a failure.** The expected mistake on a new host is
   that nobody pointed pip at the mirror. That has to come back as a plainly
   labelled failure naming the setting to fix -- not as silence, and not as a
   complaint about the package name.

Telling those two apart is harder than it looks. With retries disabled (which
they are, so a misconfigured host fails in a second instead of stalling for
minutes) pip prints **nothing at all** about an unreachable index: a mirror that
does not resolve and a package that genuinely is not on the mirror produce the
same "No matching distribution found". So when an install fails, this module
stops guessing from pip's text and asks the index itself -- see
:func:`effective_index` and :func:`probe_index`.

Installs also run on a **background thread**. An MCP client cancels a request
that outlives its own timeout -- the official SDK's default is 60 s, and
AnythingLLM surfaces the cancellation as "Context server request timeout" -- but
pulling a wheel from a mirror, plus creating the venv on the first call,
routinely takes longer than that. Blocking the tool call until pip finished
meant a working mirror still looked broken. So :func:`start_or_attach` launches
the install and the tool hands back a RUNNING answer well inside the client's
patience, then :func:`collect` returns the result on a later call.

The kernel picks the venv's site-packages up through ``site.addsitedir`` -- see
``kernel_manager._site_dir_code``.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import config

# One install at a time: two pip processes writing the same venv is a way to end
# up with a half-unpacked package and no error to show for it.
_INSTALL_LOCK = threading.Lock()

# What a caller may ask for: a PEP 508 name, optional extras, optional version
# specifiers. Nothing else -- no URLs, no local paths, no "-e", no bare flags.
# The model does not get to choose pip's arguments, only its packages.
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_EXTRAS = r"(?:\[[A-Za-z0-9._,-]+\])?"
_OP = r"(?:===|==|!=|<=|>=|~=|<|>)"
_VER = r"[A-Za-z0-9._*+!-]+"
_SPEC_RE = re.compile(
    rf"^{_NAME}{_EXTRAS}(?:\s*{_OP}\s*{_VER}(?:\s*,\s*{_OP}\s*{_VER})*)?$"
)
_NAME_ONLY_RE = re.compile(rf"^{_NAME}")

# Fragments that mean "pip never reached an index". Only printed when retries
# are turned back on, which is why probe_index() exists as well.
_OFFLINE_MARKERS = (
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "max retries exceeded",
    "getaddrinfo failed",
    "name or service not known",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "connection timed out",
    "read timed out",
    "could not fetch url",
    "newconnectionerror",
    "proxyerror",
    "sslerror",
    "certificate verify failed",
    "there was a problem confirming the ssl certificate",
)
_NOT_FOUND_MARKERS = (
    "no matching distribution found",
    "could not find a version that satisfies the requirement",
)
# The nastiest misconfiguration: an intranet mirror served over plain http is
# DROPPED by pip unless the host is also trusted, and pip then reports the
# perfectly ordinary-looking "no matching distribution found". Taken at face
# value that blames the package name for what is a settings problem.
_IGNORED_HOST_MARKER = "is not a trusted or secure host and is being ignored"

_PUBLIC_PYPI = "https://pypi.org/simple"

# pip's failure explanation is at the END of its output, so keep the tail.
_MAX_PIP_OUTPUT = 4000


@dataclass
class InstallReport:
    ok: bool = False
    returncode: int | None = None
    output: str = ""
    command: list[str] = field(default_factory=list)
    index_url: str = ""
    index_origin: str = ""
    index_label: str = ""
    venv_created: bool = False
    timed_out: bool = False
    seconds: float = 0.0
    error: str = ""  # set when pip never got to run at all


# --- venv --------------------------------------------------------------------


def venv_python() -> Path:
    """Path to the interpreter inside the sandbox venv (it may not exist yet)."""
    if os.name == "nt":
        return config.VENV_DIR / "Scripts" / "python.exe"
    return config.VENV_DIR / "bin" / "python"


def site_packages_dir() -> Path | None:
    """The venv's site-packages, or None if the venv has not been created.

    Resolved by layout rather than by asking the interpreter: this is called on
    every kernel start-up, where a subprocess round-trip would be pure latency.
    """
    if os.name == "nt":
        cand = config.VENV_DIR / "Lib" / "site-packages"
        return cand if cand.is_dir() else None
    matches = sorted(config.VENV_DIR.glob("lib/python*/site-packages"))
    return matches[-1] if matches else None


def ensure_venv() -> tuple[bool, str | None]:
    """Create the venv if missing. Returns (created_now, error_message)."""
    if venv_python().is_file():
        return False, None
    cmd = [
        config.KERNEL_PYTHON, "-m", "venv",
        "--system-site-packages",
        str(config.VENV_DIR),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=config.VENV_CREATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"Creating the package venv timed out after "
            f"{config.VENV_CREATE_TIMEOUT}s ({config.VENV_DIR})."
        )
    except OSError as exc:
        return False, f"Could not run '{config.KERNEL_PYTHON} -m venv': {exc}"
    if proc.returncode != 0 or not venv_python().is_file():
        detail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        return False, (
            f"Could not create the package venv at {config.VENV_DIR} "
            f"(exit {proc.returncode}).\n{detail}"
        )
    return True, None


# --- request parsing ---------------------------------------------------------


def parse_requests(raw: str) -> tuple[list[str], list[str]]:
    """Split a free-form package string into (accepted specs, rejected items).

    Small models write "pandas, polars" as readily as "pandas polars", and some
    try to smuggle in flags or a URL. Commas and whitespace both separate;
    anything that is not a plain requirement specifier is rejected by name so
    the refusal says which token was the problem.
    """
    tokens = [t for t in re.split(r"[\s,]+", (raw or "").strip()) if t]
    accepted: list[str] = []
    rejected: list[str] = []
    for token in tokens:
        if _SPEC_RE.match(token):
            accepted.append(token)
        else:
            rejected.append(token)
    return accepted, rejected


def dist_name(spec: str) -> str:
    """The distribution name out of a specifier ('polars>=1.0' -> 'polars')."""
    match = _NAME_ONLY_RE.match(spec)
    return match.group(0) if match else spec


# --- where packages actually come from ---------------------------------------


def effective_index() -> tuple[str, str]:
    """(url, origin) of the index pip will really use.

    Worth resolving rather than reporting "pip's own configuration": on a host
    where nobody set the mirror up, the honest answer is "the public PyPI", and
    printing that URL is usually enough for the user to see the mistake.
    """
    if config.PIP_INDEX_URL:
        return config.PIP_INDEX_URL, "SANDBOX_PIP_INDEX_URL"

    python = venv_python()
    if python.is_file():
        try:
            proc = subprocess.run(
                [str(python), "-m", "pip", "config", "list"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0:
            for line in proc.stdout.splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip().endswith("index-url"):
                    return value.strip().strip("'\""), "pip's own config file (pip.ini)"

    from_env = os.environ.get("PIP_INDEX_URL", "").strip()
    if from_env:
        return from_env, "the PIP_INDEX_URL environment variable"

    return _PUBLIC_PYPI, "pip's built-in default -- NO mirror is configured on this machine"


def _label(url: str, origin: str) -> str:
    return f"{url}  (from {origin})"


def probe_index(url: str, timeout: int) -> tuple[bool, str]:
    """Can this machine reach the index at all? Returns (reachable, detail).

    Any HTTP answer counts as reachable, including 401/403/404: the point is to
    separate "the network cannot get there" from "it got there and said no".
    """
    try:
        request = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "air-gapped-sandbox/index-probe"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"the index answered with HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return True, f"the index answered with HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(getattr(exc, "reason", exc))
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return False, f"{type(exc).__name__}: {exc}"


# --- install -----------------------------------------------------------------


def install(specs: list[str]) -> InstallReport:
    """Run one pip install into the sandbox venv and report exactly what happened."""
    report = InstallReport()
    with _INSTALL_LOCK:
        created, err = ensure_venv()
        report.venv_created = created
        report.index_url, report.index_origin = effective_index()
        report.index_label = _label(report.index_url, report.index_origin)
        if err is not None:
            report.error = err
            return report

        cmd = [
            str(venv_python()), "-m", "pip", "install",
            "--no-input",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            # Fail fast when there is no reachable index. pip's default is 5
            # retries with backoff, which turns a misconfigured host into a
            # multi-minute stall and then a timeout the model cannot interpret.
            "--retries", str(config.PIP_RETRIES),
            "--timeout", str(config.PIP_NETWORK_TIMEOUT),
        ]
        if config.PIP_INDEX_URL:
            cmd += ["--index-url", config.PIP_INDEX_URL]
        if config.PIP_TRUSTED_HOST:
            cmd += ["--trusted-host", config.PIP_TRUSTED_HOST]
        cmd += specs
        report.command = cmd

        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=config.PIP_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            report.seconds = time.monotonic() - started
            report.timed_out = True
            report.output = _tail(_decode(exc.stdout) + _decode(exc.stderr))
            return report
        except OSError as exc:
            report.seconds = time.monotonic() - started
            report.error = f"Could not run pip: {exc}"
            return report

        report.seconds = time.monotonic() - started
        report.returncode = proc.returncode
        report.output = _tail((proc.stdout or "") + (proc.stderr or ""))
        report.ok = proc.returncode == 0
    return report


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_PIP_OUTPUT:
        return text
    return "[... earlier pip output trimmed ...]\n" + text[-_MAX_PIP_OUTPUT:]


# --- background jobs ---------------------------------------------------------


@dataclass
class InstallJob:
    """One pip run, executing on its own thread."""

    specs: list[str]
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    report: InstallReport | None = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


_JOBS: dict[str, InstallJob] = {}
_JOBS_LOCK = threading.Lock()


def job_key(specs: list[str]) -> str:
    """Identity of an install, independent of the order the model listed it in."""
    return " ".join(sorted(specs))


def peek(specs: list[str]) -> InstallJob | None:
    """The job for this exact request, running or finished, if there is one."""
    with _JOBS_LOCK:
        return _JOBS.get(job_key(specs))


def start_or_attach(specs: list[str]) -> InstallJob:
    """Start this install in the background, or join one already in flight.

    Joining matters: the model is *told* to call again with the same arguments
    to collect the result, so the repeat call must not launch a second pip.
    """
    key = job_key(specs)
    with _JOBS_LOCK:
        existing = _JOBS.get(key)
        if existing is not None:
            return existing
        job = InstallJob(specs=list(specs), started=time.monotonic())
        _JOBS[key] = job

    def _run() -> None:
        try:
            job.report = install(job.specs)
        except Exception as exc:  # noqa: BLE001 - a dead thread must still answer
            job.report = InstallReport(error=f"The install thread crashed: {exc!r}")
        finally:
            job.done.set()

    threading.Thread(target=_run, name="pip-install", daemon=True).start()
    return job


def collect(job: InstallJob) -> InstallReport | None:
    """Take a finished job's result out of the registry, or None if still running.

    Removing it is what lets the same packages be installed again later instead
    of replaying a stale result forever.
    """
    if not job.done.is_set():
        return None
    with _JOBS_LOCK:
        _JOBS.pop(job_key(job.specs), None)
    return job.report


# --- diagnosis ---------------------------------------------------------------


def _unreachable_message(report: InstallReport, detail: str) -> str:
    base = (
        f"This machine cannot reach the package index it is using, "
        f"{report.index_url} ({detail}). Nothing is wrong with the package "
        "name -- this is a configuration problem for the USER to fix. "
    )
    if report.index_url == _PUBLIC_PYPI:
        return base + (
            "That URL is pip's built-in default, which means the intranet "
            "mirror was never configured here. Tell the user to set "
            "SANDBOX_PIP_INDEX_URL to the mirror (and SANDBOX_PIP_TRUSTED_HOST "
            "if it is plain http), then restart the MCP server."
        )
    return base + (
        f"That URL came from {report.index_origin}. Tell the user to check it "
        "is correct, that it ends in /simple, and that the host is reachable "
        "from this machine."
    )


def diagnose(report: InstallReport) -> str:
    """One actionable sentence about why an install failed.

    The case worth getting right is the host where the mirror was never
    configured. pip's own output for it is either a wall of urllib3 retry noise
    or -- with retries off -- indistinguishable from a typo in the package name,
    so this asks the index directly rather than trusting either.
    """
    if report.error:
        return report.error

    low = report.output.lower()

    if _IGNORED_HOST_MARKER in low:
        host = report.index_url.split("//", 1)[-1].split("/", 1)[0]
        return (
            "pip THREW AWAY the configured index instead of using it: it is "
            "served over plain http, which pip ignores unless the host is "
            "explicitly trusted. Nothing is wrong with the package name. Tell "
            f"the user to set SANDBOX_PIP_TRUSTED_HOST to {host} (or to use the "
            "mirror's https address) and restart the MCP server."
        )

    if report.timed_out:
        reachable, detail = probe_index(report.index_url, config.PIP_NETWORK_TIMEOUT)
        if not reachable:
            return _unreachable_message(report, detail)
        return (
            f"pip did not finish within {config.PIP_TIMEOUT}s and was killed, "
            f"even though {report.index_url} is reachable. The package is "
            "probably just large; raise SANDBOX_PIP_TIMEOUT if the mirror is "
            "known to be slow."
        )

    if any(marker in low for marker in _OFFLINE_MARKERS):
        _, detail = probe_index(report.index_url, config.PIP_NETWORK_TIMEOUT)
        return _unreachable_message(report, detail)

    if any(marker in low for marker in _NOT_FOUND_MARKERS):
        # With retries disabled pip says exactly this for an index it could not
        # reach, so the message alone does not tell the two apart. Ask the index.
        reachable, detail = probe_index(report.index_url, config.PIP_NETWORK_TIMEOUT)
        if not reachable:
            return _unreachable_message(report, detail)
        return (
            f"The index at {report.index_url} is reachable ({detail}) but has "
            "no distribution matching that requirement. Check the spelling -- "
            "the distribution name is often not the import name "
            "('beautifulsoup4', not 'bs4') -- and any version pin. Otherwise "
            "this mirror simply does not carry the package."
        )

    return (
        f"pip exited with code {report.returncode}. Read its output above; do "
        "not simply repeat the same install."
    )
