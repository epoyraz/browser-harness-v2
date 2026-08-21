"""The async frontends, against the same real-daemon-on-a-real-socket wiring as the sync
suite — a mocked session would prove none of the IPC, adoption, or multiplexing behaviour.

No pytest-asyncio: each test drives its own loop via `asyncio.run`, which keeps the dev
dependency surface at exactly pytest + ruff.
"""
import asyncio
import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.aio import AsyncConnection, AsyncSession
from harness.connect.client import RemoteConnection
from harness.connect.daemon import Daemon
from harness.core.outcome import Class, HarnessError, ProtocolMismatch
from tests.fake_browser import FakeBrowser


@pytest.fixture
def served(monkeypatch, tmp_path):
    runtime = tmp_path / "bhaio"
    runtime.mkdir()
    monkeypatch.setenv("BH_RUNTIME_DIR", str(runtime))
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
            selected = await s.use_tab("b")
            await s.goto("https://x.test/page")
            echoed = await s.js("1 + 1")
            empty_fill = await s.fill_form([])
            created = await s.new_tab()
            await created.goto("https://new.test/page")
            return (selected.target_id, echoed, empty_fill.ok, created.target_id,
                    asyncio.iscoroutinefunction(created.goto))

    selected, echoed, fill_ok, created, goto_is_async = _run(main())
    assert (selected, echoed, fill_ok, goto_is_async) == ("b", "1 + 1", True, True)
    assert created.startswith("T")
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


def test_connect_builds_a_leased_cursor_on_the_pinned_worker(served, monkeypatch):
    conn = RemoteConnection("aiotest")
    try:
        lease = conn.create_target_lease("b")
    finally:
        conn.close()
    monkeypatch.setenv("BH_TARGET_LEASE", lease)

    async def main():
        async with await AsyncSession.connect("aiotest") as session:
            return (await session.tab()).target_id

    assert _run(main()) == "b"


def test_an_abandoned_async_session_closes_on_its_worker():
    closed = threading.Event()

    class Sync:
        def close(self):
            closed.set()

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bh-aio-finalizer-test")
    session = AsyncSession(Sync(), executor)
    del session
    gc.collect()
    assert closed.wait(2.0)
    with pytest.raises(RuntimeError, match="shutdown"):
        executor.submit(lambda: None)


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


def test_async_connection_reads_a_large_daemon_reply(served):
    browser, _ = served
    browser.eval_hook = lambda e: "x" * 200_000

    async def main():
        async with await AsyncConnection.connect("aiotest") as conn:
            reply = await conn.request("Runtime.evaluate")
            return len(reply["result"]["value"])

    assert _run(main()) == 200_000


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


def test_a_cancelled_async_request_drops_its_pending_slot(served):
    browser, _ = served
    browser.latency = 0.5

    async def main():
        async with await AsyncConnection.connect("aiotest") as conn:
            task = asyncio.create_task(conn.request("Runtime.evaluate", timeout=5.0))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pending_after_cancel = len(conn._pending)
            browser.latency = 0.0
            await asyncio.sleep(0.6)
            reply = await conn.request("Runtime.evaluate")
            return pending_after_cancel, reply

    pending, reply = _run(main())
    assert pending == 0
    assert reply["result"]["value"] == "<browser>"


class _ImmediateWriter:
    def write(self, data): ...
    async def drain(self): ...
    def close(self): ...
    async def wait_closed(self): ...


def test_async_connection_reports_an_oversized_daemon_frame_as_protocol_error(monkeypatch):
    monkeypatch.setattr("harness.aio.MAX_DAEMON_FRAME", 64)

    async def main():
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 65)
        stream.feed_eof()
        conn = AsyncConnection("fake", None, _ImmediateWriter())
        conn._stream = stream
        future = asyncio.get_running_loop().create_future()
        conn._pending[1] = future
        await conn._pump()
        with pytest.raises(ProtocolMismatch, match="exceeds 64 bytes"):
            await future

    _run(main())


def test_async_connection_close_awaits_the_cancelled_reader():
    class NeverStream:
        async def read(self, size):
            await asyncio.Event().wait()

    async def main():
        conn = AsyncConnection("fake", None, _ImmediateWriter())
        conn._stream = NeverStream()
        reader = asyncio.create_task(conn._pump())
        conn._reader = reader
        await asyncio.sleep(0)
        await conn.close()
        return reader.done(), reader.cancelled()

    assert _run(main()) == (True, True)


# --- both frontends share one daemon ---------------------------------------------

def test_sync_and_async_clients_coexist_on_one_daemon(served):
    from harness.session import Session

    async def main():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bh-sync-test")
        sync = await loop.run_in_executor(executor, lambda: Session("aiotest"))
        try:
            async with await AsyncSession.connect("aiotest") as aio:
                await aio.use_tab("b")
                return (await loop.run_in_executor(executor, lambda: sync.tab().target_id),
                        (await aio.tab()).target_id)
        finally:
            await loop.run_in_executor(executor, sync.close)
            executor.shutdown(wait=False)

    a, b = _run(main())
    assert a == "a" and b == "b"
