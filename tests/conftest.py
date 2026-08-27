"""Fixtures shared across the test tree.

`served` and `session` bring up a real daemon on a real socket with the fake browser behind
it. Three modules need them now — the core session, the application workflow, and
recording — in two directories, and a fixture copied per directory is how three of them
drift apart.
"""
import os
import threading

import pytest

from harness.connect.daemon import Daemon
from harness.session import Session
from tests.fake_browser import FakeBrowser


@pytest.fixture
def served(monkeypatch):
    d = f"/tmp/bhs{os.getpid()}"
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("BH_RUNTIME_DIR", d)
    browser = FakeBrowser("a", "b")
    daemon = Daemon("sesstest", browser).start()
    threading.Thread(target=daemon.serve_forever, daemon=True).start()
    yield browser, daemon
    daemon.stop()


@pytest.fixture
def session(served):
    s = Session("sesstest")
    yield s
    s.close()

