from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from tests.test_smoke import _load_module


def state(active=2, synced=True):
    return {
        "ok": True,
        "result": {
            "summary": {"active_port": active, "synced": synced},
            "model": {"ports": [
                {"name": f"Input {i + 1}", "unit": 0, "channel": i}
                for i in range(4)
            ]},
            "video": {"links": [False, True, True, True]},
            "usb_otg": {"links": [False, True, True, True]},
            "unrelated": "must not be returned",
        },
    }


class SwitchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mod = _load_module()
        self.requests = []
        self.responses = []

        def handle(request):
            self.requests.append(request)
            if not self.responses:
                raise AssertionError("Unexpected HTTP request")
            return self.responses.pop(0)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.mod._conn = SimpleNamespace(
            base_url="https://kvm.example.test", http=self.http,
            held={}, send_lock=asyncio.Lock(),
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        self.mod._conn = None

    def queue(self, data, status=200):
        self.responses.append(httpx.Response(status, json=data))

    async def test_status_projects_port_numbers_and_link_state(self):
        self.queue(state())
        result = await self.mod.kvm_switch_status()
        self.assertEqual(result["active_port"], 2)
        self.assertEqual(result["ports"][1], {
            "port": 1, "input_number": 2, "name": "Input 2", "unit": 0,
            "channel": 1, "video_link": True, "usb_link": True,
        })
        self.assertNotIn("unrelated", result)
        self.assertEqual(self.requests[0].url.path, "/api/switch")

    async def test_select_waits_for_matching_synced_state(self):
        self.queue(state())
        self.queue({"ok": True})
        self.queue(state(1, False))
        self.queue(state(1, True))
        result = await self.mod.kvm_switch_port(1)
        self.assertEqual(result["active_port"], 1)
        self.assertTrue(result["synced"])
        post = self.requests[1]
        self.assertEqual(post.method, "POST")
        self.assertEqual(post.url.path, "/api/switch/set_active")
        self.assertEqual(post.url.params["port"], "1")

    async def test_already_selected_does_not_post(self):
        self.queue(state())
        await self.mod.kvm_switch_port(2)
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_arguments_do_not_make_requests(self):
        for port, timeout in [(-1, 10), (True, 10), (1.5, 10), (0, 0), (0, 31), (0, float("nan"))]:
            with self.subTest(port=port, timeout=timeout):
                with self.assertRaises(ValueError):
                    await self.mod.kvm_switch_port(port, timeout)
        self.assertFalse(self.requests)

    async def test_port_outside_advertised_model_does_not_post(self):
        self.queue(state())
        with self.assertRaises(ValueError):
            await self.mod.kvm_switch_port(4)
        self.assertEqual(len(self.requests), 1)

    async def test_held_keys_block_switch_without_requests(self):
        self.mod._conn.held["ControlLeft"] = 1
        with self.assertRaisesRegex(RuntimeError, "Release held keys"):
            await self.mod.kvm_switch_port(1)
        self.assertFalse(self.requests)

    async def test_unsupported_device(self):
        for status in (404, 405):
            self.queue({}, status)
            with self.assertRaisesRegex(RuntimeError, "does not expose"):
                await self.mod.kvm_switch_status()

    async def test_rejected_state(self):
        self.queue({"ok": False, "result": {}})
        with self.assertRaisesRegex(RuntimeError, "unsuccessful"):
            await self.mod.kvm_switch_status()

    async def test_rejected_selection(self):
        self.queue(state())
        self.queue({"ok": False})
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await self.mod.kvm_switch_port(1)

    async def test_timeout_does_not_claim_success_or_switch_again(self):
        self.queue(state())
        self.queue({"ok": True})
        self.queue(state(2, True))
        with patch.object(self.mod.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "selection is uncertain"):
                await self.mod.kvm_switch_port(1, timeout_s=1)
        self.assertEqual([r.method for r in self.requests], ["GET", "POST", "GET"])

    async def test_auth_failure_is_not_reported_as_unsupported(self):
        self.queue({}, 403)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.mod.kvm_switch_status()

    async def test_requires_connection(self):
        self.mod._conn = None
        with self.assertRaisesRegex(RuntimeError, "Not connected"):
            await self.mod.kvm_switch_status()


if __name__ == "__main__":
    unittest.main()
