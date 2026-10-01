"""ABP Web Hosting: put a website on the internet from this machine, a VPS, a personal server, or a hosting provider.

A *site* is something to serve (a folder of files, a build of a project, a local app on a port, or a container) plus
the names it answers to and where it is served from. Everything that takes a site from "a folder" to "https on my
domain" is here, each piece usable alone:

    accounts   the provider accounts a person connects (Cloudflare, DigitalOcean, Hetzner, Netlify, Vercel, an SSH
               server, an FTP host...), their secrets sealed with the vault key (bot/vault.py), each one verifiable
    dns        one interface over every DNS provider: zones, records, set a record set, delete one
    netinfo    this machine's public IPv4/IPv6, its LAN address, whether it is behind NAT / CGNAT, DNS lookups over
               DNS-over-HTTPS (what the world sees, not this machine's cache), port reachability
    upnp       ask the router to forward a port (UPnP IGD), and list / remove the forwards ABP made
    edge       ABP's own web server: static sites (with SPA fallback, compression, caching), reverse proxy to a local
               port (WebSockets too), per-domain certificates by SNI, HTTP to HTTPS redirects, ACME challenges
    acme       certificates from Let's Encrypt / ZeroSSL / any ACME CA: HTTP-01 through the edge, DNS-01 through any
               dns provider here
    caddy      the alternative engine: a Caddyfile written from the sites, Caddy started and reloaded
    tunnels    reach a site with no open port: Cloudflare Tunnel (created through the API, cloudflared run), Tailscale
               Funnel
    deploy     publish a site's files elsewhere: an SSH server (VPS or your own), FTP/FTPS (shared hosting), Netlify,
               Vercel, Cloudflare Pages, GitHub Pages
    vps        create and destroy servers at Hetzner, DigitalOcean, Vultr and Linode, set up to serve sites (cloud-init)
    service    the sites themselves and "go live": the steps from a site and a domain to a working https address,
               planned, run, and checked

The dashboard (bot/dashboard/hosting_api.py), the desktop app and the dashboard's Hosting page, `abp host ...`, the
TUI's Hosting screen and the agents' hosting_* tools all drive this package; none of them has logic of its own.
"""
