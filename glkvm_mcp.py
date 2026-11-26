#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=2.3,<3",
#     "websockets>=12",
#     "httpx>=0.27",
#     "Pillow>=10",
#     "pytesseract>=0.3.13",
#     "aiortc>=1.14,<2",
# ]
# ///
"""
GLKVM MCP Server
================

Exposes keyboard, mouse, and screenshot capabilities as MCP tools for GL.iNet
GLKVM devices running firmware 1.10 or later and compatible PiKVM devices.

Scope: LAN only. TLS verification is disabled because the device ships a
self-signed certificate. Do not expose this server's stdio to a remote agent
without first confirming the target host is on a trusted network.

Run: `uv run glkvm_mcp.py`   (or `python glkvm_mcp.py` after `pip install` of
the dependencies above).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import json
import os
import socket
import ssl
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict
from urllib.parse import urlencode, urlparse

import httpx
from PIL import Image as PILImage
import websockets
from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MIN_DOWN_UP_GAP_S = 0.025      # 25 ms minimum between keydown and keyup
INTER_CHAR_GAP_S = 0.010       # 10 ms between successive characters in send_text
MOD_GAP_S = 0.005              # 5 ms between modifier and main key
STALE_S = 0.250                # release a held key if not refreshed in 250 ms
WATCHDOG_PERIOD_S = 0.040
WS_PING_PERIOD_S = 1.0
HTTP_TIMEOUT_S = 10.0

LOG = logging.getLogger("glkvm_mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------------------------------------------------------------
# Key map: printable ASCII -> (W3C KeyboardEvent.code, needs_shift)
# US layout. Covers the characters most users actually type.
# ---------------------------------------------------------------------------
def _build_keymap() -> dict[str, tuple[str, bool]]:
    m: dict[str, tuple[str, bool]] = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m[c] = (f"Key{c.upper()}", False)
        m[c.upper()] = (f"Key{c.upper()}", True)
    # Digits row
    digits = "0123456789"
    shift_digits = ")!@#$%^&*("
    for i, d in enumerate(digits):
        m[d] = (f"Digit{d}", False)
        m[shift_digits[i]] = (f"Digit{d}", True)
    # Symbols
    extras = {
        " ": ("Space", False),
        "\t": ("Tab", False),
        "\n": ("Enter", False),
        "\r": ("Enter", False),
        "-": ("Minus", False), "_": ("Minus", True),
        "=": ("Equal", False), "+": ("Equal", True),
        "[": ("BracketLeft", False), "{": ("BracketLeft", True),
        "]": ("BracketRight", False), "}": ("BracketRight", True),
        "\\": ("Backslash", False), "|": ("Backslash", True),
        ";": ("Semicolon", False), ":": ("Semicolon", True),
        "'": ("Quote", False), '"': ("Quote", True),
        ",": ("Comma", False), "<": ("Comma", True),
        ".": ("Period", False), ">": ("Period", True),
        "/": ("Slash", False), "?": ("Slash", True),
        "`": ("Backquote", False), "~": ("Backquote", True),
    }
    m.update(extras)
    return m


CHAR_TO_KEY = _build_keymap()

# Aliases accepted in send_keys() chord strings. Maps lowercase user input ->
# canonical W3C name used by GLKVM kvmd.
KEY_ALIASES: dict[str, str] = {
    # modifiers
    "ctrl": "ControlLeft", "control": "ControlLeft",
    "lctrl": "ControlLeft", "rctrl": "ControlRight",
    "shift": "ShiftLeft", "lshift": "ShiftLeft", "rshift": "ShiftRight",
    "alt": "AltLeft", "lalt": "AltLeft", "ralt": "AltRight",
    "altgr": "AltRight", "option": "AltLeft", "opt": "AltLeft",
    "meta": "MetaLeft", "lmeta": "MetaLeft", "rmeta": "MetaRight",
    "win": "MetaLeft", "windows": "MetaLeft",
    "cmd": "MetaLeft", "command": "MetaLeft",
    "super": "MetaLeft",
    # navigation
    "esc": "Escape", "escape": "Escape",
    "enter": "Enter", "return": "Enter",
    "tab": "Tab", "space": "Space", " ": "Space",
    "backspace": "Backspace", "bs": "Backspace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End",
    "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown", "pgdown": "PageDown",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "capslock": "CapsLock", "caps": "CapsLock",
    "numlock": "NumLock", "scrolllock": "ScrollLock", "scroll": "ScrollLock",
    "printscreen": "PrintScreen", "prtsc": "PrintScreen", "prtscn": "PrintScreen",
    "pause": "Pause", "break": "Pause",
    "menu": "ContextMenu", "contextmenu": "ContextMenu",
}
for i in range(1, 13):
    KEY_ALIASES[f"f{i}"] = f"F{i}"

MOUSE_BUTTONS = {"left", "right", "middle", "up", "down"}
MODIFIER_KEYS = {
    "ControlLeft", "ControlRight",
    "ShiftLeft", "ShiftRight",
    "AltLeft", "AltRight",
    "MetaLeft", "MetaRight",
}


def resolve_key_name(name: str) -> str:
    """Accept user-friendly key names or W3C codes and return the canonical W3C code."""
    if not name:
        raise ValueError("empty key name")
    # Already a W3C-style code
    if name in CHAR_TO_KEY.values() or name.startswith(("Key", "Digit", "Numpad", "Arrow", "F")):
        return name
    return KEY_ALIASES.get(name.lower(), name)


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------
@dataclass
class Connection:
    base_url: str  # e.g. "https://kvm.example.test"
    http: httpx.AsyncClient
    ws: websockets.WebSocketClientProtocol
    held: dict[str, float] = field(default_factory=dict)  # key -> down_at (monotonic)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watchdog: Optional[asyncio.Task] = None
    pinger: Optional[asyncio.Task] = None
    live_snapshot_fallback: bool = False


_conn: Optional[Connection] = None


def _require_conn() -> Connection:
    if _conn is None:
        raise RuntimeError("Not connected. Call kvm_connect(host, username, password) first.")
    return _conn


mcp = MCPServer("glkvm")


# ---------------------------------------------------------------------------
# Multiport switch (PiKVM API)
# ---------------------------------------------------------------------------
async def _switch_state(conn: Connection) -> dict:
    response = await conn.http.get(f"{conn.base_url}/api/switch")
    if response.status_code in (404, 405):
        raise RuntimeError("This device does not expose the PiKVM switch API.")
    response.raise_for_status()
    data = response.json()
    state = data.get("result")
    if not data.get("ok") or not isinstance(state, dict):
        raise RuntimeError("Device returned an unsuccessful switch-state response.")
    return state


def _switch_summary(state: dict) -> dict:
    summary = state.get("summary", {})
    ports = state.get("model", {}).get("ports", [])
    video = state.get("video", {}).get("links", [])
    usb = state.get("usb_otg", {}).get("links", [])
    return {
        "active_port": summary.get("active_port"),
        "synced": summary.get("synced", False),
        "ports": [
            {
                "port": index,
                "input_number": index + 1,
                "name": port.get("name", ""),
                "unit": port.get("unit"),
                "channel": port.get("channel"),
                "video_link": video[index] if index < len(video) else None,
                "usb_link": usb[index] if index < len(usb) else None,
            }
            for index, port in enumerate(ports)
        ],
    }


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_switch_status() -> dict[str, Any]:
    """Read active input and available ports using GET /api/switch.

    Ports are zero-based: port 0 is physical input 1. Link indicators do not
    guarantee usable video; capture a screenshot to verify the selected host.
    Unsupported devices report an error without changing the selection.
    """
    return _switch_summary(await _switch_state(_require_conn()))


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_switch_port(port: int, timeout_s: float = 10.0) -> dict[str, Any]:
    """Select a zero-based port and wait for the device to confirm it is synced.

    Port 0 means physical input 1. Changes the target for both video and USB
    input, so verify the returned selection and a screenshot before typing.
    Selection persists until changed. On timeout the selection is uncertain;
    inspect kvm_switch_status() before retrying. Does not send host input or
    power commands. Held keys must be released before switching.
    """
    if isinstance(port, bool) or not isinstance(port, int) or port < 0:
        raise ValueError("port must be a nonnegative integer (zero-based).")
    if not 0.1 <= timeout_s <= 30.0:
        raise ValueError("timeout_s must be between 0.1 and 30 seconds.")
    conn = _require_conn()
    async with conn.send_lock:
        if conn.held:
            raise RuntimeError("Release held keys with kvm_release_all() before switching ports.")
        state = await _switch_state(conn)
        ports = state.get("model", {}).get("ports", [])
        if port >= len(ports):
            raise ValueError(f"port must be in 0..{len(ports) - 1} for this device.")
        summary = _switch_summary(state)
        if summary["active_port"] == port and summary["synced"]:
            return summary
        response = await conn.http.post(
            f"{conn.base_url}/api/switch/set_active", params={"port": port}
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Device rejected the switch request; inspect kvm_switch_status().")
        deadline = time.monotonic() + timeout_s
        while True:
            state = await _switch_state(conn)
            summary = _switch_summary(state)
            if summary["active_port"] == port and summary["synced"]:
                return summary
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out confirming port {port}; selection is uncertain. "
                    "Inspect kvm_switch_status() before sending input."
                )
            await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def _watchdog_loop(conn: Connection) -> None:
    """Force-release any key tracked as held but not refreshed within STALE_S."""
    try:
        while True:
            await asyncio.sleep(WATCHDOG_PERIOD_S)
            now = time.monotonic()
            stale = [k for k, t in conn.held.items() if now - t > STALE_S]
            for k in stale:
                LOG.warning("watchdog releasing stale key %s", k)
                try:
                    async with conn.send_lock:
                        await _ws_send_key(conn, k, state=False, finish=True)
                except Exception as e:  # noqa: BLE001
                    LOG.error("watchdog send failed for %s: %s", k, e)
                conn.held.pop(k, None)
    except asyncio.CancelledError:
        return


async def _pinger_loop(conn: Connection) -> None:
    """Send periodic ping bytes so kvmd doesn't drop us at 15 missed pings."""
    try:
        while True:
            await asyncio.sleep(WS_PING_PERIOD_S)
            try:
                await conn.ws.send(b"\x00")
            except Exception:  # noqa: BLE001
                return
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Low-level WS senders (assume caller holds send_lock when ordering matters)
# ---------------------------------------------------------------------------
async def _ws_send_key(conn: Connection, key: str, state: bool, finish: bool = False) -> None:
    import json
    payload = json.dumps({
        "event_type": "key",
        "event": {"key": key, "state": bool(state), "finish": bool(finish)},
    })
    await conn.ws.send(payload)
    if state:
        conn.held[key] = time.monotonic()
    else:
        conn.held.pop(key, None)


