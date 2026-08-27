"""Fixtures shared by the tests that drive a Tab against the fake browser.

These lived in `test_page.py` while it was the only module that needed them. The
application-state tests moved to their own module when the classification moved to
`applications/`, and duplicating a fixture is how two copies of it drift.
"""

import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.ops.page import Tab
from tests.fake_browser import FakeBrowser


@pytest.fixture
def wired():
    browser = FakeBrowser("a", "b")
    conn = Connection(browser).start()
    registry = SessionRegistry(conn)
    yield browser, conn, registry
    conn.close()


def _tab(wired, **kw):
    _browser, conn, registry = wired
    return Tab(conn, registry, "a", **kw)


def _reader_caught_up(tab):
    """Barrier for events emitted from the test thread.

    They are already sitting in the fake's queue and a reply can only be pushed behind
    them, so one completed round trip proves the single reader thread has dispatched every
    one of them. That is how a test asserts an event did NOT have an effect without
    sleeping on a thread that has no other way to say it is done.
    """
    tab.cdp("Page.getLayoutMetrics")

