#!/usr/bin/env python3
"""Export `plugins/mempalace/` into a standalone, git-ready Antigravity plugin bundle.

Why this exists:
  1. In-repo, `plugins/mempalace/` is the single source of truth for the
     Google Antigravity Marketplace plugin (`agy plugin validate --strict ./plugins/mempalace`).
  2. For standalone GitHub distribution (e.g. `MemPalace/mempalace-antigravity`)
     or release zip artifacts, this script exports a lightweight (~150 KB)
     standalone repository with LF line endings, `0755` executable bits on
     `scripts/*`, `.gitattributes`, and optional `agy plugin validate --strict`
     verification — without cloning the full Python/Rust/website monorepo.

Usage:
  python hooks/antigravity/export_standalone.py --output dist/mempalace-antigravity
  python hooks/antigravity/export_standalone.py --output dist/mempalace-antigravity --init-git --zip dist/mempalace-antigravity.zip
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / "mempalace"

TEXT_EXTENSIONS = {
    ".json",
    ".md",
    ".mjs",
    ".js",
    ".css",
    ".html",
    ".py",
    ".svg",
    ".sh",
    ".yml",
    ".yaml",
}


def _find_agy_binary() -> str | None:
    """Locate `agy` or `agy.exe` on PATH or standard Windows install paths."""
    found = shutil.which("agy")
    if found:
        return found
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidate = Path(local_app) / "agy" / "bin" / "agy.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _copy_normalized(src: Path, dst: Path) -> None:
    """Copy a file, normalizing text files to UTF-8 LF line endings."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    is_text = src.suffix.lower() in TEXT_EXTENSIONS or src.name in ("LICENSE", ".gitattributes")
    if is_text:
        text = src.read_text(encoding="utf-8")
        normalized = text.replace("\r\n", "\n")
        dst.write_bytes(normalized.encode("utf-8"))
    else:
        shutil.copy2(src, dst)

    # Preserve executable bit for anything under scripts/
    if "scripts" in src.parts:
        mode = dst.stat().st_mode
        dst.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _rmtree_force(target: Path) -> None:
    """Remove directory tree cleanly even when Windows `.git/objects` are read-only."""

    def _handle_readonly(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_handle_readonly)
    else:
        shutil.rmtree(target, onerror=_handle_readonly)


def export_standalone_bundle(
    output_dir: Path,
    *,
    repo_url: str | None = None,
    clean: bool = True,
    init_git: bool = False,
    zip_path: Path | None = None,
    validate: bool = True,
) -> dict:
    """Export `plugins/mempalace/` into `output_dir` and return a summary dict."""
    if not PLUGIN_SRC.is_dir():
        raise FileNotFoundError(f"Missing plugin source directory: {PLUGIN_SRC}")

    output_dir = output_dir.resolve()
    if output_dir == REPO_ROOT or output_dir == PLUGIN_SRC:
        raise ValueError(f"Refusing to overwrite source directory: {output_dir}")

    if clean and output_dir.exists():
        _rmtree_force(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    copied_files: list[str] = []
    for src_path in sorted(PLUGIN_SRC.rglob("*")):
        if src_path.is_symlink():
            raise RuntimeError(f"Refusing to export symlink inside plugin bundle: {src_path}")
        if not src_path.is_file():
            continue
        if "__pycache__" in src_path.parts or src_path.suffix == ".pyc":
            continue
        rel = src_path.relative_to(PLUGIN_SRC)
        dst_path = output_dir / rel
        _copy_normalized(src_path, dst_path)
        copied_files.append(rel.as_posix())

    # Write .gitattributes so standalone repo checkouts preserve LF on scripts
    gitattributes = output_dir / ".gitattributes"
    gitattributes.write_bytes(
        b"# Preserve LF line endings on scripts across Windows/macOS/Linux\n"
        b"*.mjs text eol=lf\n"
        b"*.sh text eol=lf\n"
    )
    copied_files.append(".gitattributes")

    if repo_url:
        manifest_path = output_dir / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["repository"] = repo_url
        manifest_path.write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))

    if init_git:
        subprocess.run(["git", "init", "-b", "main"], cwd=str(output_dir), check=True)
        subprocess.run(["git", "add", "."], cwd=str(output_dir), check=True)
        for rel in copied_files:
            if rel.startswith("scripts/"):
                subprocess.run(
                    ["git", "update-index", "--chmod=+x", rel],
                    cwd=str(output_dir),
                    check=True,
                )

    if zip_path is not None:
        zip_path = zip_path.resolve()
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel in sorted(copied_files):
                file_path = output_dir / rel
                info = zipfile.ZipInfo(f"mempalace/{rel}")
                info.compress_type = zipfile.ZIP_DEFLATED
                perm = 0o755 if rel.startswith("scripts/") else 0o644
                info.external_attr = (stat.S_IFREG | perm) << 16
                zf.writestr(info, file_path.read_bytes())

    validation_result: dict | None = None
    if validate:
        agy_bin = _find_agy_binary()
        if agy_bin:
            proc = subprocess.run(
                [agy_bin, "plugin", "validate", "--strict", "--json", str(output_dir)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"agy plugin validate --strict failed ({proc.returncode}):\n"
                    f"{proc.stdout}\n{proc.stderr}"
                )
            validation_result = json.loads(proc.stdout)

    return {
        "output_dir": str(output_dir),
        "file_count": len(copied_files),
        "files": sorted(copied_files),
        "zip_path": str(zip_path) if zip_path else None,
        "validation": validation_result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export plugins/mempalace/ into a standalone Antigravity plugin repository."
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Destination directory for the standalone plugin bundle.",
    )
    parser.add_argument(
        "--repo-url",
        default=None,
        help="Optional repository URL override for plugin.json (e.g. https://github.com/MemPalace/mempalace-antigravity).",
    )
    parser.add_argument(
        "--init-git",
        action="store_true",
        help="Initialize a fresh `main` git repository and mark scripts/* 0755 in the index.",
    )
    parser.add_argument(
        "--zip",
        type=Path,
        default=None,
        help="Optional zip archive output path preserving 0755 permissions on scripts/*.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip running `agy plugin validate --strict` even if `agy` is installed.",
    )
    args = parser.parse_args(argv)

    summary = export_standalone_bundle(
        args.output,
        repo_url=args.repo_url,
        init_git=args.init_git,
        zip_path=args.zip,
        validate=not args.no_validate,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