async def _ws_send_mouse_button(conn: Connection, button: str, state: bool) -> None:
    import json
    if button not in MOUSE_BUTTONS:
        raise ValueError(f"unknown mouse button: {button}")
    await conn.ws.send(json.dumps({
        "event_type": "mouse_button",
        "event": {"button": button, "state": bool(state)},
    }))


async def _ws_send_mouse_move(conn: Connection, x: int, y: int) -> None:
    """Absolute move. x, y must be in PiKVM-normalized int16 range -32768..32767."""
    import json
    x = max(-32768, min(32767, int(x)))
    y = max(-32768, min(32767, int(y)))
    await conn.ws.send(json.dumps({
        "event_type": "mouse_move",
        "event": {"to": {"x": x, "y": y}},
    }))


async def _ws_send_mouse_wheel(conn: Connection, dx: int, dy: int) -> None:
    import json
    dx = max(-127, min(127, int(dx)))
    dy = max(-127, min(127, int(dy)))
    await conn.ws.send(json.dumps({
        "event_type": "mouse_wheel",
        "event": {"delta": {"x": dx, "y": dy}, "squash": False},
    }))


# ---------------------------------------------------------------------------
# Higher-level keyboard helpers (with fix logic)
# ---------------------------------------------------------------------------
async def _atomic_press(conn: Connection, key: str, hold_s: float = MIN_DOWN_UP_GAP_S) -> None:
    """Send keydown -> sleep -> keyup(finish=True). Caller holds send_lock."""
    await _ws_send_key(conn, key, state=True, finish=False)
    await asyncio.sleep(max(hold_s, MIN_DOWN_UP_GAP_S))
    await _ws_send_key(conn, key, state=False, finish=True)


