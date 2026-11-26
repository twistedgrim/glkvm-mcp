# GLKVM MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for GL.iNet GLKVM and compatible PiKVM devices. It exposes keyboard, mouse, and OCR screenshot tools over stdio using MCP `2026-07-28`.

This project is based on [Kenny Peh’s original GLKVM MCP implementation](https://github.com/kennypeh85/glkvm-mcp). It adds CometX input switching, Comet Q snapshot fallback, local SOPS credential profiles, and percentage-based mouse drag.

## ✨ Key Features

- **Full keyboard control** — type text, send key chords (Ctrl+Alt+Del, Win+L, F11), hold keys
- **Precise mouse control** — move, click, scroll, with percentage-based positioning (100% accurate)
- **Screenshot capture** — full-resolution or preview, returned as MCP image content
- **🔧 OCR-powered targeting** — built-in Tesseract OCR integration that:
  - Reads all text on the remote screen with exact pixel + percentage coordinates
  - Lets agents click on UI elements by **text name** instead of guessing coordinates
  - Eliminates the unreliable "vision model estimates pixel position" pattern

## 🎯 Why OCR Integration?

Vision models (GPT-4V, Claude Vision, GLM-4V) are great at *reading* screenshots but terrible at *estimating pixel coordinates* — often off by 30%+. Meanwhile, the KVM mouse is accurate to <0.5%. The bottleneck was always **knowing where to click**, not the clicking itself.

**Before (unreliable):**
```
Agent: "Where is the 'Day' button?" → Vision model: "around 45%, 15%" → Misses
```

**After (OCR-powered):**
```
Agent: kvm_ocr_click("Day") → OCR finds exact position → Clicks dead center → ✅
```

## 📦 Installation

### 1. Install Tesseract OCR (system dependency)

```bash
# Windows
choco install tesseract-ocr
# or download from https://github.com/UB-Mannheim/tesseract/wiki

# macOS
brew install tesseract

# Linux (Debian/Ubuntu)
sudo apt-get install tesseract-ocr
```

### 2. Configure local SOPS credentials

Install `sops` and configure its Age identity on the MCP host. Create an encrypted JSON or YAML file with string fields `host`, `username`, and `password`. The host must use HTTPS. Enter credentials through the SOPS editor; never put the password in tool arguments or shell command arguments.

Set `GLKVM_CREDENTIALS_FILE` to the encrypted file's absolute path in the MCP server environment. `kvm_connect()` decrypts it locally in memory. SOPS must be available on the server's `PATH`. Set `SOPS_AGE_KEY_FILE` in the server environment to your existing Age identity file if SOPS does not find it automatically.

To configure multiple devices, use this structure inside the SOPS editor. The original single-device format still works with `kvm_connect()`.

```yaml
default_profile: cometx
profiles:
  cometx:
    host: https://cometx.example.test
    username: <CometX user>
    password: <CometX password>
  comet-q:
    host: https://comet-q.example.test
    username: <Comet Q user>
    password: <Comet Q password>
```

Call `kvm_connect(profile="comet-q")` to select a device explicitly. With no argument, the server uses `default_profile`. The tool never accepts an ad hoc hostname.

### 3. Configure in your MCP client

Add to your MCP client config (e.g., Claude Desktop `claude_desktop_config.json`, Hermes `config.yaml`):

```json
{
  "mcpServers": {
    "glkvm": {
      "command": "uv",
      "env": {
        "GLKVM_CREDENTIALS_FILE": "/absolute/path/to/credentials.sops.json"
      },
      "args": [
        "run",
        "--script",
        "/path/to/glkvm_mcp.py"
      ]
    }
  }
}
```

Python dependencies (`mcp`, `websockets`, `httpx`, `Pillow`, `pytesseract`, `aiortc`) are auto-installed by `uv run --script` via the inline PEP 723 metadata. Use a client that supports MCP `2026-07-28` over stdio. For Pi with `pi-mcp-adapter`, add `"protocolVersion": "2026-07-28"` to the `glkvm` server entry.

## 🔧 Tools

### Connection
| Tool | Description |
|------|-------------|
| `kvm_connect(profile?)` | Connect to the default or a named device from the local SOPS file |
| `kvm_disconnect()` | Close the session |
| `kvm_status()` | Report connection state and held keys |

### Multiport switch (CometX / compatible PiKVM devices)
| Tool | Description |
|------|-------------|
| `kvm_switch_status()` | Read active port, available inputs, and video/USB link indicators |
| `kvm_switch_port(port, timeout_s?)` | Select a port and wait for confirmed, synchronized selection |

Port numbering is **zero-based**: `port=0` is physical input 1; `port=3` is
physical input 4. The status response includes both `port` and `input_number`.
The tools use the [PiKVM switch API](https://docs.pikvm.org/api/#switch):
`GET /api/switch` and `POST /api/switch/set_active?port=N`.
Devices without that API return an unsupported-device error.

Switching changes the target of video and USB input and leaves that input selected.
Release held keys first. After switching, inspect the returned state and take a
screenshot before sending keyboard or mouse input. A connected video link does
not guarantee a usable signal; a snapshot can still return HTTP 503.
If confirmation times out, inspect status before retrying or sending input.
`timeout_s` bounds the confirmation polling window (default 10, range 0.1–30);
individual HTTP requests also have their own timeout.

```python
original = kvm_switch_status()["active_port"]
try:
    kvm_switch_port(1)  # Physical input 2
    kvm_screenshot()
finally:
    kvm_switch_port(original)
```

### Keyboard
| Tool | Description |
|------|-------------|
| `kvm_send_text(text, wpm?)` | Type a string |
| `kvm_send_keys(combo)` | Send a key chord (e.g., "Ctrl+Alt+Delete", "F5", "Win+L") |
| `kvm_hold_key(key, duration_ms)` | Press and hold a key (for auto-repeat scrolling) |
| `kvm_release_all()` | Force-release all held keys |

### Mouse
| Tool | Description |
|------|-------------|
| `kvm_mouse_move(x, y)` | Move to absolute int16 coordinates |
| `kvm_mouse_move_pct(x_pct, y_pct)` | Move to percentage of screen (0,0 = top-left) |
| `kvm_mouse_click(button?, count?)` | Click at current position |
| `kvm_mouse_scroll(dx?, dy?)` | Scroll the mouse wheel |
| `kvm_mouse_drag_pct(start_x, start_y, end_x, end_y)` | Drag with the left button, using whole-screenshot percentages |

### Screenshot
| Tool | Description |
|------|-------------|
| `kvm_screenshot(preview?, max_width?, quality?)` | Capture JPEG frame as MCP image content |
| `kvm_screenshot_to_file(path, preview?, ...)` | Capture and save to disk |

### Comet Q (GL-RMQ1)

For an attached iPhone or iPad, follow [GL.iNet's setup guide](https://docs.gl-inet.com/kvm/en/user_guide/gl-rmq1/quick_setup_guide/) to enable AssistiveTouch and select the iPhone/iPad video type in GLKVM. Comet Q has no multiport switch API. Connect with `kvm_connect(profile="comet-q")` to let the capture and OCR tools fall back to a receive-only WebRTC frame when the HTTP snapshot contains invalid data or returns HTTP 500 or 503. The fallback uses the same KVM login, needs direct LAN access to the device's WebRTC media port, and does not require a browser. Preview captures from the fallback honor the requested width and JPEG quality.

Verify iOS input in the live stream before using OCR click. The MCP mouse tools use absolute coordinates. Calculate percentages from the whole screenshot, including the black borders around a portrait iPhone. Verify the result with a fresh screenshot before sending more input.

### 🔧 OCR-Enhanced (Tesseract)
| Tool | Description |
|------|-------------|
| `kvm_ocr_screenshot(search_text?, preview?)` | Capture + OCR: returns all text with coordinates |
| `kvm_ocr_click(text, button?, count?, search_area?)` | Find text via OCR and click it — all-in-one |

## 📖 Usage Examples

### Click a button by text name
```python
# One-call: screenshot → OCR → find "Save" → move mouse → click
kvm_ocr_click("Save")
```

### Read all text on screen
```python
# Returns structured JSON with every detected word + coordinates
result = kvm_ocr_screenshot()
# {"width": 1920, "height": 1080, "elements": [
#   {"text": "File", "confidence": 96.3, "x_pct": 5.2, "y_pct": 3.1, ...},
#   {"text": "Edit", "confidence": 95.8, "x_pct": 8.7, "y_pct": 3.1, ...},
#   ...
# ]}
```

### Find specific text and get its coordinates
```python
result = kvm_ocr_screenshot("Submit")
# {"elements": [{"text": "Submit", "confidence": 94.5, "x_pct": 52.3, "y_pct": 87.1, ...}]}
# Then use: kvm_mouse_move_pct(52.3, 87.1) + kvm_mouse_click()
```

### Disambiguate with search area
```python
# If "OK" appears in multiple places, restrict to bottom-right
kvm_ocr_click("OK", search_area="bottom-right")
```

### Traditional screenshot (for vision model analysis)
```python
# Returns MCP image content — agent sees the screenshot directly
kvm_screenshot(preview=True, max_width=1024)
```

## 🏗️ Architecture

```
┌──────────────┐     MCP stdio      ┌─────────────────┐     HTTPS/WSS     ┌──────────┐
│  AI Agent    │ ◄─────────────────► │  glkvm_mcp.py   │ ◄───────────────► │  GLKVM   │
│ (Claude/GPT) │    tool calls       │  (this server)  │   (PiKVM API)     │  Device  │
└──────────────┘                     └─────────────────┘                   └──────────┘
                                            │
                                     Tesseract OCR
                                     (reads screenshot text)
```

The server maintains a persistent WebSocket connection to the GLKVM device for low-latency keyboard/mouse input, and uses HTTP for screenshots and authentication.

## 🔒 Security

- **LAN only** — designed for trusted local networks
- **TLS verification disabled** — the device ships with a self-signed certificate
- **SOPS credentials** — credentials are stored encrypted and decrypted locally in memory; the connection tool accepts no password or host arguments

## 📋 Requirements

- Python 3.11–3.15 (all five versions are tested in CI)
- A GL.iNet GLKVM device running firmware **1.10 or later**, or a compatible PiKVM device on your LAN
- Tesseract OCR installed on the host machine

## License

MIT
