"""Automate pushing a local repo to a fresh GitHub remote."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def _run(cmd: Sequence[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    printable = " ".join(cmd)
    print(f"$ {printable}")
    result = subprocess.run(list(cmd), check=False, cwd=cwd, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {printable}")
    return result


def _git_repo_root(path: Path) -> Path:
    current = path.resolve()
    git_dir = current / ".git"
    if git_dir.is_dir():
        return current
    raise ValueError(f"{current} is not a git repository (missing .git)")


def _git_has_remote(repo_path: Path, remote: str) -> bool:
    result = subprocess.run(
        ["git", "remote"],
        check=False, cwd=repo_path,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    remotes = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return remote in remotes


def _ensure_gh_cli() -> None:
    if shutil.which("gh") is None:
        raise RuntimeError("GitHub CLI (gh) is required but not found in PATH")


def _repo_exists_on_github(full_name: str) -> bool:
    view = subprocess.run(["gh", "repo", "view", full_name], check=False, capture_output=True, text=True)
    return view.returncode == 0


def create_remote_repo(
    repo_path: Path,
    org: str,
    new_name: str,
    visibility: str,
    *,
    replace_remote: bool,
    dry_run: bool,
) -> None:
    repo_root = _git_repo_root(repo_path)
    _ensure_gh_cli()
    full_name = f"{org}/{new_name}"
    visibility_flag = f"--{visibility}"

    if _repo_exists_on_github(full_name):
        raise RuntimeError(f"GitHub repo {full_name} already exists; aborting")

    had_origin = _git_has_remote(repo_root, "origin")
    if had_origin and not replace_remote:
        raise RuntimeError("Remote 'origin' exists. Re-run with --replace-remote to overwrite it.")

    temp_remote = None
    if had_origin:
        temp_remote = "origin_backup"
        if _git_has_remote(repo_root, temp_remote):
            raise RuntimeError(f"Cannot back up origin -> {temp_remote}; remote already exists")
        if not dry_run:
            _run(["git", "remote", "rename", "origin", temp_remote], cwd=repo_root)
        else:
            print("(dry-run) would rename origin to origin_backup")

    cmd = [
        "gh",
        "repo",
        "create",
        full_name,
        visibility_flag,
        "--source",
        str(repo_root),
        "--remote",
        "origin",
        "--push",
        "--disable-issues",
        "--disable-wiki",
        "--confirm",
    ]
    if dry_run:
        print("(dry-run) would run:", " ".join(cmd))
    else:
        _run(cmd, cwd=repo_root)

    if temp_remote and not dry_run:
        print("Previous origin saved as", temp_remote)

    print(f"New GitHub repo ready: https://github.com/{full_name}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create/push a throwaway GitHub repo using gh CLI.")
    parser.add_argument("--source", type=Path, default=Path.cwd(), help="Path to local git repository (default: cwd)")
    parser.add_argument("--org", default="ppnw-ai-corp", help="GitHub organization (default: ppnw-ai-corp)")
    parser.add_argument("--name", required=True, help="Name for the new GitHub repository")
    parser.add_argument(
        "--visibility",
        choices=["private", "public", "internal"],
        default="private",
        help="GitHub visibility for the new repo",
    )
    parser.add_argument(
        "--replace-remote",
        action="store_true",
        help="Rename existing origin to origin_backup before creating the new remote",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        create_remote_repo(
            repo_path=args.source,
            org=args.org,
            new_name=args.name,
            visibility=args.visibility,
            replace_remote=args.replace_remote,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - bubble up friendly message
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
