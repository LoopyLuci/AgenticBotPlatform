# Setting up a real TURN server for the mesh WebRTC fallback

The Android app's mesh APK transport tries, in order: a direct LAN socket,
then a WebRTC data channel (STUN-assisted peer-to-peer), then the server
relay. STUN alone can't get two devices talking when either one sits
behind a symmetric NAT or a strict firewall — that's what a TURN server
fixes, by relaying the actual bytes when a direct path can't be found.

BotServer does **not** run a TURN relay itself. It only mints short-lived
credentials (`bot/turn.py`, `GET /api/turn/credentials`) for a real TURN
server you run — [coturn](https://github.com/coturn/coturn) is the
standard open-source choice and is what this doc assumes.

## 1. Install coturn

- Debian/Ubuntu: `sudo apt install coturn`
- Windows: build from source or run it in WSL/a container — there's no
  official native Windows binary.
- Docker: `docker run -d --network=host coturn/coturn`

## 2. Pick a shared secret

Generate one random secret — this is what BotServer and coturn both use to
independently verify credentials, with nothing else shared between them:

```bash
openssl rand -hex 32
```

## 3. Configure coturn (`/etc/turnserver.conf`)

```
listening-port=3478
fingerprint
use-auth-secret
static-auth-secret=<the secret from step 2>
realm=botserver.local
# TLS is optional but recommended if this box has a real certificate:
# tls-listening-port=5349
# cert=/etc/coturn/cert.pem
# pkey=/etc/coturn/pkey.key
```

`use-auth-secret` + `static-auth-secret` is coturn's REST API auth mode —
it means coturn needs no user database at all; it just recomputes the same
HMAC BotServer used to mint each credential and checks it matches.

Restart coturn: `sudo systemctl restart coturn` (or re-run the container).

Make sure UDP/TCP port 3478 (and 5349 if using TLS) are reachable from the
internet — a TURN server sitting behind the same NAT it's meant to help
traverse defeats the purpose.

## 4. Configure BotServer

In the dashboard's Control Center → **TURN Server (WebRTC mesh relay)**
card (mirrored in the desktop app):

- **Shared secret**: the same value from step 2.
- **TURN URLs**: `turn:<your-host>:3478?transport=udp,
  turn:<your-host>:3478?transport=tcp` (comma-separated; add
  `turns:<your-host>:5349?transport=tcp` too if you set up TLS).
- **Credential lifetime**: how long each minted credential stays valid —
  3600 seconds (the default) is generous for a single APK transfer.
- **Enabled**: flip on last, once the URLs/secret are saved.

Or via the API directly:

```bash
curl -X POST http://localhost:8080/api/config/set \
  -H "X-Dashboard-Token: $DASHBOARD_TOKEN" -H "Content-Type: application/json" \
  -d '{"path": ["turn", "secret"], "value": "<the secret from step 2>"}'
curl -X POST http://localhost:8080/api/config/set \
  -H "X-Dashboard-Token: $DASHBOARD_TOKEN" -H "Content-Type: application/json" \
  -d '{"path": ["turn", "urls"], "value": ["turn:your-host:3478?transport=udp", "turn:your-host:3478?transport=tcp"]}'
curl -X POST http://localhost:8080/api/config/set \
  -H "X-Dashboard-Token: $DASHBOARD_TOKEN" -H "Content-Type: application/json" \
  -d '{"path": ["turn", "enabled"], "value": true}'
```

## 5. Verify

`GET /api/turn/credentials` (with a valid dashboard token or paired
device's API key) should now return `{"enabled": true, "urls": [...],
"username": "...", "credential": "...", "ttl": 3600}` instead of
`{"enabled": false}`. The Android app fetches this automatically before
each WebRTC mesh connection attempt — no app update needed once the
server side is configured.

## Notes

- The shared secret is never echoed back by `GET /api/config` or the
  config-history audit trail — both redact it — so it's safe to leave the
  dashboard open without exposing it.
- Without this configured, mesh transfers still work over STUN alone for
  most home/office networks; TURN only matters for the harder NAT cases
  (carrier-grade NAT, symmetric NAT, corporate firewalls).
