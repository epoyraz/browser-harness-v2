"""The async frontends, against the same real-daemon-on-a-real-socket wiring as the sync
suite — a mocked session would prove none of the IPC, adoption, or multiplexing behaviour.

No pytest-asyncio: each test drives its own loop via `asyncio.run`, which keeps the dev
dependency surface at exactly pytest + ruff.
"""
import asyncio
import os
import threading
import time

import pytest

from harness.aio import AsyncConnection, AsyncSession
from harness.connect.daemon import Daemon
from harness.core.outcome import Class, HarnessError
from tests.fake_browser import FakeBrowser


@pytest.fixture
def served(monkeypatch):
    d = f"/tmp/bhaio{os.getpid()}"
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("BH_RUNTIME_DIR", d)
    browser = FakeBrowser("a", "b")
    daemon = Daemon("aiotest", browser).start()
    threading.Thread(target=daemon.serve_forever, daemon=True).start()
    yield browser, daemon
    daemon.stop()


def _run(coro):
    return asyncio.run(coro)


# --- AsyncSession (Phase 1) -----------------------------------------------------

def test_an_async_session_drives_the_whole_sync_surface(served):
    browser, _ = served
    browser.eval_hook = lambda e: e          # echo the expression as the value

    async def main():
        async with await AsyncSession.connect("aiotest") as s:
            tab = await s.tab()
            await s.use_tab(tab.target_id)
            await tab.goto("https://x.test/page")
            return await tab.js("1 + 1")

    assert _run(main()) == "1 + 1"
    assert any(c["method"] == "Page.navigate" for c in browser.calls)


def test_the_async_cursor_survives_across_ops(served):
    """The reason for the single pinned worker thread: the sync session's current-tab is a
    thread-local, so a default executor pool would lose it between calls."""
    async def main():
        async with await AsyncSession.connect("aiotest") as s:
            await s.use_tab("b")
            first = await s.tab()
            second = await s.tab()
            return first.target_id, second.target_id

    assert _run(main()) == ("b", "b")


def test_two_async_sessions_adopt_different_tabs(served):
    """D1 through the async frontend: two clients, two cursors, no clobbering."""
    async def main():
        a = await AsyncSession.connect("aiotest")
        b = await AsyncSession.connect("aiotest")
        try:
            await a.use_tab("a")
            await b.use_tab("b")
            return (await a.tab()).target_id, (await b.tab()).target_id
        finally:
            await a.close()
            await b.close()

    assert _run(main()) == ("a", "b")


def test_concurrent_tasks_on_one_session_all_land_correctly(served):
    """gather() over one cursor serialises at op granularity — correct results, one
    worker thread; parallel callers open several sessions."""
    browser, _ = served
    browser.eval_hook = lambda e: e

    async def main():
        async with await AsyncSession.connect("aiotest") as s:
            tab = await s.tab()
            results = await asyncio.gather(*[tab.js(f"e{i}") for i in range(16)])
            return results

    assert len(set(_run(main()))) == 16


def test_a_cancelled_call_is_abandoned_not_corrupting(served):
    browser, _ = served
    browser.eval_hook = lambda e: e
    browser.latency = 0.5

    async def main():
        async with await AsyncSession.connect("aiotest") as s:
            tab = await s.tab()
            task = asyncio.create_task(tab.js("slow"))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            browser.latency = 0.0
            return await tab.js("next")

    assert _run(main()) == "next"


# --- AsyncConnection (Phase 2) --------------------------------------------------

def test_a_native_async_connection_speaks_raw_cdp(served):
    _, _ = served

    async def main():
        async with await AsyncConnection.connect("aiotest") as conn:
            targets = await conn.request("Target.getTargets")
            pages = [t["targetId"] for t in targets["targetInfos"]
                     if t["type"] == "page"]
            try:
                await conn.request("Target.attachToTarget",
                                   {"targetId": "ghost", "flatten": True})
            except HarnessError as e:
                cls = e.cls
            return pages, cls

    pages, cls = _run(main())
    assert set(pages) == {"a", "b"}
    assert cls is Class.TARGET_GONE


def test_async_requests_multiplex_rather_than_queue(served):
    browser, _ = served
    browser.latency = 0.2

    async def main():
        async with await AsyncConnection.connect("aiotest") as conn:
            started = time.monotonic()
            await asyncio.gather(*[conn.request("Runtime.evaluate")
                                   for _ in range(8)])
            return time.monotonic() - started

    elapsed = _run(main())
    assert elapsed < 1.0, f"8 concurrent 200ms calls took {elapsed:.2f}s — they serialised"
    assert browser.max_in_flight > 1


def test_events_reach_async_subscribers(served):
    async def main():
        async with await AsyncConnection.connect("aiotest") as conn:
            seen: list[dict] = []
            conn.subscribe(seen.append)
            await conn.request("Target.attachToTarget",
                               {"targetId": "a", "flatten": True})
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not seen:
                await asyncio.sleep(0.01)
            return seen

    seen = _run(main())
    assert any(e.get("method") == "Target.attachedToTarget" for e in seen)


def test_a_timed_out_async_request_discards_its_late_reply(served):
    browser, _ = served
    browser.latency = 0.5

    async def main():
        async with await AsyncConnection.connect("aiotest") as conn:
            try:
                await conn.request("Runtime.evaluate", timeout=0.05)
                raise AssertionError("should have timed out")
            except HarnessError:
                pass
            browser.latency = 0.0
            await asyncio.sleep(0.6)             # the abandoned reply lands here
            return await conn.request("Runtime.evaluate")

    assert _run(main())["result"]["value"] == "<browser>"


# --- both frontends share one daemon ---------------------------------------------

def test_sync_and_async_clients_coexist_on_one_daemon(served):
    from harness.session import Session

    async def main():
        loop = asyncio.get_running_loop()
        sync = await loop.run_in_executor(None, lambda: Session("aiotest"))
        try:
            async with await AsyncSession.connect("aiotest") as aio:
                await aio.use_tab("b")
                return (await loop.run_in_executor(None, lambda: sync.tab().target_id),
                        (await aio.tab()).target_id)
        finally:
            await loop.run_in_executor(None, sync.close)

    a, b = _run(main())
    assert a == "a" and b == "b"