async def _press_with_modifiers(conn: Connection, key: str, modifiers: list[str]) -> None:
    """Mods down -> key atomic-press -> mods up. Caller holds send_lock."""
    for m in modifiers:
        await _ws_send_key(conn, m, state=True, finish=False)
        await asyncio.sleep(MOD_GAP_S)
    await _atomic_press(conn, key)
    for m in reversed(modifiers):
        await _ws_send_key(conn, m, state=False, finish=True)
        await asyncio.sleep(MOD_GAP_S)


# ---------------------------------------------------------------------------
# Connection tools
# ---------------------------------------------------------------------------


# ---- Connection management ------------------------------------------------
class ConnectResult(TypedDict):
    connected: bool
    host: str
    message: str


def _load_credentials(profile: str | None = None) -> tuple[str, str, str]:
    path = os.environ.get("GLKVM_CREDENTIALS_FILE")
    if not path:
        raise RuntimeError("Set GLKVM_CREDENTIALS_FILE to a SOPS-encrypted JSON or YAML file.")
    try:
        result = subprocess.run(
            ["sops", "--decrypt", "--output-type", "json", os.path.expanduser(path)],
            capture_output=True, timeout=15, check=True,
        )
        credentials = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise RuntimeError("Could not decrypt the KVM credential file with SOPS.") from None
    if isinstance(credentials, dict) and "profiles" in credentials:
        profiles = credentials["profiles"]
        selected = profile if profile is not None else credentials.get("default_profile")
        if not isinstance(profiles, dict) or not isinstance(selected, str) or selected not in profiles:
            raise RuntimeError("Select a configured KVM profile by name or set default_profile in the credential file.")
        credentials = profiles[selected]
    elif profile is not None:
        raise RuntimeError("Named KVM profiles require a profiles map in the credential file.")
    if not isinstance(credentials, dict) or any(
        not isinstance(credentials.get(key), str) or not credentials[key].strip()
        for key in ("host", "username", "password")
    ):
        raise RuntimeError("KVM credential file requires nonempty host, username, and password strings.")
    host = credentials["host"]
    parsed = urlparse(host if "://" in host else f"https://{host}")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise RuntimeError("KVM credential host must be an HTTPS hostname without credentials or a path.")
    return host, credentials["username"], credentials["password"]


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_connect(profile: str | None = None) -> ConnectResult:
    """Connect to a named local SOPS profile or the configured default. Accepts no host or secrets."""
    host, username, password = await asyncio.to_thread(_load_credentials, profile)
    global _conn
    if _conn is not None:
        await kvm_disconnect()  # type: ignore[func-returns-value]

    # Normalize host into a base URL
    if "://" in host:
        base_url = host.rstrip("/")
    else:
        base_url = f"https://{host}"
    parsed = urlparse(base_url)
    if not parsed.hostname:
        raise ValueError(f"invalid host: {host}")

    # Self-signed cert is expected on a LAN-only device.
    http = httpx.AsyncClient(verify=False, timeout=HTTP_TIMEOUT_S, follow_redirects=False)
    try:
        login = await http.post(
            f"{base_url}/api/auth/login",
            data={"user": username, "passwd": password, "expire": "0"},
        )
        login.raise_for_status()
        if "auth_token" not in http.cookies and login.json().get("two_step_required"):
            raise RuntimeError("Two-step login required; not implemented in this build.")
        token = http.cookies.get("auth_token")
        if not token:
            raise RuntimeError("Login succeeded but no auth_token cookie returned.")
    except Exception:  # noqa: BLE001
        await http.aclose()
        raise RuntimeError("KVM login failed; check the local credentials and device connectivity.") from None

    # Open the control WebSocket. Pass the token as a query param so we don't
    # have to fight the websockets lib's cookie handling.
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/api/ws?auth_token={token}&stream=false"
    import ssl as _ssl
    sslctx = _ssl.create_default_context()
    sslctx.check_hostname = False
    sslctx.verify_mode = _ssl.CERT_NONE
    try:
        ws = await websockets.connect(
            ws_url,
            ssl=sslctx if ws_scheme == "wss" else None,
            max_size=8 * 1024 * 1024,
            open_timeout=HTTP_TIMEOUT_S,
            ping_interval=None,  # we manage our own pings
        )
    except Exception:  # noqa: BLE001
        await http.aclose()
        raise RuntimeError("KVM control connection failed.") from None

    conn = Connection(base_url=base_url, http=http, ws=ws, live_snapshot_fallback=profile == "comet-q")
    conn.watchdog = asyncio.create_task(_watchdog_loop(conn), name="glkvm-watchdog")
    conn.pinger = asyncio.create_task(_pinger_loop(conn), name="glkvm-pinger")
    _conn = conn

    LOG.info("connected to %s", base_url)
    return ConnectResult(connected=True, host=base_url, message="ok")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_disconnect() -> dict[str, Any]:
    """Close the WebSocket and HTTP session. Releases any held keys first."""
    global _conn
    if _conn is None:
        return {"connected": False, "message": "already disconnected"}
    conn = _conn

    # Release everything we know is held
    try:
        async with conn.send_lock:
            for k in list(conn.held.keys()):
                try:
                    await _ws_send_key(conn, k, state=False, finish=True)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    for task in (conn.watchdog, conn.pinger):
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    try:
        await conn.ws.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await conn.http.aclose()
    except Exception:  # noqa: BLE001
        pass
    _conn = None
    return {"connected": False, "message": "disconnected"}


