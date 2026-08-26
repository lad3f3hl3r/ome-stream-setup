# OME Live Stream Server

Self-hosted live streaming server using [OvenMediaEngine](https://github.com/AirenSoft/OvenMediaEngine) with:

- **Sub-second WebRTC** playback (original quality)
- **LLHLS** with native quality picker (bypass + 1080p / 720p / 480p)
- **1-hour DVR** scrub window
- **PIN-protected viewer page** with dynamic stream detection
- **Secret-prefixed stream keys** to gate unauthorized RTMP pushers
- **Automatic stream tabs** — streams appear/disappear as encoders connect

## Quick start

```bash
# 1. Clone and configure
git clone https://github.com/YOUR_USERNAME/ome-stream-setup
cd ome-stream-setup
cp .env.example .env
nano .env          # fill in your values

# 2. Run setup (Ubuntu 24.04, as root)
chmod +x setup.sh
./setup.sh
```

## OBS configuration

| Setting    | Value                                      |
|------------|--------------------------------------------|
| Server     | `rtmp://YOUR_IP/live`                      |
| Stream key | `SECRET~StreamName` (e.g. `abc~Stage_A`)  |

Stream names use `_` as a word separator — `Stage_A` displays as **Stage A**.

## Viewer page

Visit `https://YOUR_DOMAIN` and enter the PIN. Active streams appear automatically as tabs. Quality selection is in the player controls (gear icon).

## Ports required

| Port | Protocol | Purpose |
|------|----------|---------|
| 80 | TCP | HTTP (redirects to HTTPS) |
| 443 | TCP | HTTPS (viewer page) |
| 1935 | TCP | RTMP ingest |
| 9999 | UDP | SRT ingest |
| 3333 | TCP | WebRTC signaling |
| 3334 | TCP | WebRTC signaling (TLS) |
| 3478 | TCP | TURN relay |
| 8080 | TCP | LLHLS HTTP |
| 8090 | TCP | LLHLS HTTPS |
| 8081 | TCP | OME REST API (internal only) |
| 10000–10009 | UDP | WebRTC media |

## Customisation

| File | Purpose |
|------|---------|
| `nginx/html/config.js` | Grace period, poll interval, stream secret |
| `config/Server.xml` | OME output profiles, DVR duration, bitrates |
| `nginx/html/pins.js` | Viewer PINs (SHA-256 hashes) |

`config.js` changes take effect immediately (no restart needed). `Server.xml` requires `docker compose restart ome`.

## Adding a PIN

```bash
echo -n "YOUR_PIN" | sha256sum | cut -d' ' -f1
# Add the hash to nginx/html/pins.js
```

## Architecture

```
OBS/encoder  →  RTMP :1935  →  OME (network_mode: host)
                                ├── original stream (bypass, WebRTC)
                                ├── _abr stream (multi-rendition LLHLS)
                                └── _audio stream

Browser  →  nginx :443  →  /          (viewer page)
                        →  /api/*     (proxies to OME REST API)
                        →  :8090      (LLHLS segments, direct to OME)
```

## Notes

- OME uses `network_mode: host`; nginx reaches it via `host-gateway`
- DVR segments are stored in `/tmp/ome_dvr/` inside the OME container (lost on restart)
- `~` is the stream name separator — avoid `&`, `?`, `#`, spaces in stream names
- The `&` character works in RTMP keys but OME does not decode `%26` in URL paths; the frontend handles this with a custom encoder
