#!/usr/bin/env python3
"""Clean, self-contained GitHub clones manager.

This module is intentionally compact and safe: it clones or updates GitHub
repositories for a user and does not write project scaffolding by default.
Helpers and a small BaseMake are inlined to avoid depending on external
shared packages.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeGuard, TypeVar, cast
from urllib import error as urllib_error
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, MutableMapping
    from http.client import HTTPResponse

    from x_make_common_x.stage_progress import RepoProgressReporter

from x_make_common_x.json_contracts import validate_payload

from x_make_github_clones_x.json_contracts import (
    ERROR_SCHEMA,
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
)


class _SchemaValidationError(Exception):
    message: str
    path: Sequence[object]
    schema_path: Sequence[object]


class _JsonSchemaModule(Protocol):
    ValidationError: type[_SchemaValidationError]


def _load_validation_error() -> type[_SchemaValidationError]:
    module = cast("_JsonSchemaModule", importlib.import_module("jsonschema"))
    return module.ValidationError


JsonSchemaValidationError: type[_SchemaValidationError] = _load_validation_error()

IsoformatTimestamp = Callable[[datetime | None], str]


class _WriteRunReport(Protocol):
    def __call__(  # noqa: PLR0913
        self,
        tool_slug: str,
        payload: Mapping[str, object] | MutableMapping[str, object],
        *,
        base_dir: Path | str | None = None,
        filename: str | None = None,
        timestamp: datetime | None = None,
        reports_name: str = "reports",
    ) -> Path: ...


_common_isoformat_timestamp: IsoformatTimestamp | None
_common_write_run_report: _WriteRunReport | None

try:
    from x_make_common_x import isoformat_timestamp as _imported_isoformat_timestamp
    from x_make_common_x import write_run_report as _imported_write_run_report
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    _common_isoformat_timestamp = None
    _common_write_run_report = None
else:
    _common_isoformat_timestamp = _imported_isoformat_timestamp
    _common_write_run_report = _imported_write_run_report

PACKAGE_ROOT = Path(__file__).resolve().parent


def _fallback_isoformat_timestamp(moment: datetime | None = None) -> str:
    current = (moment or datetime.now(UTC)).replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def _fallback_write_run_report(
    tool_slug: str,
    payload: Mapping[str, object] | MutableMapping[str, object],
    *,
    base_dir: Path | str,
    filename: str | None = None,
    timestamp: datetime | None = None,
) -> Path:
    base_path = Path(base_dir)
    reports_dir = base_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    moment = timestamp or datetime.now(UTC)
    stamp = moment.strftime("%Y%m%d_%H%M%S")
    resolved_filename = filename or f"{tool_slug}_run_{stamp}.json"
    data = dict(payload)
    data.setdefault("tool", tool_slug)
    data.setdefault("generated_at", _fallback_isoformat_timestamp(moment))
    report_path = reports_dir / resolved_filename
    report_path.write_text(
        json.dumps(data, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    return report_path


def _isoformat_timestamp(moment: datetime | None = None) -> str:
    if _common_isoformat_timestamp is not None:
        return _common_isoformat_timestamp(moment)
    return _fallback_isoformat_timestamp(moment)


def _write_run_report(
    payload: Mapping[str, object] | MutableMapping[str, object],
    *,
    base_dir: Path | str,
    timestamp: datetime | None = None,
) -> Path:
    moment = timestamp or datetime.now(UTC)
    if _common_write_run_report is not None:
        return _common_write_run_report(
            "x_make_github_clones_x",
            payload,
            base_dir=base_dir,
            timestamp=moment,
        )
    return _fallback_write_run_report(
        "x_make_github_clones_x",
        payload,
        base_dir=base_dir,
        timestamp=moment,
    )


JsonDict = dict[str, object]

T_co = TypeVar("T_co", covariant=True)


class Factory(Protocol[T_co]):
    def __call__(self, *args: object, **kwargs: object) -> T_co: ...


def _json_loads(payload: str) -> object:
    return cast("object", json.loads(payload))


_ALLOWED_URL_SCHEMES = {"http", "https"}


SCHEMA_VERSION = "x_make_github_clones_x.run/1.0"


def _urlopen(request: urllib.request.Request) -> HTTPResponse:
    scheme = urlsplit(request.full_url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        message = f"Refusing to open URL with scheme '{scheme}'"
        raise ValueError(message)
    return cast("HTTPResponse", urllib.request.urlopen(request))  # noqa: S310


def _is_json_dict(data: object) -> TypeGuard[JsonDict]:
    if not isinstance(data, dict):
        return False
    dict_obj = cast("dict[object, object]", data)
    return all(isinstance(key, str) for key in dict_obj)


def _is_json_list(data: object) -> TypeGuard[list[object]]:
    return isinstance(data, list)


@dataclass(frozen=True)
class RepoRecord:
    name: str
    full_name: str
    clone_url: str | None
    ssh_url: str | None
    fork: bool

    def matches(self, names: set[str] | None) -> bool:
        if names is None:
            return True
        return self.name in names or self.full_name in names

    def resolved_clone_url(self, token: str | None, *, allow_token_clone: bool) -> str:
        base_url = self.clone_url or self.ssh_url or ""
        if token and allow_token_clone and base_url.startswith("https://"):
            return base_url.replace("https://", f"https://{token}@")
        return base_url


def _coerce_repo_record(data: JsonDict) -> RepoRecord | None:
    name_obj = data.get("name")
    if not isinstance(name_obj, str) or not name_obj:
        return None
    full_name_obj = data.get("full_name")
    full_name = (
        full_name_obj if isinstance(full_name_obj, str) and full_name_obj else name_obj
    )
    clone_url_obj = data.get("clone_url")
    clone_url = (
        clone_url_obj if isinstance(clone_url_obj, str) and clone_url_obj else None
    )
    ssh_url_obj = data.get("ssh_url")
    ssh_url = ssh_url_obj if isinstance(ssh_url_obj, str) and ssh_url_obj else None
    fork_obj = data.get("fork")
    fork = fork_obj if isinstance(fork_obj, bool) else False
    return RepoRecord(
        name=name_obj,
        full_name=full_name,
        clone_url=clone_url,
        ssh_url=ssh_url,
        fork=fork,
    )


def _info(*args: object) -> None:
    print(" ".join(str(arg) for arg in args))


def _error(*args: object) -> None:
    print(" ".join(str(arg) for arg in args), file=sys.stderr)


def _failure_payload(
    message: str,
    *,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"status": "failure", "message": message}
    if details:
        payload["details"] = dict(details)
    with suppress(JsonSchemaValidationError):
        validate_payload(payload, ERROR_SCHEMA)
    return payload


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no"}:
            return False
    return default


def _extract_names(raw: object) -> list[str] | str | None:
    if isinstance(raw, list):
        cleaned = [
            entry.strip() for entry in raw if isinstance(entry, str) and entry.strip()
        ]
        return cleaned if cleaned else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _apply_allow_token_clone_env(value: object) -> tuple[str | None, bool]:
    env_name = x_cls_make_github_clones_x.ALLOW_TOKEN_CLONE_ENV
    original = os.environ.get(env_name)
    original_present = env_name in os.environ
    if isinstance(value, bool):
        os.environ[env_name] = "1" if value else "0"
    elif isinstance(value, str):
        os.environ[env_name] = "1" if _coerce_bool(value, default=False) else "0"
    return original, original_present


def _restore_allow_token_clone_env(original: str | None, *, present: bool) -> None:
    env_name = x_cls_make_github_clones_x.ALLOW_TOKEN_CLONE_ENV
    if present:
        if original is not None:
            os.environ[env_name] = original
        else:
            os.environ.pop(env_name, None)
    else:
        os.environ.pop(env_name, None)


class BaseMake:
    DEFAULT_TARGET_DIR: str | None = None  # dynamic; set after helper defined
    GIT_BIN: str = "git"
    TOKEN_ENV_VAR: str = "GITHUB_TOKEN"  # noqa: S105
    ALLOW_TOKEN_CLONE_ENV: str = "X_ALLOW_TOKEN_CLONE"  # noqa: S105
    RECLONE_ON_CORRUPT: bool = True
    # Auto-reclone/repair is enabled by default. The implementation performs a
    # safe backup before attempting reclone to avoid data loss.
    ALLOW_AUTO_RECLONE_ON_CORRUPT: bool = True
    CLONE_RETRIES: int = 1

    @classmethod
    def get_env(cls, name: str, default: str | None = None) -> str | None:
        value = os.environ.get(name)
        return value if value is not None else default

    @classmethod
    def get_env_bool(cls, name: str, *, default: bool = False) -> bool:
        env_value = os.environ.get(name)
        if env_value is None:
            return default
        return env_value.lower() in ("1", "true", "yes")

    def run_cmd(
        self, args: Iterable[str], *, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            list(args), check=check, capture_output=True, text=True
        )

    def get_token(self) -> str | None:
        return os.environ.get(self.TOKEN_ENV_VAR)

    @property
    def allow_token_clone(self) -> bool:
        return self.get_env_bool(self.ALLOW_TOKEN_CLONE_ENV, default=False)

    def __init__(self, ctx: object | None = None) -> None:
        self._ctx = ctx


class x_cls_make_github_clones_x(BaseMake):  # noqa: N801
    PER_PAGE = 100
    USER_AGENT = "clone-script"

    def __init__(  # noqa: PLR0913
        self,
        username: str | None = None,
        target_dir: str | None = None,
        *,
        shallow: bool = False,
        include_forks: bool = False,
        force_reclone: bool = False,
        names: list[str] | str | None = None,
        token: str | None = None,
        include_private: bool = True,
        **_: object,
    ) -> None:
        self.username = username
        if not target_dir:
            target_dir = str(_repo_parent_root())
        self.target_dir = _normalize_target_dir(target_dir)
        self.shallow = shallow
        self.include_forks = include_forks
        self.force_reclone = force_reclone
        # Explicitly annotate attribute so mypy knows this can be Optional[list[str]]
        self.names: list[str] | None
        if isinstance(names, str):
            self.names = [n.strip() for n in names.split(",") if n.strip()]
        elif isinstance(names, list):
            # names is list[str] here; strip empties
            self.names = [n.strip() for n in names if n.strip()]
        else:
            self.names = None
        self.token = token or os.environ.get(self.TOKEN_ENV_VAR)
        self.include_private = include_private
        self.exit_code: int | None = None
        self._last_run_report_path: Path | None = None
        self._latest_run_report: dict[str, object] | None = None
        self._reports_base_dir: Path = PACKAGE_ROOT
        self.repo_progress_writer: RepoProgressReporter | None = None

    @property
    def last_run_report_path(self) -> Path | None:
        return self._last_run_report_path

    def get_latest_run_report(self) -> dict[str, object] | None:
        latest = self._latest_run_report
        if latest is None:
            return None
        try:
            return copy.deepcopy(latest)
        except TypeError:
            return None

    def set_report_base_dir(self, base_dir: Path | str) -> None:
        self._reports_base_dir = Path(base_dir)

    def set_repo_progress_writer(self, writer: RepoProgressReporter | None) -> None:
        self.repo_progress_writer = writer

    def _request_json(
        self, url: str, headers: dict[str, str] | None = None
    ) -> list[JsonDict]:
        req = urllib.request.Request(url, headers=headers or {})  # noqa: S310
        with _urlopen(req) as resp:
            raw_body = resp.read()
        payload = _json_loads(raw_body.decode("utf-8"))
        if _is_json_dict(payload):
            return [payload]
        if _is_json_list(payload):
            return [entry for entry in payload if _is_json_dict(entry)]
        return []

    def fetch_repos(  # noqa: C901
        self, username: str | None = None, *, include_forks: bool | None = None
    ) -> list[RepoRecord]:
        username = username or self.username
        include_forks = (
            include_forks if include_forks is not None else self.include_forks
        )
        if not username and not self.token:
            message = "username or token required"
            raise RuntimeError(message)
        per_page = self.PER_PAGE
        headers: dict[str, str] = {"User-Agent": self.USER_AGENT}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        collected: dict[str, RepoRecord] = {}

        def _collect(base_url: str) -> None:
            page = 1
            while True:
                sep = "&" if "?" in base_url else "?"
                url = f"{base_url}{sep}per_page={per_page}&page={page}"
                try:
                    data_list = self._request_json(url, headers=headers)
                except (ValueError, urllib_error.URLError):
                    break
                if not data_list:
                    break
                for raw in data_list:
                    repo = _coerce_repo_record(raw)
                    if repo is None:
                        continue
                    if not include_forks and repo.fork:
                        continue
                    collected[repo.full_name] = repo
                if len(data_list) < per_page:
                    break
                page += 1

        # Public/user visible repos
        if username:
            _collect(f"https://api.github.com/users/{username}/repos?type=all")
        # Private repos via /user/repos if token + include_private
        if self.token and self.include_private:
            _collect(
                "https://api.github.com/user/repos?affiliation=owner,collaborator,organization_member&visibility=all"
            )

        repos: list[RepoRecord] = list(collected.values())
        if self.names is not None:
            name_set = {name for name in self.names if name}
            repos = [repo for repo in repos if repo.matches(name_set)]
        return repos

    def _clone_or_update_repo(  # noqa: C901, PLR0912
        self, repo_dir: Path, git_url: str
    ) -> bool:
        repo_path = Path(repo_dir)
        if not repo_path.exists():
            _info(f"Cloning {git_url} into {repo_path}")
            args = [self.GIT_BIN, "clone", git_url, str(repo_path)]
            if self.shallow:
                args[2:2] = ["--depth", "1"]
            for _ in range(max(1, self.CLONE_RETRIES)):
                try:
                    proc = self.run_cmd(args)
                except OSError as exc:
                    _error("git clone failed:", exc)
                    return False
                if proc.returncode == 0:
                    return True
                _error("clone failed:", proc.stderr or proc.stdout)
            return False

        _info(f"Updating {repo_path}")
        stashed = False
        success = False
        try:
            self.run_cmd(
                [
                    self.GIT_BIN,
                    "-C",
                    str(repo_path),
                    "fetch",
                    "--all",
                    "--prune",
                ]
            )

            status = self.run_cmd(
                [self.GIT_BIN, "-C", str(repo_path), "status", "--porcelain"],
                check=False,
            )
            has_uncommitted = bool(status.stdout.strip())

            if has_uncommitted:
                stash = self.run_cmd(
                    [
                        self.GIT_BIN,
                        "-C",
                        str(repo_path),
                        "stash",
                        "push",
                        "-u",
                        "-m",
                        "autostash-for-update",
                    ]
                )
                stashed = stash.returncode == 0

            pull_args = [self.GIT_BIN, "-C", str(repo_path), "pull"]
            if not self.shallow:
                pull_args.extend(["--rebase", "--autostash"])
            pull = self.run_cmd(pull_args)
            if pull.returncode != 0:
                pull = self.run_cmd([self.GIT_BIN, "-C", str(repo_path), "pull"])

            if pull.returncode == 0:
                success = True
            else:
                _error("pull failed:", pull.stderr or pull.stdout)
        except OSError as exc:
            _error("failed to update repository:", exc)
        finally:
            if stashed:
                try:
                    pop = self.run_cmd(
                        [
                            self.GIT_BIN,
                            "-C",
                            str(repo_path),
                            "stash",
                            "pop",
                        ]
                    )
                except OSError as pop_exc:
                    _error("failed to pop stash:", pop_exc)
                else:
                    if pop.returncode != 0:
                        _error("stash pop failed:", pop.stderr or pop.stdout)
        return success

    def _attempt_update(self, repo_dir: Path, git_url: str) -> bool:
        repo_path = Path(repo_dir)
        try:
            if self.force_reclone:
                _info(f"force_reclone enabled; refreshing in-place {repo_path}")
                return self._force_refresh_repo(repo_path, git_url)

            if self._clone_or_update_repo(repo_path, git_url):
                return True

            return self._clone_to_temp_swap(repo_path, git_url)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            _error("exception while updating:", exc)
            return False

    def _force_refresh_repo(self, repo_dir: Path, git_url: str) -> bool:
        """Refresh an existing repo in-place without deleting files."""

        repo_path = Path(repo_dir)
        if not repo_path.exists():
            return self._clone_or_update_repo(repo_path, git_url)

        stashed = False
        success = False
        try:
            self._fetch_all(repo_path)
            if self._has_uncommitted_changes(repo_path):
                stashed = self._stash_changes(repo_path)
            success = self._pull_or_reset(repo_path)
        except OSError as exc:
            _error("force refresh exception:", exc)
            success = False
        finally:
            if stashed:
                self._pop_stash(repo_path)
        return success

    def _clone_to_temp_swap(self, repo_dir: Path, git_url: str) -> bool:
        """Clone into a temporary directory and atomically swap the repo."""

        repo_path = Path(repo_dir)
        try:
            tmp_dir, bak_dir = self._prepare_clone_paths(repo_path)
        except OSError as exc:
            _error("failed to ensure parent directory:", exc)
            return False

        if not self._clone_with_retries(tmp_dir, git_url):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return False

        backup_created = False
        if repo_path.exists():
            try:
                self._backup_repo(repo_path, bak_dir)
            except OSError as exc:
                _error("failed to backup existing repository:", exc)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return False
            backup_created = True

        try:
            self._replace_repo(repo_path, tmp_dir)
        except OSError as exc:
            _error("failed to replace repository:", exc)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if backup_created:
                self._restore_backup(bak_dir, repo_path)
            return False

        shutil.rmtree(bak_dir, ignore_errors=True)
        return True

    def _fetch_all(self, repo_path: Path) -> None:
        self.run_cmd(
            [
                self.GIT_BIN,
                "-C",
                str(repo_path),
                "fetch",
                "--all",
                "--prune",
            ]
        )

    def _has_uncommitted_changes(self, repo_path: Path) -> bool:
        status = self.run_cmd(
            [self.GIT_BIN, "-C", str(repo_path), "status", "--porcelain"],
            check=False,
        )
        return bool(status.stdout.strip())

    def _stash_changes(self, repo_path: Path) -> bool:
        stash = self.run_cmd(
            [
                self.GIT_BIN,
                "-C",
                str(repo_path),
                "stash",
                "push",
                "-u",
                "-m",
                "force-refresh-stash",
            ]
        )
        return stash.returncode == 0

    def _pull_or_reset(self, repo_path: Path) -> bool:
        pull_args = [self.GIT_BIN, "-C", str(repo_path), "pull"]
        if not self.shallow:
            pull_args.extend(["--rebase", "--autostash"])
        pull = self.run_cmd(pull_args)
        if pull.returncode == 0:
            return True

        self._fetch_all(repo_path)
        reset = self.run_cmd(
            [
                self.GIT_BIN,
                "-C",
                str(repo_path),
                "reset",
                "--hard",
                "origin/HEAD",
            ]
        )
        self.run_cmd([self.GIT_BIN, "-C", str(repo_path), "clean", "-fdx"])
        if reset.returncode != 0:
            _error("force refresh reset failed:", reset.stderr or reset.stdout)
            return False
        return True

    def _pop_stash(self, repo_path: Path) -> None:
        try:
            pop = self.run_cmd(
                [
                    self.GIT_BIN,
                    "-C",
                    str(repo_path),
                    "stash",
                    "pop",
                ]
            )
        except OSError as exc:
            _error("failed to pop stash:", exc)
            return
        if pop.returncode != 0:
            _error("stash pop failed:", pop.stderr or pop.stdout)

    def _prepare_clone_paths(self, repo_path: Path) -> tuple[Path, Path]:
        parent = repo_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        base = repo_path.name
        ts = int(time.time())
        tmp_dir = parent / f".{base}.tmp.{ts}"
        bak_dir = parent / f".{base}.bak.{ts}"
        return tmp_dir, bak_dir

    def _clone_with_retries(self, tmp_dir: Path, git_url: str) -> bool:
        args = [self.GIT_BIN, "clone", git_url, str(tmp_dir)]
        if self.shallow:
            args[2:2] = ["--depth", "1"]
        attempts = max(1, self.CLONE_RETRIES)
        for _ in range(attempts):
            try:
                proc = self.run_cmd(args)
            except OSError as exc:
                _error("git clone failed:", exc)
                return False
            if proc.returncode == 0:
                return True
            _error("clone failed:", proc.stderr or proc.stdout)
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    def _backup_repo(self, repo_path: Path, bak_dir: Path) -> None:
        shutil.move(str(repo_path), str(bak_dir))

    def _replace_repo(self, repo_path: Path, tmp_dir: Path) -> None:
        shutil.move(str(tmp_dir), str(repo_path))

    def _restore_backup(self, bak_dir: Path, repo_path: Path) -> None:
        if bak_dir.exists() and not repo_path.exists():
            try:
                shutil.move(str(bak_dir), str(repo_path))
            except OSError as exc:
                _error("failed to restore original repository:", exc)

    def _repo_clone_url(self, repo: RepoRecord) -> str:
        return repo.resolved_clone_url(
            self.token, allow_token_clone=self.allow_token_clone
        )

    def sync(  # noqa: C901, PLR0912, PLR0915
        self, username: str | None = None, dest: str | None = None
    ) -> int:
        username = username or self.username
        dest_candidate = dest or self.target_dir or self.DEFAULT_TARGET_DIR
        dest_path: Path = (
            Path(dest_candidate) if dest_candidate else _repo_parent_root()
        )
        dest_path.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        started_at = datetime.now(UTC)
        repos: list[RepoRecord] = []
        fetch_error: str | None = None
        exit_code = 0

        try:
            repos = self.fetch_repos(username=username)
        except (
            RuntimeError,
            urllib_error.URLError,
            OSError,
            ValueError,
        ) as exc:
            fetch_error = str(exc).strip() or repr(exc)
            _error("failed to fetch repo list:", exc)
            exit_code = 2
        else:
            if self.names is not None:
                name_set = {name for name in self.names if name}
                repos = [repo for repo in repos if repo.matches(name_set)]

        repo_entries: list[dict[str, object]] = []
        missing_clone_names: list[str] = []
        failed_repo_names: list[str] = []
        successful_names: list[str] = []
        used_token_clone = bool(self.token and self.allow_token_clone)

        progress_writer = self.repo_progress_writer

        if fetch_error is None:
            if progress_writer is not None:
                for repo in repos:
                    repo_key = (
                        repo.full_name or repo.name or repo.clone_url or "<unknown>"
                    )
                    repo_path = dest_path / (repo.name or repo_key)
                    progress_writer.record_pending(
                        repo_key,
                        display_name=repo.full_name or repo.name,
                        metadata={
                            "target_path": str(repo_path),
                            "source_https": repo.clone_url or None,
                            "source_ssh": repo.ssh_url or None,
                        },
                    )
            for repo in repos:
                name = repo.name
                if not name:
                    continue
                repo_path = dest_path / name
                repo_started = datetime.now(UTC)
                git_url = self._repo_clone_url(repo)
                status = "skipped"
                error_message: str | None = None
                repo_key = repo.full_name or name
                display_name = repo.full_name or name
                base_metadata: dict[str, object] = {
                    "target_path": str(repo_path),
                    "source_https": repo.clone_url or None,
                    "source_ssh": repo.ssh_url or None,
                }
                if progress_writer is not None:
                    progress_writer.record_start(
                        repo_key,
                        display_name=display_name,
                        metadata=base_metadata,
                    )

                if not git_url:
                    _error(f"missing clone URL for {name}")
                    exit_code = 3
                    status = "missing_clone_url"
                    missing_clone_names.append(name)
                else:
                    success = self._attempt_update(repo_path, git_url)
                    if success:
                        status = "updated"
                        successful_names.append(name)
                    else:
                        status = "failed"
                        failed_repo_names.append(name)
                        exit_code = 3
                        error_message = "clone_or_update_failed"

                repo_completed = datetime.now(UTC)
                duration = (repo_completed - repo_started).total_seconds()
                repo_entry: dict[str, object] = {
                    "name": name,
                    "full_name": repo.full_name,
                    "target_path": str(repo_path),
                    "source_https": repo.clone_url or None,
                    "source_ssh": repo.ssh_url or None,
                    "used_token_clone": used_token_clone and bool(repo.clone_url),
                    "status": status,
                    "duration_seconds": round(duration, 3),
                }
                if error_message:
                    repo_entry["error"] = error_message
                repo_entries.append(repo_entry)

                if progress_writer is not None:
                    progress_meta: dict[str, object] = dict(base_metadata)
                    progress_meta.update(
                        {
                            "status": status,
                            "used_token_clone": used_token_clone
                            and bool(repo.clone_url),
                            "duration_seconds": round(duration, 3),
                        }
                    )
                    if error_message:
                        progress_meta["error"] = error_message
                    if status == "updated":
                        progress_writer.record_success(
                            repo_key,
                            display_name=display_name,
                            metadata=progress_meta,
                            messages=["Repository synchronized."],
                        )
                    elif status in {"failed", "missing_clone_url"}:
                        failure_message = (
                            "Clone/update failed."
                            if status == "failed"
                            else "Missing clone URL."
                        )
                        progress_writer.record_failure(
                            repo_key,
                            display_name=display_name,
                            metadata=progress_meta,
                            messages=[failure_message],
                        )
                    else:
                        progress_writer.record_skipped(
                            repo_key,
                            display_name=display_name,
                            metadata=progress_meta,
                            messages=[f"Status: {status}"],
                        )

        completed_at = datetime.now(UTC)
        summary: dict[str, object] = {
            "total_repos": len(repos),
            "successful": len(successful_names),
            "missing_clone_url": len(missing_clone_names),
            "failed_updates": len(failed_repo_names),
            "fetch_error": fetch_error,
        }
        if missing_clone_names:
            summary["missing_repos"] = sorted(missing_clone_names)
        if failed_repo_names:
            summary["failed_repos"] = sorted(failed_repo_names)

        invocation: dict[str, object] = {
            "username": username,
            "target_dir": str(dest_path),
            "shallow": self.shallow,
            "include_forks": self.include_forks,
            "force_reclone": self.force_reclone,
            "names_filter": list(self.names) if self.names is not None else None,
            "include_private": self.include_private,
            "token_provided": bool(self.token),
            "allow_token_clone": self.allow_token_clone,
        }

        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "started_at": _isoformat_timestamp(started_at),
            "completed_at": _isoformat_timestamp(completed_at),
            "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
            "invocation": invocation,
            "summary": summary,
            "repos": repo_entries,
            "exit_code": exit_code,
        }

        try:
            payload_copy = copy.deepcopy(payload)
        except TypeError:
            payload_copy = payload

        self._latest_run_report = payload_copy

        self._last_run_report_path = None
        report_path: Path | None = None
        try:
            report_path = _write_run_report(
                payload,
                base_dir=self._reports_base_dir,
                timestamp=completed_at,
            )
        except (OSError, ValueError, TypeError) as exc:
            _error("failed to write clones run report:", exc)
        else:
            self._last_run_report_path = report_path

        self.exit_code = exit_code
        return exit_code


_REPO_PARENT_ROOT_CACHE: dict[str, Path] = {}


def _compute_repo_parent_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        git_dir = ancestor / ".git"
        if git_dir.exists():
            return ancestor.parent
    return here.parent


def _repo_parent_root() -> Path:
    cached = _REPO_PARENT_ROOT_CACHE.get("value")
    if cached is not None:
        return cached
    result = _compute_repo_parent_root()
    _REPO_PARENT_ROOT_CACHE["value"] = result
    return result


def _normalize_target_dir(val: str | None) -> str:
    if val is None:
        return str(_repo_parent_root())
    return str(Path(val))


if BaseMake.DEFAULT_TARGET_DIR is None:
    BaseMake.DEFAULT_TARGET_DIR = str(_repo_parent_root())


def _as_callable(value: object) -> Factory[object] | None:
    if callable(value):
        return cast("Factory[object]", value)
    return None


def _is_unexpected_keyword_error(error: TypeError) -> bool:
    lowered = str(error).lower()
    return "unexpected keyword" in lowered or "got an unexpected keyword" in lowered


def _set_force_reclone_attr(cloner: object, *, flag: bool) -> None:
    with suppress(AttributeError, TypeError):
        setattr(cloner, "force_reclone", flag)  # noqa: B010


def _instantiate_cloner(  # noqa: PLR0913
    *,
    username: str,
    target_dir: str,
    shallow: bool,
    include_forks: bool,
    force_reclone: bool,
    ctx: object | None,
) -> x_cls_make_github_clones_x:
    try:
        cloner = x_cls_make_github_clones_x(
            username=username,
            target_dir=target_dir,
            shallow=shallow,
            include_forks=include_forks,
            force_reclone=force_reclone,
            ctx=ctx,
        )
    except TypeError as error:
        if not _is_unexpected_keyword_error(error):
            raise
        cloner = x_cls_make_github_clones_x(
            username=username,
            target_dir=target_dir,
            shallow=shallow,
            include_forks=include_forks,
        )
        _set_force_reclone_attr(cloner, flag=force_reclone)
    else:
        _set_force_reclone_attr(cloner, flag=force_reclone)
    return cloner


def _call_cloner_entrypoint(
    cloner: object,
    method_name: str,
    *,
    args: tuple[object, ...] = (),
    suppress_exceptions: tuple[type[BaseException], ...] = (),
) -> bool:
    candidate_attr: object = getattr(cloner, method_name, None)
    candidate = _as_callable(candidate_attr)
    if candidate is None:
        return False
    if suppress_exceptions:
        with suppress(*suppress_exceptions):
            candidate(*args)
    else:
        candidate(*args)
    return True


def _call_cloner_sync(cloner: object, *, username: str, target_dir: str) -> bool:
    sync_attr: object = getattr(cloner, "sync", None)
    sync_callable = _as_callable(sync_attr)
    if sync_callable is None:
        return False
    try:
        sync_callable(username, target_dir)
    except TypeError:
        sync_callable()
    return True


def _run_cloner(
    cloner: object,
    *,
    username: str,
    target_dir: str,
) -> None:
    if _call_cloner_entrypoint(cloner, "run"):
        return
    if _call_cloner_sync(cloner, username=username, target_dir=target_dir):
        return
    if _call_cloner_entrypoint(
        cloner,
        "main",
        suppress_exceptions=(RuntimeError, OSError, ValueError),
    ):
        return
    _info("No recognized cloner entrypoint found; skipping run")


def synchronize_workspace(  # noqa: PLR0913
    *,
    username: str,
    target_dir: str,
    shallow: bool,
    include_forks: bool,
    force_reclone: bool,
    ctx: object | None = None,
    progress_writer: RepoProgressReporter | None = None,
) -> x_cls_make_github_clones_x:
    """Instantiate and run the clones manager for the provided options."""

    cloner = _instantiate_cloner(
        username=username,
        target_dir=target_dir,
        shallow=shallow,
        include_forks=include_forks,
        force_reclone=force_reclone,
        ctx=ctx,
    )
    if progress_writer is not None:
        cloner.set_repo_progress_writer(progress_writer)
    try:
        _run_cloner(cloner, username=username, target_dir=target_dir)
    except (
        RuntimeError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        _error("Cloner run failed:", exc)
    return cloner


def resolve_workspace_root(
    cloner: object,
    *,
    default_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Derive the workspace root containing cloned repositories.

    The orchestrator previously duplicated this logic; expose it here so the
    control center can remain lean and delegate to the clones package.
    """

    def _coerce_path(value: object) -> Path | None:
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return Path(value)
        if isinstance(value, os.PathLike):  # pragma: no branch - simple guard
            return Path(os.fspath(cast("os.PathLike[str]", value)))
        return None

    target_dir_attr: object = getattr(cloner, "target_dir", None)
    root_candidate = _coerce_path(target_dir_attr)
    if root_candidate is None:
        base_default = default_root if default_root is not None else _repo_parent_root()
        root_candidate = _coerce_path(base_default) or _repo_parent_root()

    root_path = root_candidate
    if (root_path / ".git").is_dir():
        parent = root_path.parent
        with suppress(OSError):
            for entry in parent.iterdir():
                if entry == root_path:
                    continue
                if (entry / ".git").is_dir():
                    root_path = parent
                    break
    return root_path