# ---- Keyboard tools -------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_send_text(text: str, wpm: int = 200) -> dict[str, Any]:
    """
    Type a string on the remote machine using an atomic press
    pattern (each char is sent as keydown -> 25 ms gap -> keyup(finish=True)).

    Args:
        text: the literal text to type. Newlines are sent as Enter.
        wpm: approximate words-per-minute (used to pace inter-character gap).
            Use 0 for as-fast-as-possible.

    Returns:
        {"chars": <count>, "skipped": [<unsupported chars>], "elapsed_s": ...}
    """
    conn = _require_conn()
    inter = max(0.0, 60.0 / max(1, wpm) / 5.0) if wpm > 0 else 0.0
    start = time.monotonic()
    sent = 0
    skipped: list[str] = []
    async with conn.send_lock:
        for ch in text:
            mapping = CHAR_TO_KEY.get(ch)
            if mapping is None:
                skipped.append(ch)
                continue
            key, needs_shift = mapping
            mods = ["ShiftLeft"] if needs_shift else []
            await _press_with_modifiers(conn, key, mods)
            sent += 1
            if inter > 0:
                await asyncio.sleep(inter + INTER_CHAR_GAP_S)
            else:
                await asyncio.sleep(INTER_CHAR_GAP_S)
    return {"chars": sent, "skipped": skipped, "elapsed_s": round(time.monotonic() - start, 3)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_send_keys(combo: str) -> dict[str, Any]:
    """
    Send a single key chord, e.g. "Ctrl+Alt+Delete", "Cmd+Tab", "Win+L", "F11".

    Modifiers are pressed first, then the main key is sent atomically
    (down -> 25 ms -> up), then modifiers are released in reverse order. This
    is the noVNC pattern — it avoids the "Cmd+key release order" bug
    (gl-inet/glkvm issue #22) and the more general dropped-keyup bug.

    Args:
        combo: '+'-separated key names. Aliases accepted: ctrl, shift, alt, cmd,
            win, meta, opt, esc, enter, tab, space, backspace, del, home, end,
            pgup, pgdn, up/down/left/right, f1..f12, etc. The last token is the
            main key; everything before it is treated as a modifier.

    Returns:
        {"sent": <combo>, "modifiers": [...], "key": "..."}
    """
    conn = _require_conn()
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty combo")

    if len(parts) == 1:
        key = resolve_key_name(parts[0])
        async with conn.send_lock:
            await _atomic_press(conn, key)
        return {"sent": combo, "modifiers": [], "key": key}

    *mod_tokens, main_token = parts
    modifiers = [resolve_key_name(m) for m in mod_tokens]
    for m in modifiers:
        if m not in MODIFIER_KEYS:
            # tolerate non-modifier in mod position by treating it as a chained press
            pass
    key = resolve_key_name(main_token)
    async with conn.send_lock:
        await _press_with_modifiers(conn, key, modifiers)
    return {"sent": combo, "modifiers": modifiers, "key": key}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_hold_key(key: str, duration_ms: int) -> dict[str, Any]:
    """
    Press and hold a single key for an explicit duration, then release.

    Use when you specifically want OS-level auto-repeat (e.g. holding ArrowDown
    to scroll) — kvm_send_keys is not appropriate for that because it releases
    after 25 ms.

    Args:
        key: key name (W3C code or alias, e.g. "ArrowDown", "Backspace", "F5").
        duration_ms: how long to hold, in milliseconds. Capped at 5000.
    """
    conn = _require_conn()
    duration_ms = max(1, min(5000, int(duration_ms)))
    canonical = resolve_key_name(key)
    async with conn.send_lock:
        await _ws_send_key(conn, canonical, state=True, finish=False)
    try:
        await asyncio.sleep(duration_ms / 1000.0)
    finally:
        async with conn.send_lock:
            await _ws_send_key(conn, canonical, state=False, finish=True)
    return {"key": canonical, "duration_ms": duration_ms}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def kvm_release_all() -> dict[str, Any]:
    """Force-release every key the server thinks is currently held. Recovery tool."""
    conn = _require_conn()
    released: list[str] = []
    async with conn.send_lock:
        for k in list(conn.held.keys()):
            try:
                await _ws_send_key(conn, k, state=False, finish=True)
                released.append(k)
            except Exception as e:  # noqa: BLE001
                LOG.error("release_all failed for %s: %s", k, e)
    return {"released": released}


# ---- Mouse tools ----------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_move(x: int, y: int) -> dict[str, Any]:
    """
    Move the remote cursor to absolute coordinates.

    Coordinates are in PiKVM-normalized int16 space: -32768..32767 spans the
    full screen on each axis. So (0, 0) is screen center, (-32768, -32768) is
    top-left, (32767, 32767) is bottom-right.

    Tip: most callers find it easier to use kvm_mouse_move_pct(x_pct, y_pct).
    """
    conn = _require_conn()
    async with conn.send_lock:
        await _ws_send_mouse_move(conn, x, y)
    return {"x": x, "y": y}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_move_pct(x_pct: float, y_pct: float) -> dict[str, Any]:
    """Move the cursor to a percentage of the screen. (0,0) = top-left, (100,100) = bottom-right."""
    conn = _require_conn()
    x = int(round((max(0.0, min(100.0, x_pct)) / 100.0) * 65535 - 32768))
    y = int(round((max(0.0, min(100.0, y_pct)) / 100.0) * 65535 - 32768))
    async with conn.send_lock:
        await _ws_send_mouse_move(conn, x, y)
    return {"x": x, "y": y, "x_pct": x_pct, "y_pct": y_pct}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_click(button: str = "left", count: int = 1) -> dict[str, Any]:
    """
    Click the named mouse button at the current cursor position.

    Args:
        button: "left", "right", "middle", "up" (X1), or "down" (X2).
        count: number of clicks (1 for single, 2 for double, etc.). Capped at 5.
    """
    conn = _require_conn()
    count = max(1, min(5, int(count)))
    async with conn.send_lock:
        for _ in range(count):
            await _ws_send_mouse_button(conn, button, True)
            await asyncio.sleep(MIN_DOWN_UP_GAP_S)
            await _ws_send_mouse_button(conn, button, False)
            await asyncio.sleep(0.030)
    return {"button": button, "count": count}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_drag_pct(
    start_x: float, start_y: float, end_x: float, end_y: float,
) -> dict[str, Any]:
    """Press, move, and release the left button. Coordinates are 0–100 percentages of the entire screenshot, including black borders."""
    if not all(0 <= value <= 100 for value in (start_x, start_y, end_x, end_y)):
        raise ToolError("Drag coordinates must be between 0 and 100.")
    conn = _require_conn()
    x0, y0, x1, y1 = (round(value * 65535 / 100 - 32768) for value in (start_x, start_y, end_x, end_y))
    async with conn.send_lock:
        await _ws_send_mouse_move(conn, x0, y0)
        await asyncio.sleep(0.1)
        try:
            await _ws_send_mouse_button(conn, "left", True)
            await asyncio.sleep(0.06)
            for step in range(1, 11):
                await _ws_send_mouse_move(conn, round(x0 + (x1 - x0) * step / 10), round(y0 + (y1 - y0) * step / 10))
                await asyncio.sleep(0.025)
        finally:
            await _ws_send_mouse_button(conn, "left", False)
    return {"start": [start_x, start_y], "end": [end_x, end_y]}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_scroll(dx: int = 0, dy: int = 0) -> dict[str, Any]:
    """
    Scroll the remote mouse wheel.

    Args:
        dx: horizontal wheel delta (-127..127). Positive = right.
        dy: vertical wheel delta   (-127..127). Positive = down.
    """
    conn = _require_conn()
    async with conn.send_lock:
        await _ws_send_mouse_wheel(conn, dx, dy)
    return {"dx": dx, "dy": dy}


# ---- Screenshot -----------------------------------------------------------
def _is_jpeg(content: bytes) -> bool:
    try:
        with PILImage.open(io.BytesIO(content), formats=["JPEG"]) as image:
            image.load()
        return True
    except (OSError, ValueError, PILImage.DecompressionBombError):
        return False


def _check_snapshot_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        raise ToolError(f"KVM snapshot request failed with HTTP {response.status_code}.") from None


async def _capture_live_jpeg(conn: Connection, max_width: int | None, quality: int) -> bytes:
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

    device = urlparse(conn.base_url)
    addresses = await asyncio.get_running_loop().getaddrinfo(device.hostname, None, type=socket.SOCK_DGRAM)
    device_ips = {address[4][0] for address in addresses}
    query = urlencode({"auth_token": conn.http.cookies["auth_token"]})
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    video = asyncio.get_running_loop().create_future()

    @peer.on("track")
    def on_track(track):
        if track.kind == "video" and not video.done():
            video.set_result(track)

    try:
        async with websockets.connect(
            f"wss://{device.netloc}/janus/ws?{query}", ssl=tls,
            subprotocols=["janus-protocol"], close_timeout=1,
        ) as ws:
            async def send(method, **fields):
                transaction = uuid.uuid4().hex
                await ws.send(json.dumps({"janus": method, "transaction": transaction, **fields}))
                return transaction

            async def receive(kind, *, transaction=None, route=None):
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("janus") == "error" and message.get("transaction") == transaction:
                        raise ValueError("Janus rejected the request")
                    if message.get("janus") != kind:
                        continue
                    if route is None and message.get("transaction") == transaction:
                        return message
                    if route is not None and (message.get("session_id"), message.get("sender")) == route:
                        plugin = message.get("plugindata", {}).get("data", {})
                        if plugin.get("error") or plugin.get("error_code"):
                            raise ValueError("Camera stream request failed")
                        if message.get("jsep", {}).get("type") == "offer":
                            return message

            session = (await receive("success", transaction=await send("create")))["data"]["id"]
            try:
                handle = (await receive("success", transaction=await send(
                    "attach", session_id=session, plugin="janus.plugin.ustreamer",
                )))["data"]["id"]
                route = {"session_id": session, "handle_id": handle}
                await send("message", **route, body={"request": "features"})
                await send("message", **route, body={"request": "watch", "params": {
                    "orientation": 0, "audio": False, "video": True,
                    "mic": False, "camera": False, "video_format": 0,
                }})
                # The uStreamer offer is an event without the watch transaction ID.
                offer = (await receive("event", route=(session, handle)))["jsep"]
                # The device can advertise public ICE candidates even on its LAN console.
                sdp = "\r\n".join(
                    line for line in offer["sdp"].splitlines()
                    if not line.startswith("a=candidate:") or line.split()[4:5] and line.split()[4] in device_ips
                ) + "\r\n"
                await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                for transceiver in peer.getTransceivers():
                    transceiver.direction = "recvonly" if transceiver.kind == "video" else "inactive"
                await peer.setLocalDescription(await peer.createAnswer())
                await send("message", **route, body={"request": "start"}, jsep={
                    "type": "answer", "sdp": peer.localDescription.sdp, "trickle": False,
                })
                frame = await (await video).recv()
            finally:
                try:
                    await asyncio.wait_for(send("destroy", session_id=session), timeout=1)
                except (asyncio.TimeoutError, websockets.WebSocketException):
                    pass
    finally:
        await peer.close()

    with frame.to_image() as image, io.BytesIO() as buffer:
        if max_width is not None and image.width > max_width:
            image.thumbnail((max_width, image.height), PILImage.Resampling.LANCZOS)
        image.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()


async def _get_snapshot(conn: Connection, preview: bool, max_width: int, quality: int) -> httpx.Response:
    quality = max(1, min(100, int(quality)))
    max_width = max(1, int(max_width))
    params = {"allow_offline": "true"}
    url = f"{conn.base_url}/api/streamer/snapshot"
    if preview:
        response = await conn.http.get(url, params={
            **params,
            "preview": "true",
            "preview_max_width": str(max_width),
            "preview_quality": str(quality),
        })
        # Older Comet Q firmware can reject preview while serving a valid full frame.
        if response.status_code != 500 and not (conn.live_snapshot_fallback and response.status_code == 503):
            _check_snapshot_status(response)
            if _is_jpeg(response.content):
                return response
    response = await conn.http.get(url, params=params)
    if response.status_code not in (500, 503) or not conn.live_snapshot_fallback:
        _check_snapshot_status(response)
    if response.is_success and _is_jpeg(response.content):
        return response
    if conn.live_snapshot_fallback:
        try:
            jpeg = await asyncio.wait_for(
                _capture_live_jpeg(conn, max_width if preview else None, quality), timeout=15,
            )
        except Exception:
            raise ToolError("Comet Q could not capture a live frame. Check the video in the KVM web console.") from None
        return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})
    raise ToolError("KVM snapshot did not contain a valid JPEG. Check the live stream in the KVM web console.")


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_screenshot(preview: bool = True, max_width: int = 1024, quality: int = 60) -> Image:
    """
    Capture a single JPEG frame from the remote video output and return it
    as MCP image content (so the host displays it directly and Claude reads
    it via vision tokens, not as base64 text).

    Args:
        preview: if True (default), request a downsized JPEG, with a full-frame
            fallback if preview returns HTTP 500 or is not a valid JPEG.
            The named comet-q profile can fall back to its live stream. Full frames may exceed the
            client's tool-output budget.
        max_width: when preview=True, max width in pixels (default 1024).
            Lower this if responses are still too large for your host.
        quality: when preview=True, JPEG quality 1-100 (default 60).

    Returns:
        MCP image content (image/jpeg).
    """
    r = await _get_snapshot(_require_conn(), preview, max_width, quality)
    return Image(data=r.content, format="jpeg")


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_screenshot_to_file(path: str, preview: bool = False, max_width: int = 1920, quality: int = 80) -> dict[str, Any]:
    """
    Capture a JPEG frame and save it to a file on disk. Returns the path and
    byte size. Useful when you want the full-resolution image without sending
    it through the MCP channel at all (e.g. for archival or offline review).

    Args:
        path: absolute file path to write to. Parent directory must exist.
        preview: if True, downsize before saving (faster, smaller file).
        max_width: when preview=True, max width in pixels.
        quality: when preview=True, JPEG quality 1-100.
    """
    r = await _get_snapshot(_require_conn(), preview, max_width, quality)
    with open(path, "wb") as f:
        f.write(r.content)
    return {"path": path, "bytes": len(r.content), "mime_type": r.headers.get("content-type", "image/jpeg")}


