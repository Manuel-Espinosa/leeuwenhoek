"""Packaging checks: the built wheel must ship templates, static files and py.typed."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = [
    "leeuwenhoek/templates/leeuwenhoek/base.html",
    "leeuwenhoek/static/leeuwenhoek/bulma.min.css",
    "leeuwenhoek/py.typed",
]


@pytest.mark.slow
def test_wheel_contains_templates_static_and_py_typed(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")

    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    names = set(zipfile.ZipFile(wheels[0]).namelist())
    missing = [path for path in REQUIRED_FILES if path not in names]
    assert not missing, f"wheel is missing: {missing}"
