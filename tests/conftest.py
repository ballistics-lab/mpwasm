"""`--js-runtime`: run the suite on one specific JavaScript host.

    uv run pytest                       # automatic pick (see mpwasm.AUTO_ORDER)
    uv run pytest --js-runtime node
    uv run pytest --js-runtime gi-jsc   # WebKitGTK JavaScriptCore (needs PyGObject; see README)

The choice is applied before any test runs, both in-process and for subprocesses (MPWASM_HOST). A
runtime that can't start stops the whole run with an error -- it is never silently skipped, so a CI
step named after a runtime really ran on it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import mpwasm


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--js-runtime",
        action="store",
        default=None,
        choices=sorted(mpwasm.HOSTS),
        help="JavaScript host to run MicroPython on (default: automatic pick)",
    )


def pytest_configure(config: pytest.Config) -> None:
    runtime = config.getoption("--js-runtime")
    if runtime:
        os.environ["MPWASM_HOST"] = runtime
    try:
        # probe: start the host and load the interpreter once, up front
        mpwasm.MicroPython().close()
    except Exception as exc:  # noqa: BLE001 -- any failure here means the run can't be meaningful
        pytest.exit(f"Cannot start tests: JavaScript runtime {runtime or '(auto)'} failed: {exc}", returncode=1)


def pytest_report_header(config: pytest.Config) -> str:
    with mpwasm.MicroPython() as mp:
        return f"mpwasm: MicroPython on JS runtime {mp.host}"
