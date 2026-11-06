import sys
from pathlib import Path

import pytest

# Repo root on sys.path so tests import the plugin as the package Hermes loads it as.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _fake_user_home(monkeypatch):
    """Guard tests spell paths as ~/.hermes; make ~ mean the fake user they assert against."""
    monkeypatch.setenv("HOME", "/home/u")
