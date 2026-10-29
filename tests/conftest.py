"""Shared fixtures. The suite runs offline: no API key, no network, no uv (the MCP server runs in-process).

    cd labs && python -m pytest -q sawn/tests
"""

import os
import sys
from pathlib import Path

import pytest

SAWN_DIR = Path(__file__).resolve().parents[1]
LABS = SAWN_DIR.parent
for p in (str(LABS), str(SAWN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("OPENROUTER_API_KEY", "offline-tests")  # never used: every model call is scripted

import harness  # noqa: E402
from sawn import Sawn  # noqa: E402


@pytest.fixture(scope="session")
def mcp_tools():
    with harness.policy_server("inproc") as bridge:
        yield bridge.agent_tools()


@pytest.fixture
def db(tmp_path):
    return Sawn(tmp_path / "db")
