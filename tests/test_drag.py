import asyncio
import json
import unittest
from unittest.mock import patch

from tests.test_smoke import _load_module


class DragTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_drag_releases_left_button(self):
        mod = _load_module()
        pressed = asyncio.Event()
        buttons = []

        class Wire:
            async def send(self, packet):
                message = json.loads(packet)
                if message["event_type"] == "mouse_button":
                    buttons.append(message["event"])
                    if message["event"] == {"button": "left", "state": True}:
                        pressed.set()
                        await asyncio.Future()

        conn = mod.Connection("https://kvm.example.test", None, Wire())
        with patch.object(mod, "_conn", conn):
            drag = asyncio.create_task(mod.kvm_mouse_drag_pct(25, 30, 75, 70))
            try:
                await asyncio.wait_for(pressed.wait(), timeout=2)
            finally:
                drag.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await drag

        self.assertEqual(buttons, [
            {"button": "left", "state": True},
            {"button": "left", "state": False},
        ])
