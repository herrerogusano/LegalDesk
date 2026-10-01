"""Build deterministic, dependency-complete LegalDesk release artifacts.

This script is intentionally offline: dependencies must already exist in a
Linux/Python 3.12 dependency root prepared by the release operator. It never
invokes pip, AWS, or a network client.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


RUNTIME = "python3.12"
PLATFORM = "manylinux_x86_64"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
FRONTEND_FILES = ("index.html", "styles.css", "citations.js", "diagnostics.js", "app.js")
EXCLUDED_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "tests",
        "test",
        "tmp",
        ".agents",
    }
)
SECRET_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secret.json",
    }
)


class PackagingError(ValueError):
    """The release input is unsafe or not reproducible."""


def _safe_relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path.replace("\\", "/"))
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any(part.endswith(":") or "\x00" in part for part in candidate.parts)
    ):
        raise PackagingError("unsafe artifact path")
    return candidate


def _excluded(relative: PurePosixPath) -> bool:
    names = {part.lower() for part in relative.parts}
    if names & EXCLUDED_NAMES:
        return True
    leaf = relative.name.lower()
    if leaf in SECRET_NAMES or leaf.endswith((".pyc", ".pyo")):
        return True
    if leaf.startswith(".env.") or leaf.endswith((".secret", ".secrets")):
        return True
    return False


def _assert_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise PackagingError(f"symlinks are not allowed: {path}")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise PackagingError(f"non-regular file is not allowed: {path}")


def _collect_tree(root: Path, destination: str) -> list[tuple[str, Path]]:
    # Check the path before resolving it.  ``Path.resolve`` would otherwise
    # hide a symlink supplied as a package root and make the result depend on
    # the caller's filesystem layout.
    if root.is_symlink() or not root.is_dir():
        raise PackagingError(f"source directory is invalid: {root}")
    root = root.resolve()
    collected: list[tuple[str, Path]] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(directories)
        files = sorted(files)
        for name in tuple(directories):
            child = current_path / name
            if child.is_symlink():
                raise PackagingError(f"symlinks are not allowed: {child}")
            relative = _safe_relative(child.relative_to(root).as_posix())
            if _excluded(relative):
                directories.remove(name)
        for name in files:
            source = current_path / name
            relative = _safe_relative(source.relative_to(root).as_posix())
            _assert_regular_file(source)
            if _excluded(relative):
                continue
            target_name = (
                f"{destination.rstrip('/')}/{relative.as_posix()}"
                if destination
                else relative.as_posix()
            )
            target = _safe_relative(target_name).as_posix()
            collected.append((target, source))
    return collected


def _collect_file(source: Path, target: str) -> list[tuple[str, Path]]:
    _assert_regular_file(source)
    return [(_safe_relative(target).as_posix(), source)]


def _merge(entries: Iterable[tuple[str, Path]]) -> list[tuple[str, Path]]:
    result: dict[str, Path] = {}
    normalized: dict[str, str] = {}
    for target, source in entries:
        if target in result:
            raise PackagingError(f"duplicate artifact path: {target}")
        folded = target.casefold()
        if folded in normalized:
            raise PackagingError(f"duplicate artifact path: {target}")
        normalized[folded] = target
        result[target] = source
    return sorted(result.items(), key=lambda item: item[0])


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_zip(output: Path, package: str, entries: list[tuple[str, Path]]) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    file_manifest: list[dict[str, object]] = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for target, source in entries:
            data = source.read_bytes()
            info = zipfile.ZipInfo(target, date_time=ZIP_EPOCH)
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            file_manifest.append({"path": target, "size": len(data), "sha256": _sha256_bytes(data)})
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "name": package,
        "path": output.name,
        "sha256": digest,
        "files": file_manifest,
    }


def build_release(*, repo_root: Path, dependency_root: Path, output_dir: Path) -> dict[str, object]:
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    app_entries = _merge(
        [
            *_collect_tree(repo_root / "backend" / "src" / "legaldesk", "legaldesk"),
            *_collect_tree(repo_root / "agent" / "src" / "legaldesk_agent", "legaldesk_agent"),
            *_collect_file(repo_root / "prompts" / "legaldesk-system.md", "prompts/legaldesk-system.md"),
            *_collect_tree(dependency_root, ""),
        ]
    )
    frontend_entries = _merge(
        entry
        for name in FRONTEND_FILES
        for entry in _collect_file(repo_root / "frontend" / name, name)
    )
    application = _build_zip(output_dir / "legaldesk-lambda.zip", "application", app_entries)
    frontend = _build_zip(output_dir / "legaldesk-frontend.zip", "frontend", frontend_entries)
    manifest = {
        "schemaVersion": 1,
        "runtime": RUNTIME,
        "platform": PLATFORM,
        "artifacts": [application, frontend],
    }
    manifest_path = output_dir / "legaldesk-release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependency-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    manifest = build_release(repo_root=args.repo_root, dependency_root=args.dependency_root, output_dir=args.output_dir)
    print(json.dumps(manifest, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
