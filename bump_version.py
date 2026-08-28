"""Bump the project version in every place that records it, from one command.

Keeps these in sync (config.VERSION is the runtime source of truth):
  1. config.py            -> VERSION = "X.Y.Z"
  2. README.md            -> top badge + a new version-history row (with --note)
  3. wiki/project-facts.json -> project.version
and re-syncs config.py into the dist package.

Usage:
  python bump_version.py 0.4.0 --note "새 기능 요약"
  python bump_version.py 0.4.0 --dry-run          # show changes, write nothing

Runs on any Python 3.8+ (stdlib only), including the air-gapped host.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Notes are Korean; a cp949 console would otherwise crash on printing them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.py"
README = ROOT / "README.md"
FACTS = ROOT / "wiki" / "project-facts.json"
DIST_CONFIG = ROOT / "dist" / "AirGappedPySandbox" / "mcp-server" / "config.py"

SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _write(p: Path, text: str, dry: bool) -> None:
    if dry:
        return
    p.write_text(text, encoding="utf-8")


def bump_config(new: str, dry: bool) -> str | None:
    text = _read(CONFIG)
    new_text, n = re.subn(
        r'(VERSION:\s*str\s*=\s*")([^"]*)(")', rf"\g<1>{new}\g<3>", text, count=1
    )
    if n != 1:
        return "config.py: VERSION line not found"
    _write(CONFIG, new_text, dry)
    if not dry and DIST_CONFIG.exists():
        _write(DIST_CONFIG, new_text, dry)
    return None


def bump_facts(new: str, dry: bool) -> str | None:
    if not FACTS.exists():
        return None  # wiki is optional
    text = _read(FACTS)
    new_text, n = re.subn(
        r'("version"\s*:\s*")([^"]*)(")', rf"\g<1>{new}\g<3>", text, count=1
    )
    if n != 1:
        return "project-facts.json: \"version\" key not found"
    _write(FACTS, new_text, dry)
    return None


def bump_readme(new: str, note: str | None, dry: bool) -> str | None:
    lines = _read(README).splitlines(keepends=True)

    badge_done = False
    for i, line in enumerate(lines):
        if line.startswith("**버전: v"):
            lines[i] = re.sub(r"v\d+\.\d+\.\d+", f"v{new}", line, count=1)
            badge_done = True
            break
    if not badge_done:
        return "README.md: version badge line (**버전: vX.Y.Z**) not found"

    if note:
        sep_idx = None
        for i, line in enumerate(lines):
            if re.match(r"^\|\s*-+\s*\|\s*-+\s*\|\s*$", line) and i > 0 and "버전" in lines[i - 1]:
                sep_idx = i
                break
        if sep_idx is None:
            return "README.md: version-history table not found (needed for --note)"
        # Demote any previously-bold version rows so only the newest is bold.
        for j in range(sep_idx + 1, len(lines)):
            lines[j] = re.sub(r"^\|\s*\*\*v(\d+\.\d+\.\d+)\*\*\s*\|", r"| v\1 |", lines[j])
        lines.insert(sep_idx + 1, f"| **v{new}** | {note} |\n")

    _write(README, "".join(lines), dry)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Bump the project version everywhere.")
    ap.add_argument("version", help="new version, e.g. 0.4.0")
    ap.add_argument("--note", help="changelog note; adds a README version-history row")
    ap.add_argument("--dry-run", action="store_true", help="show plan, write nothing")
    args = ap.parse_args()

    new = args.version.lstrip("v")
    if not SEMVER.match(new):
        print(f"error: '{args.version}' is not a valid semantic version (X.Y.Z)")
        return 2

    current = "?"
    m = re.search(r'VERSION:\s*str\s*=\s*"([^"]*)"', _read(CONFIG))
    if m:
        current = m.group(1)

    print(f"{'[dry-run] ' if args.dry_run else ''}bumping {current} -> {new}")
    if args.note:
        print(f"  changelog note: {args.note}")
    if not args.note:
        print("  (no --note: README history table not touched)")

    errors = [
        bump_config(new, args.dry_run),
        bump_readme(new, args.note, args.dry_run),
        bump_facts(new, args.dry_run),
    ]
    errors = [e for e in errors if e]
    if errors:
        for e in errors:
            print(f"  ERROR: {e}")
        return 1

    print("  updated: config.py (+ dist copy), README.md, wiki/project-facts.json")
    if not args.dry_run:
        print("Next: review the diff, then commit. Re-package dist if shipping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
