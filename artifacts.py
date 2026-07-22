"""Artifact interceptor: snapshot the workspace before/after execution and turn
any newly created or modified files into Markdown links the LLM can surface.

The strategy is a simple, dependency-free directory snapshot (path -> (mtime,
size)). We compare a "before" snapshot to an "after" snapshot and report files
that are new or whose size/mtime changed. This is more portable and predictable
inside an air-gapped host than a filesystem-watcher thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import IMAGE_EXTENSIONS, WORKSPACE_DIR


@dataclass(frozen=True)
class Artifact:
    path: Path          # absolute path on disk
    rel_path: str       # path relative to project root, POSIX style
    size: int
    is_image: bool

    def to_markdown(self) -> str:
        # Use a POSIX-style "./workspace/..." link so it renders correctly in
        # the AnythingLLM chat pane regardless of host OS.
        href = f"./{self.rel_path}"
        if self.is_image:
            return f"![{self.path.name}]({href})"
        return f"[{self.path.name}]({href})"


def snapshot(directory: Path = WORKSPACE_DIR) -> dict[str, tuple[float, int]]:
    """Map of file path -> (mtime, size) for every file under ``directory``.

    Missing directory yields an empty snapshot rather than raising, so the
    caller does not have to special-case a fresh workspace.
    """
    result: dict[str, tuple[float, int]] = {}
    if not directory.exists():
        return result
    for entry in directory.rglob("*"):
        if entry.is_file():
            try:
                stat = entry.stat()
            except OSError:
                continue
            result[str(entry)] = (stat.st_mtime, stat.st_size)
    return result


def diff(
    before: dict[str, tuple[float, int]],
    after: dict[str, tuple[float, int]],
    project_root: Path,
) -> list[Artifact]:
    """Return artifacts that are new or changed between two snapshots."""
    changed: list[Artifact] = []
    for path_str, meta in after.items():
        if before.get(path_str) == meta:
            continue  # unchanged
        path = Path(path_str)
        try:
            rel = path.relative_to(project_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        changed.append(
            Artifact(
                path=path,
                rel_path=rel,
                size=meta[1],
                is_image=path.suffix.lower() in IMAGE_EXTENSIONS,
            )
        )
    # Newest first so the most relevant chart/file leads the list.
    changed.sort(key=lambda a: after[str(a.path)][0], reverse=True)
    return changed