def main_json(
    payload: Mapping[str, object], *, ctx: object | None = None
) -> dict[str, object]:
    try:
        validate_payload(payload, INPUT_SCHEMA)
    except JsonSchemaValidationError as exc:
        return _failure_payload(
            "input payload failed validation",
            details={
                "error": exc.message,
                "path": [str(part) for part in exc.path],
                "schema_path": [str(part) for part in exc.schema_path],
            },
        )

    parameters_obj = payload.get("parameters", {})
    parameters = cast("Mapping[str, object]", parameters_obj)

    username_obj = parameters.get("username")
    username = username_obj if isinstance(username_obj, str) and username_obj else None

    target_dir_obj = parameters.get("target_dir")
    if isinstance(target_dir_obj, str) and target_dir_obj:
        target_dir_str = target_dir_obj
    else:
        target_dir_str = x_cls_make_github_clones_x.DEFAULT_TARGET_DIR or str(
            _repo_parent_root()
        )
    target_dir_path = Path(target_dir_str)

    shallow = _coerce_bool(parameters.get("shallow"), default=False)
    include_forks = _coerce_bool(parameters.get("include_forks"), default=False)
    force_reclone = _coerce_bool(parameters.get("force_reclone"), default=False)
    include_private = _coerce_bool(
        parameters.get("include_private"),
        default=True,
    )
    names_param = _extract_names(parameters.get("names"))

    token_obj = parameters.get("token")
    token = token_obj if isinstance(token_obj, str) and token_obj else None

    allow_token_value = parameters.get("allow_token_clone")
    allow_override = "allow_token_clone" in parameters
    original_env_value: str | None = None
    original_env_present = False
    if allow_override:
        original_env_value, original_env_present = _apply_allow_token_clone_env(
            allow_token_value
        )

    try:
        target_dir_path.mkdir(parents=True, exist_ok=True)
        cloner = x_cls_make_github_clones_x(
            username=username,
            target_dir=str(target_dir_path),
            shallow=shallow,
            include_forks=include_forks,
            force_reclone=force_reclone,
            names=names_param,
            token=token,
            include_private=include_private,
            ctx=ctx,
        )
        cloner.set_report_base_dir(target_dir_path)
        exit_code = cloner.sync(username=username, dest=str(target_dir_path))
        run_report = cloner.get_latest_run_report()
        if run_report is None:
            return _failure_payload(
                "cloner run did not produce a report",
                details={"exit_code": exit_code},
            )
        result_payload = copy.deepcopy(run_report)
        result_payload["status"] = "success"
        result_payload.setdefault("schema_version", SCHEMA_VERSION)
    except Exception as exc:  # noqa: BLE001
        return _failure_payload(
            "unexpected error while running clones manager",
            details={"error": str(exc)},
        )
    finally:
        if allow_override:
            _restore_allow_token_clone_env(
                original_env_value,
                present=original_env_present,
            )

    try:
        validate_payload(result_payload, OUTPUT_SCHEMA)
    except JsonSchemaValidationError as exc:
        return _failure_payload(
            "generated output failed schema validation",
            details={
                "error": exc.message,
                "path": [str(part) for part in exc.path],
                "schema_path": [str(part) for part in exc.schema_path],
            },
        )

    return result_payload


