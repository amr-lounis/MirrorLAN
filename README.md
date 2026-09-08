# MirrorLAN

Tiny HTTPS server for LAN screen sharing, with a Tkinter control panel and a minimal WebRTC signaling API.

It serves the pages in `www/` (`Sharer.html`, `Viewer.html`) over TLS, redirects plain HTTP to HTTPS, and exposes a few `/api/*` endpoints so sharers and viewers can exchange offers/answers on the local network. Self-signed ECDSA certificates are generated with the standard library only — no OpenSSL needed.

![MirrorLAN running with LAN addresses](readme/gui-running.JPG)

## Features

- HTTPS static file server + HTTP → HTTPS redirect
- WebRTC signaling API (offers, answers, rooms, sharer heartbeat)
- Tkinter GUI: pick a port, Start/Stop, copy LAN addresses
- Self-signed certs (stdlib only, SANs for LAN IPs)
- Single-file Windows build via PyInstaller (`build.bat`)

## How it works

1. The **sharer** opens `https://<server>/`, types a room name, and presses **Share** — the page captures the screen/window and sends a heartbeat so the room stays listed as live.
2. A **viewer** on another device opens the same address, sees the live room, and presses **Watch** — the page posts a WebRTC offer to `/api/offer`.
3. The sharer claims the offer (`/api/claim`), replies with an answer (`/api/answer`), and the viewer picks it up.
4. Video/audio then flows **directly browser-to-browser** (WebRTC peer connection) — the server only relays the signaling, it never sees the media.

Rooms are in-memory only: a room disappears ~15 s after the sharer closes the page (missed heartbeats), and everything is cleared on server restart. Room names allow `a-z 0-9 - _` only, max 32 chars. Signaling is lightweight polling (1 s ticks, keep-alive connections) — typical join takes ~1–2 s. Unclaimed offers and undelivered answers expire after 90 s so crashed viewers never clog the queue; live viewers refresh automatically.

**Max viewers** (default 1, up to 99) is chosen when creating the room. The sharer serves at most that many viewers — the badge shows `viewers/max` — and extra viewers wait: after ~12 s without a slot the viewer page shows "Waiting for a free slot", then connects automatically when someone leaves. When a viewer closes the page the slot frees instantly; brief network blips get a 5 s grace before the slot is released. If an active viewer's connection drops (server or network), the viewer page retries automatically with backoff (2s…15s) until the stream returns — pressing ✕ (Leave) stops retrying.

## Screenshots

### 1. Start the server

Open the app, pick a port, and press **Start Server**. Copy one of the LAN addresses for the other devices.

![Server stopped](readme/gui-stopped.JPG)
![Server running with LAN addresses](readme/gui-running.JPG)

### 2. Create a room

Open the address in a browser, type a room name, set **Max viewers** (1–99, default 1), and press **Share**.

![New room](readme/rooms-new.JPG)

### 3. Choose what to share

