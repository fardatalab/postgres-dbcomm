#!/usr/bin/env python3
"""Synchronize a hierarchical Markdown knowledge base (KB) index.

This script is intentionally conservative:

- It only auto-writes content inside marker blocks in each directory's `README.md`.
- It creates missing `README.md` files using a template.
- It warns when a non-leaf directory contains topic docs (non-README `.md` files),
  because the KB convention is "topic docs live in leaf directories only".

Markers used in `README.md`:

- Subdirectories block: `<!-- kb-subdirs:start -->` ... `<!-- kb-subdirs:end -->`
- Documents block:      `<!-- kb-docs:start -->` ... `<!-- kb-docs:end -->`

The blocks are rewritten to reflect the filesystem tree while preserving any existing
1-line descriptions (when they can be parsed).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SUBDIRS_START = "<!-- kb-subdirs:start -->"
SUBDIRS_END = "<!-- kb-subdirs:end -->"
DOCS_START = "<!-- kb-docs:start -->"
DOCS_END = "<!-- kb-docs:end -->"

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".svn",
    "__pycache__",
    "node_modules",
}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _load_readme_template(script_dir: Path) -> str:
    # Keep the template in `references/` so it can be edited without touching code.
    template_path = script_dir.parent / "references" / "readme_template.md"
    try:
        return _read_text(template_path)
    except FileNotFoundError:
        # Fallback: keep a minimal inline template so the script is usable standalone.
        return (
            "# {{DIR_NAME}}\n\n"
            "## Purpose\n\nTODO\n\n"
            "## Subdirectories\n\n"
            f"{SUBDIRS_START}\n- (none)\n{SUBDIRS_END}\n\n"
            "## Documents\n\n"
            f"{DOCS_START}\n- (none)\n{DOCS_END}\n\n"
            "## Related\n\n- TODO\n"
        )


def _ensure_trailing_newline(s: str) -> str:
    return s if s.endswith("\n") else (s + "\n")


def _list_immediate_subdirs(dir_path: Path, ignore_dirs: set[str]) -> list[str]:
    subdirs: list[str] = []
    try:
        for entry in dir_path.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if name in ignore_dirs or name.startswith("."):
                continue
            subdirs.append(name)
    except FileNotFoundError:
        return []

    return sorted(subdirs)


def _list_immediate_docs(dir_path: Path, readme_name: str) -> list[str]:
    docs: list[str] = []
    try:
        for entry in dir_path.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() != ".md":
                continue
            if entry.name == readme_name:
                continue
            if entry.name.startswith("."):
                continue
            docs.append(entry.name)
    except FileNotFoundError:
        return []

    return sorted(docs)


def _extract_block(content: str, start: str, end: str) -> str | None:
    pattern = re.compile(re.escape(start) + r"\n(.*?)\n" + re.escape(end), re.DOTALL)
    m = pattern.search(content)
    if not m:
        return None
    return m.group(1)


def _replace_or_append_block(
    content: str,
    heading: str,
    start: str,
    end: str,
    new_lines: list[str],
) -> str:
    new_block = start + "\n" + "\n".join(new_lines) + "\n" + end

    pattern = re.compile(re.escape(start) + r"\n.*?\n" + re.escape(end), re.DOTALL)
    if pattern.search(content):
        return pattern.sub(new_block, content, count=1)

    # Markers missing: append a new section to avoid clobbering unknown formats.
    content = _ensure_trailing_newline(content).rstrip("\n")
    return content + "\n\n" + heading + "\n\n" + new_block + "\n"


def _parse_existing_subdir_descriptions(block: str) -> dict[str, str]:
    """Parse `- `subdir/`: description` into {"subdir/": "description"}."""

    result: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        # Expected: - `foo/`: something
        m = re.match(r"^-\s+`([^`]+)`\s*:\s*(.*)$", line)
        if not m:
            continue
        key = m.group(1).strip()
        desc = m.group(2).strip() or "TODO"
        if not key.endswith("/"):
            key += "/"
        result[key] = desc
    return result


def _parse_existing_doc_descriptions(block: str) -> dict[str, str]:
    """Parse `- [file](file) - description` into {"file": "description"}."""

    result: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        # Expected: - [foo.md](foo.md) - something
        m = re.match(r"^-\s+\[([^\]]+)\]\(([^)]+)\)\s*-\s*(.*)$", line)
        if not m:
            continue
        label = m.group(1).strip()
        target = m.group(2).strip()
        desc = m.group(3).strip() or "TODO"
        # Prefer the link target as the key, fall back to label.
        key = target if target else label
        result[key] = desc
    return result


def _render_subdir_lines(subdirs: list[str], existing: dict[str, str]) -> list[str]:
    if not subdirs:
        return ["- (none)"]

    lines: list[str] = []
    for name in subdirs:
        key = name + "/"
        desc = existing.get(key, "TODO")
        lines.append(f"- `{key}`: {desc}")
    return lines


def _render_doc_lines(docs: list[str], existing: dict[str, str]) -> list[str]:
    if not docs:
        return ["- (none)"]

    lines: list[str] = []
    for filename in docs:
        desc = existing.get(filename, "TODO")
        lines.append(f"- [{filename}]({filename}) - {desc}")
    return lines


def _readme_title_for_dir(dir_path: Path, root: Path) -> str:
    if dir_path.resolve() == root.resolve():
        return "Knowledge Base"
    return dir_path.name


def _sync_one_dir(
    *,
    dir_path: Path,
    root: Path,
    readme_name: str,
    ignore_dirs: set[str],
    dry_run: bool,
    verbose: bool,
) -> tuple[bool, list[str]]:
    """Return (changed, warnings)."""

    warnings: list[str] = []

    readme_path = dir_path / readme_name
    created_readme = False

    if not readme_path.exists():
        template = _load_readme_template(Path(__file__).resolve().parent)
        title = _readme_title_for_dir(dir_path, root)
        content = template.replace("{{DIR_NAME}}", title)
        if dry_run:
            if verbose:
                print(f"[dry-run] create {readme_path}")
        else:
            _write_text(readme_path, _ensure_trailing_newline(content))
        created_readme = True

    # If we just created it, re-read from disk for consistent processing.
    content = _read_text(readme_path) if readme_path.exists() else ""

    subdirs = _list_immediate_subdirs(dir_path, ignore_dirs)
    docs = _list_immediate_docs(dir_path, readme_name)

    # Warn if this directory is not a leaf but contains topic docs.
    if subdirs and docs:
        warnings.append(
            f"Non-leaf directory contains topic docs: {dir_path} (docs: {', '.join(docs)})"
        )

    existing_subdir_block = _extract_block(content, SUBDIRS_START, SUBDIRS_END) or ""
    existing_doc_block = _extract_block(content, DOCS_START, DOCS_END) or ""

    existing_subdir_desc = _parse_existing_subdir_descriptions(existing_subdir_block)
    existing_doc_desc = _parse_existing_doc_descriptions(existing_doc_block)

    new_subdir_lines = _render_subdir_lines(subdirs, existing_subdir_desc)
    new_doc_lines = _render_doc_lines(docs, existing_doc_desc)

    new_content = content
    new_content = _replace_or_append_block(
        new_content,
        "## Subdirectories",
        SUBDIRS_START,
        SUBDIRS_END,
        new_subdir_lines,
    )
    new_content = _replace_or_append_block(
        new_content,
        "## Documents",
        DOCS_START,
        DOCS_END,
        new_doc_lines,
    )

    new_content = _ensure_trailing_newline(new_content)

    changed = created_readme or (new_content != content)

    if changed:
        if dry_run:
            if verbose:
                print(f"[dry-run] update {readme_path}")
        else:
            _write_text(readme_path, new_content)

    return changed, warnings


def _walk_kb_dirs(root: Path, ignore_dirs: set[str]) -> list[Path]:
    dirs: list[Path] = []
    for current_dir, subdirs, _files in os.walk(root):
        # Mutate `subdirs` in-place to prune traversal.
        subdirs[:] = [
            d
            for d in subdirs
            if d not in ignore_dirs and not d.startswith(".")
        ]
        dirs.append(Path(current_dir))
    return dirs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize KB README indexes under a root directory (default: docs/kb)."
    )
    parser.add_argument(
        "--root",
        default="docs/kb",
        help="KB root directory (default: docs/kb)",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Create the root directory if it does not exist.",
    )
    parser.add_argument(
        "--readme-name",
        default="README.md",
        help="README filename to manage (default: README.md)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-directory updates.",
    )
    parser.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="Exit non-zero if warnings are emitted.",
    )

    args = parser.parse_args()

    root = Path(args.root)

    if not root.exists():
        if not args.init:
            print(f"ERROR: KB root does not exist: {root} (use --init to create)", file=sys.stderr)
            return 2
        if args.dry_run:
            if args.verbose:
                print(f"[dry-run] mkdir -p {root}")
        else:
            root.mkdir(parents=True, exist_ok=True)

    ignore_dirs = set(DEFAULT_IGNORE_DIRS)

    kb_dirs = _walk_kb_dirs(root, ignore_dirs)

    changed_any = False
    all_warnings: list[str] = []

    for dir_path in kb_dirs:
        changed, warnings = _sync_one_dir(
            dir_path=dir_path,
            root=root,
            readme_name=args.readme_name,
            ignore_dirs=ignore_dirs,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        changed_any = changed_any or changed
        all_warnings.extend(warnings)

    if all_warnings:
        print("WARNINGS:")
        for w in all_warnings:
            print(f"- {w}")

    if args.verbose:
        summary = "changed" if changed_any else "no changes"
        print(f"Done ({summary}).")

    if all_warnings and args.fail_on_warn:
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