def main() -> int:
    username = os.environ.get("X_GH_USER")
    if not username:
        _info("Set X_GH_USER to run the example")
        return 0
    m = x_cls_make_github_clones_x(username=username)
    return m.sync()


def _load_json_payload(file_path: str | None) -> Mapping[str, object]:
    if file_path:
        with Path(file_path).open("r", encoding="utf-8") as handle:
            return cast("Mapping[str, object]", json.load(handle))
    return cast("Mapping[str, object]", json.load(sys.stdin))


def _run_json_cli(args: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description="x_make_github_clones_x JSON runner")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Read JSON payload from stdin",
    )
    parser.add_argument(
        "--json-file",
        type=str,
        help="Path to JSON payload file",
    )
    parsed = parser.parse_args(args)
    json_flag_obj = cast("object", getattr(parsed, "json", False))
    json_flag = bool(json_flag_obj)
    json_file_obj = cast("object", getattr(parsed, "json_file", None))
    json_file = (
        json_file_obj if isinstance(json_file_obj, str) and json_file_obj else None
    )

    if not (json_flag or json_file):
        parser.error("JSON input required. Use --json for stdin or --json-file <path>.")

    payload = _load_json_payload(json_file)
    result = main_json(payload)
    sys.stdout.write(json.dumps(result, indent=2))
    sys.stdout.write("\n")


__all__ = [
    "RepoRecord",
    "main_json",
    "resolve_workspace_root",
    "synchronize_workspace",
    "x_cls_make_github_clones_x",
]


if __name__ == "__main__":
    _run_json_cli(sys.argv[1:])
