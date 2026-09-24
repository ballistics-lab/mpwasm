"""Build hook: fetch the MicroPython assets as part of every build (see build_assets.py).

All metadata lives in pyproject.toml; this only wires build_assets.fetch() into build_py, so wheels,
sdist -> wheel builds and editable installs (`uv sync`) all get the files without a separate step.
"""

import os
import sys

from setuptools import setup
from setuptools.command.build_py import build_py

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_assets


class BuildPyWithAssets(build_py):
    def run(self) -> None:
        build_assets.fetch()
        super().run()


setup(cmdclass={"build_py": BuildPyWithAssets})
