"""Drive `bh mcp` over real stdio JSON-RPC, the way a client would.

Nothing else proves the server works. A unit test can call the wrapped functions, but the
two things most likely to break are protocol-shaped: a stray `print` corrupting a frame,
and a value `json.dumps` refuses. Both only appear over the wire.

    uv pip install "mcp>=2.0.0,<3"
    BH_HEADLESS=1 uv run python tests/live/mcp_check.py
"""
import itertools
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _browser

scratch = Path(tempfile.mkdtemp(prefix="bh-mcp-"))
runtime = Path(tempfile.mkdtemp(prefix="bhmcp-", dir="/tmp" if os.name != "nt" else None))
_browser.launch(scratch)
env = {**os.environ, "BH_RUNTIME_DIR": str(runtime), "BH_PROFILE_DIRS": str(scratch),
       "BU_CDP_URL": "", "BU_CDP_WS": "", "PYTHONPATH": str(ROOT)}

proc = subprocess.Popen([sys.executable, "-m", "harness.cli.main", "mcp"], cwd=ROOT, env=env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
errs=[]
threading.Thread(target=lambda: [errs.append(l) for l in proc.stderr], daemon=True).start()
lines = queue.Queue()
threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()

_next_id = itertools.count(1)


def send(method, params=None):
    mid = next(_next_id)
    proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":mid,"method":method,
                                 "params":params or {}})+"\n"); proc.stdin.flush()
    deadline=time.time()+60
    while time.time()<deadline:
        try:
            line = lines.get(timeout=max(0.01, deadline - time.time()))
        except queue.Empty:
            break
        if not line: break
        try: msg=json.loads(line)
        except json.JSONDecodeError:
            print(f"  !! non-JSON on stdout (would corrupt the protocol): {line[:120]!r}"); continue
        if msg.get("id")==mid: return msg
    return {"error":"timeout"}

def notify(method, params=None):
    proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":method,"params":params or {}})+"\n")
    proc.stdin.flush()

ok=fail=0
def check(label, cond, got=""):
    global ok,fail
    if cond: ok+=1; print(f"  PASS  {label:<52} {got}")
    else: fail+=1; print(f"  FAIL  {label:<52} {got}")
try:
    init = send("initialize", {"protocolVersion":"2025-06-18",
        "capabilities":{}, "clientInfo":{"name":"probe","version":"0"}})
    check("initialize handshake", "result" in init,
          (init.get("result") or {}).get("serverInfo",{}).get("name",""))
    notify("notifications/initialized")

    tools = send("tools/list").get("result",{}).get("tools",[])
    names = sorted(t["name"] for t in tools)
    check("tools are advertised", len(tools) >= 25, f"{len(tools)} tools")
    check("the query helpers are exposed",
          {"browser_ax","browser_find","browser_extract"} <= set(names))
    check("cdp is deliberately absent", "browser_cdp" not in names)
    schema = next(t for t in tools if t["name"]=="browser_find")
    props = set((schema.get("inputSchema") or {}).get("properties",{}))
    check("schemas come from the signatures", {"pattern","exclude","max_len"} <= props,
          ",".join(sorted(props)[:4]))

    def call(name, args=None):
        r = send("tools/call", {"name":name,"arguments":args or {}})
        content = (r.get("result") or {}).get("content") or []
        return json.loads(content[0]["text"]) if content else r

    fixture = "data:text/html,<title>MCP fixture</title><button type=button>Test button</button>"
    got = call("browser_goto", {"url": fixture})
    check("a real navigation through MCP", got.get("landed", "") == fixture,
          str(got.get("landed"))[:40])
    page = call("browser_read_page", {"max_chars":400})
    check("read_page returns structure", page.get("title")=="MCP fixture", str(page.get("title")))
    ax = call("browser_ax", {"limit":5})
    check("ax reaches the accessibility tree", isinstance(ax, list) and any(r.get("role") for r in ax),
          str([r.get("role") for r in ax][:3]))

    bad_result = send("tools/call", {"name": "browser_click", "arguments": {"ref": "e999"}})
    check("a failed action sets the MCP error flag", bad_result["result"].get("isError") is True)
    bad = json.loads(bad_result["result"]["content"][0]["text"])
    check("a failure keeps its typed class", bad.get("class")=="element_gone", str(bad.get("class")))
    check("and carries a recovery line", bool(bad.get("recovery")), str(bad.get("recovery"))[:44])
    # A missing required argument is rejected by MCP's own schema validation before the
    # tool runs — the protocol layer's job, not ours.
    raw = send("tools/call", {"name":"browser_goto","arguments":{}})
    rejected = "error" in raw or (raw.get("result") or {}).get("isError")
    check("a schema-invalid call is refused by the protocol", bool(rejected))

    # A failure inside a tool that is not a browser failure must not borrow an outcome
    # class it does not belong to.
    tool_err = call("browser_extract", {"selector":"li[", "fields":{"t":"h3"}})
    check("a bad selector still arrives typed", tool_err.get("class")=="scope_refused",
          str(tool_err.get("class")))
    big = call("browser_js", {"expression":"'x'.repeat(3000000)"})
    check("an oversized value spills instead of flooding the client",
          "_elided" in json.dumps(big) or len(json.dumps(big)) < 500_000,
          f"{len(json.dumps(big)):,} bytes")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    try:
        stopped = subprocess.run(
            [sys.executable, "-m", "harness.cli.main", "daemon", "stop", "mcp"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=20, check=False)
        check("MCP daemon shuts down cleanly", stopped.returncode == 0, stopped.stderr[-100:])
    finally:
        _browser.kill(scratch)
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)
noise = [e for e in errs if "Traceback" in e]
print(f"\n{ok}/{ok+fail} passed" + (f"   stderr tracebacks: {len(noise)}" if noise else ""))
sys.exit(1 if fail else 0)
