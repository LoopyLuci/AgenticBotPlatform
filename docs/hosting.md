# ABP Web Hosting

Put a website on the internet from this machine, a VPS, a server of your own, or a hosting provider. You can do all of it from:

- the **Hosting** page (dashboard and desktop app);
- `abp host …`;
- the TUI's Hosting screen (`n`);
- MCP clients;
- ABP's agents, through their `hosting_*` tools.

The code lives in `bot/hosting/`. The API is `bot/dashboard/hosting_api.py`, under `/api/hosting`.

## The model

A **site** is something to serve plus the names it answers to:

| kind       | what it serves                                                                          |
|------------|-----------------------------------------------------------------------------------------|
| `static`   | a folder of files, optionally produced by a build (`npm run build` → `dist`)             |
| `proxy`    | an app on this machine or the LAN (`http://127.0.0.1:3000`), WebSockets included        |
| `redirect` | every path to another address (301)                                                     |

Each site can also have:

- HTTPS: `auto`, `self-signed` or `off`;
- a basic-auth password;
- custom headers;
- deploy targets.

**Going live** means choosing an exposure mode. ABP turns the mode into a plan of steps (`plan`), runs them (`go_live`) and checks the result from outside (`check`):

| mode                | steps                                                                                     | when                                       |
|---------------------|-------------------------------------------------------------------------------------------|--------------------------------------------|
| `cloudflare-tunnel` | edge running → tunnel created/reused through the API → ingress + proxied CNAMEs → `cloudflared` runs | no ports to open; works behind CGNAT       |
| `tailscale-funnel`  | edge running → `tailscale funnel` to the edge → `https://<machine>.<tailnet>.ts.net`      | no domain needed                           |
| `port-forward`      | edge → A records on the public IP (kept current: dynamic DNS) → UPnP forwards 80/443 → Let's Encrypt (http-01) | a home router that forwards                |
| `direct`            | edge → A records → certificate                                                            | the machine has a public IP                |
| `server`            | build → upload over SSH (atomic `current` switch, five releases kept) → Caddy block → A records | a VPS or a server of your own              |
| `provider`          | build → deploy (Netlify, Vercel, Cloudflare Pages, GitHub Pages, FTP) → custom domain at the provider + CNAME | managed hosting                            |
| `lan`               | edge only                                                                                 | home network only                          |

`recommend()` chooses a mode from the network it finds:

- a public IP, a home router, or double/carrier-grade NAT, detected by comparing the router's own WAN address (UPnP) with what the internet sees;
- which accounts are connected.

## Pieces (`bot/hosting/`)

| module     | what it does |
|------------|--------------|
| `accounts` | Provider credentials, sealed with the vault key (`bot/vault.py`). They are never listed or returned and never given to agents. `verify()` makes one read-only call. |
| `dns`      | One record-set interface over 12 providers: Cloudflare, DigitalOcean, Hetzner (Cloud API), Vultr, Linode, Porkbun, deSEC, Gandi, Route 53 (SigV4), Duck DNS, Netlify and Vercel. `set()` makes a name/type hold exactly the given values, updating stale records in place. A CNAME replaces that name's address records. |
| `netinfo`  | Public IPv4/IPv6, the LAN address, the NAT situation, DNS-over-HTTPS lookups (Cloudflare, then Google), propagation checks, HTTP probes with TLS validity. |
| `upnp`     | Router port forwards over UPnP IGD, written with the standard library only. ABP removes only the forwards it described as its own. |
| `edge`     | ABP's web server, `python -m bot.hosting.edge`. It routes by host from `sites.json`, re-read when it changes. It also serves ACME HTTP-01 and redirects HTTP to HTTPS. Static sites get an SPA fallback, clean URLs, ETag/Range, gzip and immutable caching for fingerprinted assets. Proxied apps get X-Forwarded-* headers and WebSockets. Certificates are chosen per host by SNI; a self-signed one is used until a real one exists. |
| `acme`     | An RFC 8555 client covering Let's Encrypt, ZeroSSL and Google (with EAB) or any directory URL. It handles HTTP-01 through the edge and DNS-01 through any connected DNS account, including wildcards. It renews 30 days before expiry. It refuses to create a CA account until the person agrees to the CA's terms. |
| `caddy`    | The alternative engine for this machine. It also writes the per-site block on servers. |
| `tunnels`  | Cloudflare Tunnel (API-managed, token sealed) and Tailscale Funnel. `cloudflared` is downloaded from Cloudflare's GitHub releases only when a person presses Install. |
| `deploy`   | SSH (tar over the connection, atomic release switch, rollback), FTP/FTPS (only changed files), Netlify (file digests), Vercel (SHA-1 uploads), Cloudflare Pages (`wrangler` via npx), GitHub Pages (force-push a branch; the token goes in an environment variable, never on the command line). |
| `vps`      | Hetzner, DigitalOcean, Vultr and Linode: sizes with monthly prices, plus create and destroy. Cloud-init sets up user `abp` (ABP's ed25519 key, passwordless sudo), Caddy and a firewall. A new server becomes an SSH account. |
| `service`  | Sites, build, plan, go live, publish, check, dynamic DNS, renewals, and keeping the edge and the tunnel running (`run_forever`, started by `bot/main.py`). |
| `procs`    | The detached processes (the edge, `cloudflared`, Caddy): pid files, logs, stop. |

State lives in `data/hosting/` (or `ABP_HOSTING_DIR`): `sites.json`, `accounts.json`, `settings.json`, `tunnels.json`, `certs/<host>/`, `acme/`, `ssh/`, `logs/`, `run/`, `bin/`.

## Safety

- Every route needs the dashboard token.
- Agents can create, edit and take sites live, publish them, and change DNS. Each of those actions asks the person first.
- Agents cannot read or add credentials, create or destroy paid servers, or agree to a CA's terms.
- Creating a server needs the person to confirm its monthly price. Destroying one needs its name typed.
- Removing a router forward refuses any forward ABP did not make.

## What has been verified, and how

| What | How it was verified |
|------|---------------------|
| The edge | Run as a real process in `tests/test_hosting.py` and live: static files, SPA fallback, clean URLs, Range, gzip, caching, path-traversal refusal, a basic-auth proxy with forwarded headers, a WebSocket through the proxy, redirects, 421 for unknown hosts, HTTPS with a per-host SNI certificate, and adding a domain with no restart. |
| `netinfo` | Live: public IP, DoH lookups, the NAT verdict. On the home network UPnP was not answered, and the advice correctly says to forward ports in the router's page or use a tunnel. |
| ACME | Live against Let's Encrypt **staging**: directory and nonce, JWS verification, and refusal without agreement. A full issuance needs a domain pointed here, or Pebble, LE's test CA (a Docker image to download). That has not run yet. |
| Provider APIs | Checked against stateful stand-ins of their APIs (Cloudflare, DigitalOcean, deSEC, an ACME directory), because a real run needs the person's accounts. Each provider's first real use should start with **Verify** on the Accounts tab. |
