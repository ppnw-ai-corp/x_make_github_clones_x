# ruff: noqa: S101

from __future__ import annotations

import importlib
import json
import os
import typing
from pathlib import Path
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

import pytest
from x_make_common_x.json_contracts import validate_payload, validate_schema

from x_make_github_clones_x.json_contracts import (
    ERROR_SCHEMA,
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
)
from x_make_github_clones_x.x_cls_make_github_clones_x import (
    RepoRecord,
    main_json,
    x_cls_make_github_clones_x,
)

clones_module: types.ModuleType = importlib.import_module(
    "x_make_github_clones_x.x_cls_make_github_clones_x"
)

_P = ParamSpec("_P")
_T = TypeVar("_T")
if TYPE_CHECKING:
    import types
    from collections.abc import Callable

    from _pytest.monkeypatch import MonkeyPatch
else:  # pragma: no cover - runtime typing fallback
    Callable = typing.Callable
    MonkeyPatch = object


if TYPE_CHECKING:

    def typed_fixture(
        *_args: object, **_kwargs: object
    ) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]: ...

else:

    def typed_fixture(
        *args: object, **kwargs: object
    ) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
        def _decorate(func: Callable[_P, _T]) -> Callable[_P, _T]:
            decorated = pytest.fixture(*args, **kwargs)(func)
            return cast("Callable[_P, _T]", decorated)

        return _decorate


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "json_contracts"


def _load_fixture(name: str) -> dict[str, object]:
    with (FIXTURE_DIR / f"{name}.json").open("r", encoding="utf-8") as handle:
        data: object = json.load(handle)
    if not isinstance(data, dict):
        message = f"fixture '{name}' must be a JSON object"
        raise TypeError(message)
    return cast("dict[str, object]", data)


@typed_fixture(scope="module")
def sample_input() -> dict[str, object]:
    return _load_fixture("input")


@typed_fixture(scope="module")
def sample_output() -> dict[str, object]:
    return _load_fixture("output")


@typed_fixture(scope="module")
def sample_error() -> dict[str, object]:
    return _load_fixture("error")


def test_schemas_are_valid() -> None:
    for schema in (INPUT_SCHEMA, OUTPUT_SCHEMA, ERROR_SCHEMA):
        validate_schema(schema)


def test_sample_payloads_match_schema(
    sample_input: dict[str, object],
    sample_output: dict[str, object],
    sample_error: dict[str, object],
) -> None:
    validate_payload(sample_input, INPUT_SCHEMA)
    validate_payload(sample_output, OUTPUT_SCHEMA)
    validate_payload(sample_error, ERROR_SCHEMA)


