"""pyproject.toml must agree with requirements.txt and .python-version.

Vercel installs from the [project] table; CI and local setups install from
requirements.txt. If the two drift apart, the deployed app runs on different packages
than the ones the tests ran on.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def runtime_requirements() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [
        line.split("#")[0].strip()
        for line in lines
        if line.split("#")[0].strip() and not line.strip().startswith("-")
    ]


def test_project_dependencies_match_requirements_txt() -> None:
    assert sorted(pyproject()["project"]["dependencies"]) == sorted(runtime_requirements())


def test_development_tools_stay_out_of_the_project_dependencies() -> None:
    names = {
        dep.split("[")[0].split("=")[0].lower() for dep in pyproject()["project"]["dependencies"]
    }

    assert names.isdisjoint({"pytest", "httpx", "ruff", "pip-audit"})


def test_requires_python_matches_python_version_file() -> None:
    version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()

    assert pyproject()["project"]["requires-python"] == f">={version}"


def test_it_is_an_app_not_a_package() -> None:
    config = pyproject()

    assert "build-system" not in config
    assert config["tool"]["uv"]["package"] is False


def test_the_vercel_entrypoint_exposes_the_fastapi_app() -> None:
    import importlib

    from fastapi import FastAPI

    module_name, attribute = pyproject()["tool"]["vercel"]["entrypoint"].split(":")

    assert (module_name, attribute) == ("app.main", "app")
    assert isinstance(getattr(importlib.import_module(module_name), attribute), FastAPI)
