#!/usr/bin/env python3
"""
Apply the patches in ``patches/`` to an installed LiteLLM tree.

Runs inside the image build with nothing but the standard library, so the
runtime image needs no ``patch``, ``git`` or network access. Every target file
is checked against the SHA-256 recorded in ``manifest.json`` before and after
patching, so a base image that is not stock 1.82.3, or a patch that applies
somewhere unintended, fails the build instead of shipping.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Hunk:
    old_start: int
    lines: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FilePatch:
    path: str
    hunks: tuple[Hunk, ...]


def parse_patch(text: str) -> tuple[FilePatch, ...]:
    file_patches: list[FilePatch] = []
    path: str | None = None
    hunks: list[Hunk] = []
    hunk_lines: list[tuple[str, str]] = []
    old_start = 0

    def flush_hunk() -> None:
        nonlocal hunk_lines
        if hunk_lines:
            hunks.append(Hunk(old_start=old_start, lines=tuple(hunk_lines)))
            hunk_lines = []

    def flush_file() -> None:
        nonlocal hunks
        flush_hunk()
        if path is not None:
            if not hunks:
                raise PatchError(f"no hunks found for {path}")
            file_patches.append(FilePatch(path=path, hunks=tuple(hunks)))
        hunks = []

    for line in text.splitlines():
        if line.startswith("--- "):
            flush_file()
            path = None
        elif line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            path = target[2:] if target.startswith("b/") else target
        elif line.startswith("@@"):
            flush_hunk()
            header = line.split("@@")[1].strip()
            old_range = header.split()[0]
            old_start = int(old_range[1:].split(",")[0])
        elif path is not None and line[:1] in {" ", "+", "-"}:
            hunk_lines.append((line[0], line[1:]))
        elif path is not None and line == "":
            hunk_lines.append((" ", ""))
        elif line.startswith("\\ No newline"):
            raise PatchError("patches for files without a trailing newline are not supported")

    flush_file()
    if not file_patches:
        raise PatchError("patch contained no file sections")
    return tuple(file_patches)


def apply_hunks(original: tuple[str, ...], file_patch: FilePatch) -> tuple[str, ...]:
    result: list[str] = []
    cursor = 0
    for hunk in file_patch.hunks:
        start = hunk.old_start - 1
        if start < cursor:
            raise PatchError(f"{file_patch.path}: overlapping hunks at line {hunk.old_start}")
        result.extend(original[cursor:start])
        cursor = start
        for op, text in hunk.lines:
            if op == "+":
                result.append(text)
                continue
            if cursor >= len(original):
                raise PatchError(f"{file_patch.path}: hunk at line {hunk.old_start} runs past end of file")
            if original[cursor] != text:
                raise PatchError(
                    f"{file_patch.path}: context mismatch at line {cursor + 1}\n"
                    f"  patch expects: {text!r}\n"
                    f"  file contains: {original[cursor]!r}"
                )
            if op == " ":
                result.append(text)
            cursor += 1
    result.extend(original[cursor:])
    return tuple(result)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def litellm_package_roots(extra: tuple[Path, ...]) -> tuple[Path, ...]:
    """
    Every directory holding a ``litellm`` package this image might import.

    The official image installs LiteLLM into site-packages and also copies the
    source repo to /app, which is the working directory, so both can win an
    import depending on how the proxy is started. Patching only one of them
    would silently leave the other in place.
    """
    import importlib.util  # noqa: PLC0415  - only needed here, and importing litellm itself is slow

    roots = [Path(p).resolve() for p in extra]
    spec = importlib.util.find_spec("litellm")
    for location in (spec.submodule_search_locations or ()) if spec is not None else ():
        roots.append(Path(location).resolve().parent)
    roots.append(Path("/app"))
    seen: dict[Path, None] = {}
    for root in roots:
        if (root / "litellm" / "__init__.py").is_file():
            seen.setdefault(root, None)
    return tuple(seen)


def patch_root(root: Path, patches: tuple[FilePatch, ...], manifest: dict, dry_run: bool) -> int:
    changed = 0
    for file_patch in patches:
        target = root / file_patch.path
        expected = manifest["files"][file_patch.path]
        if not target.is_file():
            raise PatchError(f"{target} does not exist")

        original_text = target.read_text(encoding="utf-8")
        actual = sha256(original_text)
        if actual == expected["after"]:
            print(f"    already patched: {file_patch.path}")
            continue
        if actual != expected["before"]:
            raise PatchError(
                f"{target} is not the stock {manifest['base_version']} file\n"
                f"  expected sha256 {expected['before']}\n"
                f"  found    sha256 {actual}"
            )

        if "\r\n" in original_text:
            raise PatchError(f"{target} has CRLF line endings; the patches assume LF")
        patched = apply_hunks(tuple(original_text.split("\n")), file_patch)
        patched_text = "\n".join(patched)
        produced = sha256(patched_text)
        if produced != expected["after"]:
            raise PatchError(
                f"{target}: patched result does not match the manifest\n"
                f"  expected sha256 {expected['after']}\n"
                f"  produced sha256 {produced}"
            )
        if not dry_run:
            target.write_text(patched_text, encoding="utf-8")
            shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
        print(f"    patched: {file_patch.path}")
        changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        type=Path,
        help="extra directory containing a litellm package (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="verify without writing")
    args = parser.parse_args()

    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    patches = tuple(
        file_patch
        for patch_file in sorted((HERE / "patches").glob("*.patch"))
        for file_patch in parse_patch(patch_file.read_text(encoding="utf-8"))
    )
    patched_paths = {file_patch.path for file_patch in patches}
    if patched_paths != set(manifest["files"]):
        raise PatchError(f"patches touch {sorted(patched_paths)} but the manifest lists {sorted(manifest['files'])}")

    roots = litellm_package_roots(tuple(args.root))
    if not roots:
        raise PatchError("no litellm package found to patch")

    for root in roots:
        print(f"  {root}")
        patch_root(root, patches, manifest, args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PatchError as exc:
        print(f"apply_patches: {exc}", file=sys.stderr)
        sys.exit(1)