def test_main_json_runs_successfully(
    sample_input: dict[str, object],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    payload = cast("dict[str, object]", json.loads(json.dumps(sample_input)))
    parameters_obj = payload.get("parameters")
    assert isinstance(parameters_obj, dict)
    parameters = cast("dict[str, object]", parameters_obj)

    target_dir = tmp_path / "clones"
    parameters["target_dir"] = str(target_dir)
    parameters["username"] = "octocat"
    parameters["names"] = ["demo"]

    repo = RepoRecord(
        name="demo",
        full_name="octocat/demo",
        clone_url="https://example.com/octocat/demo.git",
        ssh_url="git@example.com:octocat/demo.git",
        fork=False,
    )

    def fake_fetch(
        _self: object,
        username: str | None = None,
        *,
        include_forks: bool | None = None,
    ) -> list[RepoRecord]:
        _ = (username, include_forks)
        return [repo]

    def fake_attempt(_self: object, repo_dir: Path, git_url: str) -> bool:
        repo_dir.mkdir(parents=True, exist_ok=True)
        _ = git_url
        return True

    def fake_write(
        payload_data: dict[str, object],
        *,
        base_dir: Path | str,
        _timestamp: object | None = None,
    ) -> Path:
        base = Path(base_dir)
        report_path = base / "reports" / "run.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload_data), encoding="utf-8")
        return report_path

    monkeypatch.setattr(x_cls_make_github_clones_x, "fetch_repos", fake_fetch)
    monkeypatch.setattr(x_cls_make_github_clones_x, "_attempt_update", fake_attempt)
    monkeypatch.setattr(clones_module, "_write_run_report", fake_write)

    result = main_json(payload)

    validate_payload(result, OUTPUT_SCHEMA)
    status_value = result.get("status")
    assert isinstance(status_value, str)
    assert status_value == "success"

    invocation_obj = result.get("invocation")
    assert isinstance(invocation_obj, dict)
    invocation = cast("dict[str, object]", invocation_obj)
    target_dir_value = invocation.get("target_dir")
    assert isinstance(target_dir_value, str)
    assert target_dir_value == str(target_dir)

    summary_obj = result.get("summary")
    assert isinstance(summary_obj, dict)
    summary = cast("dict[str, object]", summary_obj)
    successful_value = summary.get("successful")
    assert isinstance(successful_value, int)
    assert successful_value == 1

    repos_obj = result.get("repos")
    assert isinstance(repos_obj, list)
    assert repos_obj, "repo list should not be empty"
    first_repo = repos_obj[0]
    assert isinstance(first_repo, dict)
    first_repo_data = cast("dict[str, object]", first_repo)
    status_value = first_repo_data.get("status")
    assert isinstance(status_value, str)
    assert status_value == "updated"


def test_main_json_restores_allow_token_clone_env(
    sample_input: dict[str, object],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    payload = cast("dict[str, object]", json.loads(json.dumps(sample_input)))
    parameters_obj = payload.get("parameters")
    assert isinstance(parameters_obj, dict)
    parameters = cast("dict[str, object]", parameters_obj)

    parameters["target_dir"] = str(tmp_path / "clones")
    parameters["allow_token_clone"] = False
    parameters["names"] = ["demo"]

    repo = RepoRecord(
        name="demo",
        full_name="octocat/demo",
        clone_url="https://example.com/octocat/demo.git",
        ssh_url="git@example.com:octocat/demo.git",
        fork=False,
    )

    monkeypatch.setenv(x_cls_make_github_clones_x.ALLOW_TOKEN_CLONE_ENV, "1")

    def fake_fetch(
        _self: object,
        username: str | None = None,
        *,
        include_forks: bool | None = None,
    ) -> list[RepoRecord]:
        _ = (username, include_forks)
        return [repo]

    def fake_attempt(_self: object, repo_dir: Path, git_url: str) -> bool:
        repo_dir.mkdir(parents=True, exist_ok=True)
        _ = git_url
        return True

    def fake_write(
        payload_data: dict[str, object],
        *,
        base_dir: Path | str,
        _timestamp: object | None = None,
    ) -> Path:
        base = Path(base_dir)
        report_path = base / "reports" / "run.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload_data), encoding="utf-8")
        return report_path

    monkeypatch.setattr(x_cls_make_github_clones_x, "fetch_repos", fake_fetch)
    monkeypatch.setattr(x_cls_make_github_clones_x, "_attempt_update", fake_attempt)
    monkeypatch.setattr(clones_module, "_write_run_report", fake_write)

    result = main_json(payload)

    validate_payload(result, OUTPUT_SCHEMA)
    invocation_obj = result.get("invocation")
    assert isinstance(invocation_obj, dict)
    invocation = cast("dict[str, object]", invocation_obj)
    allow_token_value = invocation.get("allow_token_clone")
    assert isinstance(allow_token_value, bool)
    assert allow_token_value is False
    assert os.environ[x_cls_make_github_clones_x.ALLOW_TOKEN_CLONE_ENV] == "1"


def test_main_json_reports_validation_error() -> None:
    payload = {"command": "x_make_github_clones_x", "parameters": {}}

    result = main_json(payload)

    validate_payload(result, ERROR_SCHEMA)
    status_value = result.get("status")
    message_value = result.get("message")
    assert isinstance(status_value, str)
    assert isinstance(message_value, str)
    assert status_value == "failure"
    assert message_value == "input payload failed validation"
