"""
Smoke test for glkvm_mcp.py.

Imports the server module without launching the stdio loop and asserts that the
expected MCP tools are registered. Run from the repo root:

    python -m unittest tests.test_smoke

or directly:

    python tests/test_smoke.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import sys
import unittest
import json
import subprocess

import httpx
from PIL import Image
from unittest.mock import AsyncMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SERVER_PATH = os.path.join(REPO_ROOT, "glkvm_mcp.py")

EXPECTED_TOOLS = {
    "kvm_connect",
    "kvm_disconnect",
    "kvm_send_text",
    "kvm_send_keys",
    "kvm_hold_key",
    "kvm_release_all",
    "kvm_mouse_move",
    "kvm_mouse_move_pct",
    "kvm_mouse_click",
    "kvm_mouse_scroll",
    "kvm_mouse_drag_pct",
    "kvm_screenshot",
    "kvm_screenshot_to_file",
    "kvm_status",
    "kvm_switch_status",
    "kvm_switch_port",
    "kvm_ocr_screenshot",
    "kvm_ocr_click",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("glkvm_mcp", SERVER_PATH)
    assert spec is not None and spec.loader is not None, "could not load glkvm_mcp.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["glkvm_mcp"] = mod
    spec.loader.exec_module(mod)
    return mod


class SmokeTest(unittest.TestCase):
    def test_module_imports(self):
        mod = _load_module()
        self.assertTrue(hasattr(mod, "mcp"), "module should expose `mcp` (MCPServer instance)")

    def test_tools_registered(self):
        mod = _load_module()
        tools = asyncio.run(mod.mcp.list_tools())
        names = {t.name for t in tools}
        missing = EXPECTED_TOOLS - names
        self.assertFalse(
            missing,
            msg=f"Missing tools: {sorted(missing)}. Got: {sorted(names)}",
        )

    def test_kvm_connect_signature(self):
        mod = _load_module()
        tools = asyncio.run(mod.mcp.list_tools())
        connect = next((t for t in tools if t.name == "kvm_connect"), None)
        self.assertIsNotNone(connect, "kvm_connect tool not registered")
        schema = connect.input_schema
        required = set(schema.get("required", []))
        self.assertEqual(required, set())
        self.assertEqual(set(schema.get("properties", {})), {"profile"})

    def test_sops_credentials(self):
        mod = _load_module()
        payload = {"host": "kvm.example.test", "username": "admin", "password": "test-only"}
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode())
        with patch.dict(os.environ, {"GLKVM_CREDENTIALS_FILE": "/tmp/test.sops.json"}), patch.object(mod.subprocess, "run", return_value=response) as run:
            self.assertEqual(mod._load_credentials(), tuple(payload.values()))
            self.assertEqual(run.call_args.args[0], ["sops", "--decrypt", "--output-type", "json", "/tmp/test.sops.json"])

    def test_selects_only_configured_profiles(self):
        mod = _load_module()
        default = {"host": "cometx.example.test", "username": "admin", "password": "test-only"}
        comet_q = {**default, "host": "comet-q.example.test"}
        payload = {"default_profile": "cometx", "profiles": {"cometx": default, "comet-q": comet_q}}
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode())
        with patch.dict(os.environ, {"GLKVM_CREDENTIALS_FILE": "/tmp/test.sops.json"}), patch.object(mod.subprocess, "run", return_value=response):
            self.assertEqual(mod._load_credentials(), tuple(default.values()))
            self.assertEqual(mod._load_credentials("comet-q"), tuple(comet_q.values()))
            with self.assertRaisesRegex(RuntimeError, "Select a configured KVM profile"):
                mod._load_credentials("https://other.example.test")

    def test_sops_error_does_not_expose_output(self):
        mod = _load_module()
        error = subprocess.CalledProcessError(1, ["sops"], output=b"sensitive-output", stderr=b"sensitive-error")
        with patch.dict(os.environ, {"GLKVM_CREDENTIALS_FILE": "/tmp/test.sops.json"}), patch.object(mod.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^Could not decrypt the KVM credential file with SOPS.$"):
                mod._load_credentials()

    def test_rejects_empty_password_and_unsafe_host(self):
        mod = _load_module()
        for host, password in [("kvm.example.test", ""), ("http://kvm.example.test", "test-only"), ("https://kvm.example.test/path", "test-only")]:
            payload = {"host": host, "username": "admin", "password": password}
            response = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode())
            with self.subTest(host=host), patch.dict(os.environ, {"GLKVM_CREDENTIALS_FILE": "/tmp/test.sops.json"}), patch.object(mod.subprocess, "run", return_value=response):
                with self.assertRaises(RuntimeError):
                    mod._load_credentials()


class SnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_comet_q_uses_live_frame_only_when_snapshot_cannot_supply_one(self):
        mod = _load_module()
        with io.BytesIO() as buffer:
            Image.new("RGB", (8, 8)).save(buffer, "JPEG")
            jpeg = buffer.getvalue()

        cases = [
            (True, 200, b"not a JPEG", True),
            (True, 503, b"", True),
            (True, 200, jpeg, False),
            (True, 401, b"", False),
            (False, 503, b"", False),
        ]
        for comet_q, status, content, use_live in cases:
            with self.subTest(comet_q=comet_q, status=status, use_live=use_live):
                transport = httpx.MockTransport(lambda _: httpx.Response(status, content=content))
                async with httpx.AsyncClient(transport=transport) as client:
                    conn = mod.Connection("https://kvm.example.test", client, None, live_snapshot_fallback=comet_q)
                    capture = AsyncMock(return_value=jpeg)
                    with patch.object(mod, "_conn", conn), patch.object(mod, "_capture_live_jpeg", capture):
                        if use_live or content == jpeg:
                            result = await mod.kvm_screenshot(max_width=960, quality=82)
                            self.assertEqual(result.data, jpeg)
                        else:
                            with self.assertRaisesRegex(mod.ToolError, f"HTTP {status}"):
                                await mod.kvm_screenshot()
                        if use_live:
                            capture.assert_awaited_once_with(conn, 960, 82)
                        else:
                            capture.assert_not_awaited()

    async def test_preview_fallback_and_non_retryable_error(self):
        mod = _load_module()
        def jpeg(color):
            with io.BytesIO() as buffer:
                Image.new("RGB", (8, 8), color).save(buffer, "JPEG")
                return buffer.getvalue()

        preview_jpeg, full_jpeg = jpeg("red"), jpeg("blue")
        for preview_status, preview_body, full_body, expected_calls in [
            (500, b"error", full_jpeg, 2),
            (200, preview_jpeg, full_jpeg, 1),
            (200, preview_jpeg[:100], full_jpeg, 2),
            (200, b"not a JPEG", b"not a JPEG", 2),
            (401, b"unauthorized", full_jpeg, 1),
        ]:
            queries = []

            def get(request):
                self.assertEqual(request.url.path, "/api/streamer/snapshot")
                queries.append(dict(request.url.params))
                if "preview" in request.url.params:
                    return httpx.Response(preview_status, content=preview_body)
                return httpx.Response(200, content=full_body, headers={"content-type": "image/jpeg"})

            with self.subTest(preview_status=preview_status, preview_size=len(preview_body)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(get)) as client:
                    conn = mod.Connection(base_url="https://comet-q.example.test", http=client, ws=None)
                    with patch.object(mod, "_conn", conn):
                        if preview_status == 401:
                            with self.assertRaisesRegex(mod.ToolError, "HTTP 401"):
                                await mod.kvm_screenshot()
                        elif full_body != full_jpeg:
                            with self.assertRaisesRegex(mod.ToolError, "did not contain a valid JPEG"):
                                await mod.kvm_screenshot()
                        else:
                            result = await mod.kvm_screenshot()
                            self.assertEqual(result.data, full_jpeg if expected_calls == 2 else preview_jpeg)
                self.assertEqual(len(queries), expected_calls)
                self.assertEqual(queries[0], {
                    "allow_offline": "true", "preview": "true",
                    "preview_max_width": "1024", "preview_quality": "60",
                })
                if expected_calls == 2:
                    self.assertEqual(queries[1], {"allow_offline": "true"})


if __name__ == "__main__":
    unittest.main(verbosity=2)