import asyncio
import json
import sys
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "glkvm_mcp.py"
MODERN = "2026-07-28"
CLIENT_INFO = {"name": "glkvm-test", "version": "1"}


class StdioTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await asyncio.create_subprocess_exec(
            sys.executable, str(SERVER),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        self.addAsyncCleanup(self.stop_server)
        self.request_id = 0

    async def stop_server(self):
        self.server.stdin.close()
        try:
            await asyncio.wait_for(self.server.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.server.kill()
            await self.server.wait()

    async def send(self, method, params):
        self.request_id += 1
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        self.server.stdin.write(json.dumps(message).encode() + b"\n")
        await self.server.stdin.drain()
        line = await asyncio.wait_for(self.server.stdout.readline(), timeout=10)
        self.assertTrue(line, "MCP server closed stdout before replying")
        response = json.loads(line)
        self.assertEqual(response.get("id"), self.request_id)
        self.assertNotIn("error", response)
        return response["result"]

    async def check_tools(self, metadata):
        tools = await self.send("tools/list", metadata)
        by_name = {tool["name"]: tool for tool in tools["tools"]}
        self.assertIn("kvm_screenshot", by_name)
        self.assertEqual(by_name["kvm_switch_status"]["outputSchema"]["type"], "object")
        status = await self.send("tools/call", {
            **metadata, "name": "kvm_status", "arguments": {},
        })
        self.assertFalse(status["isError"])
        self.assertEqual(status["structuredContent"], {
            "connected": False, "host": "", "held_keys": [], "ws_open": False,
        })
        disconnect = await self.send("tools/call", {
            **metadata, "name": "kvm_disconnect", "arguments": {},
        })
        self.assertFalse(disconnect["isError"])
        self.assertIs(disconnect["structuredContent"]["connected"], False)

    async def test_july_28_discovery_and_tools_without_initialize(self):
        metadata = {"_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": {},
        }}
        discovery = await self.send("server/discover", metadata)
        self.assertIn(MODERN, discovery["supportedVersions"])
        self.assertIn("tools", discovery["capabilities"])
        await self.check_tools(metadata)