# ---- OCR-enhanced screenshot tools ----------------------------------------
# These tools integrate Tesseract OCR so the agent gets structured text +
# coordinates alongside the raw image, eliminating the need for vision models
# to estimate pixel positions (which are unreliable — often off by 30%+).

import io as _io
import shutil as _shutil
import os as _os
import struct as _struct


def _find_tesseract_binary() -> str | None:
    """Locate the tesseract binary on disk (Windows/Linux/macOS)."""
    # 1. Explicit env override
    env_path = _os.environ.get("TESSERACT_PATH") or _os.environ.get("TESSERACT_CMD")
    if env_path and _os.path.isfile(env_path):
        return env_path
    # 2. On PATH already
    found = _shutil.which("tesseract")
    if found:
        return found
    # 3. Common Windows install locations
    if _os.name == "nt":
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if _os.path.isfile(candidate):
                return candidate
    return None


_TESSERACT_BIN: str | None = _find_tesseract_binary()


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Extract width/height from JPEG bytes without decoding the full image."""
    i = 0
    while i < len(data) - 1:
        if data[i] == 0xFF and data[i + 1] in (0xC0, 0xC2):
            h = _struct.unpack(">H", data[i + 5:i + 7])[0]
            w = _struct.unpack(">H", data[i + 7:i + 9])[0]
            return w, h
        i += 1
    return 1920, 1080


def _run_ocr(image_bytes: bytes, search_text: str = "") -> dict:
    """
    Run Tesseract OCR on raw JPEG bytes.
    Returns {"width": W, "height": H, "elements": [...], "tesseract_found": bool}.
    Each element: {text, confidence, x_pct, y_pct, pixel: [cx, cy], box: [x,y,w,h]}.
    """
    from PIL import Image as _PILImage

    img_w, img_h = _jpeg_dimensions(image_bytes)
    result: dict = {
        "width": img_w,
        "height": img_h,
        "elements": [],
        "tesseract_found": _TESSERACT_BIN is not None,
    }

    if _TESSERACT_BIN is None:
        result["error"] = (
            "Tesseract OCR binary not found. Install it: "
            "Windows: choco install tesseract-ocr  |  macOS: brew install tesseract  |  "
            "Linux: apt-get install tesseract-ocr  — or set TESSERACT_PATH env var."
        )
        return result

    import pytesseract as _pyt

    _pyt.pytesseract.tesseract_cmd = _TESSERACT_BIN

    pil_img = _PILImage.open(_io.BytesIO(image_bytes))
    # image_to_data gives word-level bounding boxes + confidence
    data = _pyt.image_to_data(pil_img, output_type=_pyt.Output.DICT)

    elements: list[dict] = []
    search_lower = search_text.strip().lower() if search_text else ""

    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        if not word:
            continue
        conf = float(data["conf"][i]) if data["conf"][i] not in ("", "-1") else 0.0
        if conf < 30:  # skip low-confidence noise
            continue
        if search_lower and search_lower not in word.lower():
            continue
        x = int(data["left"][i])
        y = int(data["top"][i])
        w = int(data["width"][i])
        h = int(data["height"][i])
        cx = x + w // 2
        cy = y + h // 2
        elements.append({
            "text": word,
            "confidence": round(conf, 1),
            "x_pct": round(cx / img_w * 100, 1),
            "y_pct": round(cy / img_h * 100, 1),
            "pixel": [cx, cy],
            "box": [x, y, w, h],
        })

    # Sort by confidence descending, then by position
    elements.sort(key=lambda e: (-e["confidence"], e["y_pct"], e["x_pct"]))
    result["elements"] = elements
    return result


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_ocr_screenshot(search_text: str = "", preview: bool = False) -> dict[str, Any]:
    """
    Capture a full-resolution screenshot from the remote machine AND run Tesseract
    OCR on it. Returns every detected text element with its exact pixel and
    percentage coordinates — ready to feed directly into kvm_mouse_move_pct().

    This eliminates the need for vision models to estimate pixel positions,
    which are unreliable (often off by 30%+). The mouse itself is accurate to
    <0.5%; the bottleneck was always knowing WHERE to click.

    Args:
        search_text: if provided, only return elements whose text contains this
            substring (case-insensitive). Empty = return all detected text.
        preview: if True, capture a smaller preview image (faster but less OCR
            accuracy). Default False = full resolution for maximum OCR accuracy.

    Returns:
        {"width": 1920, "height": 1080, "tesseract_found": true,
         "elements": [
           {"text": "Day", "confidence": 95.2, "x_pct": 39.2, "y_pct": 12.5,
            "pixel": [753, 135], "box": [730, 110, 46, 25]},
           ...
         ]}

    Typical usage:
      1. Call kvm_ocr_screenshot("Day") → get x_pct=39.2, y_pct=12.5
      2. Call kvm_mouse_move_pct(39.2, 12.5)
      3. Call kvm_mouse_click("left", 1)

    Or use kvm_ocr_click() to do all three in one call.
    """
    r = await _get_snapshot(_require_conn(), preview, 1280, 80)
    return _run_ocr(r.content, search_text)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_ocr_click(text: str, button: str = "left", count: int = 1, search_area: str = "") -> dict[str, Any]:
    """
    Find text on the remote screen via OCR and click on it — all in one call.

    Captures a full-resolution screenshot, runs Tesseract OCR to find the exact
    pixel position of the specified text, moves the mouse there, and clicks.

    Args:
        text: the text to search for (case-insensitive substring match).
            Must match at least one word detected by OCR.
        button: mouse button — "left", "right", "middle", "up" (X1), "down" (X2).
        count: number of clicks (1=single, 2=double). Capped at 5.
        search_area: optional — restrict search to a quadrant for disambiguation.
            One of: "top-left", "top-right", "bottom-left", "bottom-right", "" (full screen).

    Returns:
        {"found": true, "text": "Day", "confidence": 95.2,
         "x_pct": 39.2, "y_pct": 12.5, "clicked": true, "button": "left", "count": 1}

    If multiple matches are found, picks the one with highest OCR confidence.
    If search_area is set, further filters to matches in that screen quadrant.
    """
    conn = _require_conn()

    r = await _get_snapshot(conn, preview=False, max_width=1920, quality=80)
    ocr = _run_ocr(r.content, text)

    if not ocr["elements"]:
        return {"found": False, "text": text, "message": "No OCR matches for this text.",
                "tesseract_found": ocr.get("tesseract_found", False)}

    elements = ocr["elements"]

    # Filter by search area quadrant if requested
    if search_area:
        area_filters = {
            "top-left":     lambda e: e["x_pct"] < 50 and e["y_pct"] < 50,
            "top-right":    lambda e: e["x_pct"] >= 50 and e["y_pct"] < 50,
            "bottom-left":  lambda e: e["x_pct"] < 50 and e["y_pct"] >= 50,
            "bottom-right": lambda e: e["x_pct"] >= 50 and e["y_pct"] >= 50,
        }
        f = area_filters.get(search_area)
        if f:
            filtered = [e for e in elements if f(e)]
            if filtered:
                elements = filtered

    # Best match = highest confidence (already sorted, but re-sort after area filter)
    elements.sort(key=lambda e: -e["confidence"])
    best = elements[0]

    # Move mouse to the text center
    x = int(round((best["x_pct"] / 100.0) * 65535 - 32768))
    y = int(round((best["y_pct"] / 100.0) * 65535 - 32768))
    count = max(1, min(5, int(count)))

    async with conn.send_lock:
        await _ws_send_mouse_move(conn, x, y)
        for _ in range(count):
            await _ws_send_mouse_button(conn, button, True)
            await asyncio.sleep(MIN_DOWN_UP_GAP_S)
            await _ws_send_mouse_button(conn, button, False)
            await asyncio.sleep(0.030)

    return {
        "found": True,
        "text": best["text"],
        "confidence": best["confidence"],
        "x_pct": best["x_pct"],
        "y_pct": best["y_pct"],
        "pixel": best["pixel"],
        "clicked": True,
        "button": button,
        "count": count,
        "other_matches": len(elements) - 1,
    }


# ---- Status ---------------------------------------------------------------
class StatusResult(TypedDict):
    connected: bool
    host: str
    held_keys: list[str]
    ws_open: bool


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_status() -> StatusResult:
    """Report current connection state and any keys the server believes are still held."""
    if _conn is None:
        return StatusResult(connected=False, host="", held_keys=[], ws_open=False)
    return StatusResult(
        connected=True,
        host=_conn.base_url,
        held_keys=sorted(_conn.held.keys()),
        ws_open=_conn.ws.state.name == "OPEN" if hasattr(_conn.ws, "state") else not _conn.ws.closed,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Suppress noisy InsecureRequestWarning from urllib3 (we know — self-signed cert).
    import warnings
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    mcp.run()  # stdio