The browser asks what to share — a single window or the entire screen (see [Monitor capture](#monitor-capture) · [Window capture](#window-capture)).

![Share a window](readme/picker-window.JPG)
![Share the entire screen](readme/picker-screen.JPG)

### 4. Sharing live

The sharer view shows the stream with the viewer count on top.

![Sharing](readme/sharer-live.JPG)

### 5. Watch from another device

The room appears as live — press **Watch** to view the shared screen.

![Live room](readme/rooms-live.JPG)

## Monitor capture

Share a full display through the browser picker (`Entire Screen` tab):

1. On the rooms page, type a room name and press **Share**.
2. In the browser dialog, open the **Entire Screen** tab, pick the display, optionally enable **Share with system audio**, and confirm.

Behavior:

- **Frame rate**: requested at ~15 fps (up to 30) — smooth enough for demos and docs while staying light on the LAN.
- **Audio**: system audio is requested when the browser allows it; if the browser refuses audio, sharing continues video-only automatically.
- **Cursor**: the mouse pointer is part of the capture, as rendered by the OS.
- **Viewer count**: the badge on the sharer page shows live WebRTC connections.
- **Stop**: red stop button, the browser's own "Stop sharing" control, or just close the tab — the room is freed immediately (closing the tab also notifies the server, otherwise the room drops after ~15 s of missed heartbeats).
- **Offline-friendly**: peer connections use no STUN/TURN (`iceServers: []`) — everything stays on the LAN and works without internet.

### Extend display to any browser device

Turn any phone, tablet, or TV browser into a wireless second monitor:

1. On Windows, extend your desktop: **Settings → System → Display → Extend these displays** (or `Win+P` → Extend). With a single physical screen, create a virtual one with https://github.com/VirtualDrivers/Virtual-Display-Driver

![Extended displays in Windows settings](readme/extend-display.JPG)
2. In MirrorLAN, press **Share** and pick the extended/virtual display under the **Entire Screen** tab.
3. Open the room from the other device's browser and press **Watch** — it now shows your second screen.

## Window capture

Share one app window instead of the whole screen (browser picker → `Window` tab):

1. On the rooms page, type a room name and press **Share**.
2. In the browser dialog, open the **Window** tab, pick the app window, and confirm.

Behavior:

- The window picture is **isolated**: overlapping windows on your desktop do not leak into the stream.
- **Close the shared window**: capture ends automatically (track-ended detection) — sharing stops and the room is freed, no stuck "live" room.
- **Minimize**: the stream keeps running; what viewers see while minimized depends on the browser (usually the last frame).
- Same URL/heartbeat/viewer-count behavior as monitor mode. Switching between window and screen requires pressing Share again and picking a new source.

## Requirements

- Python 3.10+ (standard library only, no third-party deps to run)
- PyInstaller only for building the `.exe` (installed automatically by `build.bat`)

## Quick start

```bash
# Launch the GUI (port, Start/Stop, copy addresses)
python main.py

# Headless server on default port 443
python main.py --serve

# Headless server on a custom port
python main.py --serve 8443

# Serve another folder
python main.py --serve 8443 --dir ./site
```

First run creates `cert.pem` / `key.pem` next to `main.py` (or next to the `.exe` when frozen). Headless mode prints the LAN addresses to the console.

Then on any device on the same network, open `https://<LAN-IP>/` (or `https://<LAN-IP>:8443/` for a custom port) in a browser. The browser will warn about the self-signed certificate — accept it once per device (or install `cert.pem` as trusted).

GUI buttons:

- **Copy** / **Copy All** — copy one or all LAN addresses to the clipboard
- **Make Cert** — ensure a valid `cert.pem` / `key.pem`: creates them if missing, otherwise renews automatically when expired, expiring (< 30 days), or when your LAN IPs changed. Renewal keeps the same private key; foreign certificates (different name) are never touched

## Build the .exe (Windows)

```bat
build.bat
```

Output: `dist\MirrorLAN.exe` (one file, GUI, no console). Double-click it — the GUI starts and certs are created automatically.

## Project layout

```text
main.py            entry point (GUI or --serve)
core/config.py     all settings in one Config dataclass
core/certs.py      self-signed ECDSA certificates + ensure_default_cert (stdlib only)
core/net.py        local IPs and public URLs
core/signaling.py  thread-safe viewer offer/answer store
core/server.py     https server + http redirect + ServerManager (shared CORS mixin)
core/gui.py        Tkinter control panel
www/shared.css     stage theme shared by Sharer/Viewer
www/shared.js      stage helpers (toast, fullscreen, room parsing, autoplay…)
www/               pages (index.html, Sharer.html, Viewer.html)
```

## Configuration

Defaults live in `core/config.py`:

| Setting | Default | Notes |
|---|---|---|
| `https_port` | `443` | needs admin rights on Windows |
| `http_port` | `80` | best-effort redirect listener |
| `www_dir` | `www/` | served folder |
| `cert_file` / `key_file` | `cert.pem` / `key.pem` | auto-generated |
| `sharer_timeout` | `15` s | room dropped after no heartbeat |

CLI flags: `--serve [PORT]`, `--dir PATH`, `--https-port PORT`, `--http-port PORT`.

## API

- `GET /api/rooms` — list active rooms
- `GET /api/offers?room=` — list viewer offers in a room
- `GET /api/answer?id=&room=` — fetch an answer (404 `not-ready` if missing)
- `POST /api/offer` `{id, sdp, room}` — publish a viewer offer
- `POST /api/answer` `{id, sdp, room}` — publish an answer
- `POST /api/claim` `{room}` — sharer claims the next offer (404 `empty`)
- `POST /api/leave` `{id, room}` — remove an offer/answer
- `POST /api/sharer/heartbeat` `{room}` / `POST /api/sharer/leave` `{room}`

## Security

LAN-trust model — anyone on your local network with the URL can create and watch rooms:

- Traffic is TLS-encrypted, but the certificate is **self-signed** (browsers show a warning until accepted/trusted).
- There is **no password or access code** — for a trusted home/office LAN only, do not expose to the internet.
- Abuse limits: room names `a-z 0-9 - _` (max 32), viewer IDs (max 64), SDP blobs (max 200 KB) — oversized/invalid signaling is rejected (`400`, bodies over 256 KB get `413` with the connection closed).
- The HTTPS API sends `Access-Control-Allow-Origin: *` (handy for local dev, open by design).

## Troubleshooting

- **Port 443 needs admin** — run as administrator, or use a high port (`python main.py --serve 8443`), no admin needed.
- **Windows Firewall prompt** on first start — allow access for private networks so other devices can connect.
- **Browser says "not secure"** — expected for a self-signed cert; accept/continue, or install `cert.pem` as a trusted certificate.
- **"Cannot bind port"** — another app uses the port; pick a different one.
- **Room stays listed after closing** — it drops automatically after ~15 s of missed heartbeats.
- **Moved to another network / IP changed** — just restart the app or press **Make Cert**: the certificate renews itself automatically (same key kept), no manual steps.

### Phone shows a black screen (PC works)

1. **Same Wi-Fi** — the phone must be on the same Wi-Fi network as the PC, not mobile data.
2. **Accept the certificate on the phone** — open `https://<LAN-IP>/` in the phone browser first and proceed past the warning; otherwise nothing loads.
3. **Use Chrome (Android) or Safari (iPhone)**, updated — in-app browsers and old versions may lack WebRTC.
4. **Check the sharer page viewer count** after pressing Watch on the phone:
   - Count goes up but still black → the video path is blocked: disable **AP/client isolation** (or "guest mode") on the router, or try another phone/hotspot.
   - Count stays 0 → the phone never reached the server: recheck steps 1–2 and the IP address.
5. **No sound on the phone** — phones often force muted autoplay; tap the video once to restore sound.

## License

MIT — see [LICENSE](LICENSE).
