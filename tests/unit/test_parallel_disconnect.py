"""A dead browser ends the run; it does not become N instant per-item failures.

Measured 2026-08-29 on a 500-item apply-chain run: the CDP connection dropped after ~225
items and every remaining item failed on `new_tab` within seconds — 265 records that said
`browser_disconnected` with no work behind them, indistinguishable in the summary from
265 pages that had genuinely failed.
"""
import threading

from harness.core.outcome import BrowserDisconnected
from harness.ops.parallel import CancelToken, parallel

from .test_parallel import FakeSession


class DyingSession(FakeSession):
    """The browser goes away after `after` tabs have been handed out."""

    def __init__(self, after: int):
        super().__init__()
        self.after = after
        self.handed = 0
        self.dead = False
        self._count_lock = threading.Lock()

    def new_tab(self, url="about:blank", *, context_id=None):
        with self._count_lock:
            self.handed += 1
            if self.handed > self.after:
                self.dead = True
        if self.dead:
            raise BrowserDisconnected("connection closed")
        return super().new_tab(url, context_id=context_id)

    def use_tab(self, target_id):
        if self.dead:
            raise BrowserDisconnected("connection closed")
        return super().use_tab(target_id)


def test_browser_disconnect_stops_claiming_and_names_the_unstarted_items():
    s = DyingSession(after=2)
    seen = []
    lock = threading.Lock()

    def fn(item):
        with lock:
            seen.append(item)
        if item == 1:
            # the connection dies while this item is in flight
            s.dead = True
            raise BrowserDisconnected("connection closed")
        return item

    out = parallel(s, range(40), fn, workers=2, reuse_tabs=False)
    assert len(out) == 40
    # the item that observed the disconnect keeps its own typed outcome
    assert out[1]["ok"] is False and out[1]["class"] == "browser_disconnected"
    # nothing after the disconnect ran the item function: the run stopped instead of
    # burning through the queue with instant failures
    assert len(seen) < 10
    unstarted = [r for r in out if r.get("error", "").endswith("did not start: browser_disconnected")]
    assert len(unstarted) >= 30
    assert all(r["class"] == "browser_disconnected" and r["ok"] is False for r in unstarted)


def test_cancel_reason_is_first_writer_wins():
    token = CancelToken()
    token.cancel(reason="browser_disconnected")
    token.cancel(reason="deadline")
    assert token.cancelled and token.reason == "browser_disconnected"


def test_plain_cancel_keeps_the_generic_reason():
    s = FakeSession()
    token = CancelToken()
    token.cancel()
    out = parallel(s, range(3), lambda item: item, workers=1, token=token)
    assert all(r["class"] == "cancelled" for r in out)
    assert all(r["error"] == "parallel item did not start: cancelled" for r in out)
