"""Build an sdist, build its wheel, and exercise a clean install outside the checkout.

Run with `uv run python tests/packaging_check.py`. No browser or personal profile is used.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SMOKE = r'''
import asyncio
import json
import sys
from importlib.resources import files
from pathlib import Path

import applications
import evidence.recording
import mcp_server
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

assert callable(applications.run_application)
assert callable(evidence.recording.install)
assert files("harness").joinpath("SKILL.md").is_file()
assert files("harness").joinpath("assets/tab_recorder/manifest.json").is_file()
assert Path(mcp_server.__file__).is_relative_to(Path(sys.prefix))

async def check():
    server = StdioServerParameters(command=sys.argv[1], args=["mcp"])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=15) as client:
            await client.initialize()
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"browser_goto", "browser_click", "browser_read_page"} <= names
            # Schema validation fails before a browser is requested.
            bad = await client.call_tool("browser_goto", {})
            assert bad.is_error
            print(json.dumps({"installed_mcp_tools": len(names), "schema_error": True}))

asyncio.run(check())
'''


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bh-package-") as temporary:
        scratch = Path(temporary)
        artifacts = scratch / "artifacts"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BH_", "BU_")) and k != "PYTHONPATH"}
        env["PYTHONNOUSERSITE"] = "1"

        def run(command: list[str], *, cwd: Path = scratch) -> str:
            result = subprocess.run(command, cwd=cwd, env=env, check=True,
                                    capture_output=True, text=True, timeout=180)
            return result.stdout.strip()

        run(["uv", "build", "--sdist", "--out-dir", str(artifacts)], cwd=ROOT)
        sdist, = artifacts.glob("*.tar.gz")
        with tarfile.open(sdist) as archive:
            members = archive.getnames()
            assert not any(Path(p).name in {"required.txt", "jobs.json"} for p in members)
        run(["uv", "build", str(sdist), "--wheel", "--out-dir", str(artifacts)])
        wheel, = artifacts.glob("*.whl")
        with zipfile.ZipFile(wheel) as archive:
            assert {"applications/__init__.py", "evidence/__init__.py", "mcp_server.py",
                    "harness/SKILL.md"} <= set(archive.namelist())
        venv = scratch / "venv"
        run(["uv", "venv", "--python", sys.executable, str(venv)])
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        bh = scripts / ("bh.exe" if os.name == "nt" else "bh")
        run(["uv", "pip", "install", "--python", str(python), f"{wheel}[mcp]"])
        assert "daemon" in run([str(bh), "--help"])
        assert run([str(bh), "--version"])
        empty = scratch / "empty"
        empty.mkdir()
        run([str(bh), "stats", str(empty), "--json"])
        print(run([str(python), "-I", "-c", SMOKE, str(bh)]))
        print("sdist -> wheel -> clean install: imports, assets, CLI, and MCP passed")


if __name__ == "__main__":
    main()
