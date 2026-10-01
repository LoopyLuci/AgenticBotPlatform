"""Apps for the file server: one-click containers that work on the shares (what Unraid's Community Applications are),
installed as Docker Compose stacks through bot/docker_mgr.py, so they show up with every other container in ABP.

Each template names the shares it needs (`mounts`: what the app calls it -> where in the container) and its ports;
installing maps the person's chosen shares and ports into a compose file, stored under data/stacks/<name>/ and started.
Pulling an image downloads it from its registry: the install asks first (the page and CLI show the image names).
"""
from __future__ import annotations

import re
from typing import Optional

import yaml

from bot.fileserver import shares
from bot.fileserver.store import FsError

CATALOG: list[dict] = [
    {"id": "jellyfin", "title": "Jellyfin", "category": "Media", "image": "jellyfin/jellyfin:latest", "ports": {"web": "8096:8096"},
     "mounts": {"media": "/media", "config": "/config", "cache": "/cache"}, "about": "Stream your films, shows and music to any device."},
    {"id": "plex", "title": "Plex Media Server", "category": "Media", "image": "plexinc/pms-docker:latest", "ports": {"web": "32400:32400"},
     "mounts": {"media": "/data", "config": "/config"}, "env": {"TZ": "UTC"}, "about": "Media server (a Plex account is needed to claim it)."},
    {"id": "immich", "title": "Immich (server)", "category": "Photos", "image": "ghcr.io/immich-app/immich-server:release",
     "ports": {"web": "2283:2283"}, "mounts": {"photos": "/usr/src/app/upload"}, "about": "Photo and video backup from phones (needs its database stack; see immich.app)."},
    {"id": "photoprism", "title": "PhotoPrism", "category": "Photos", "image": "photoprism/photoprism:latest", "ports": {"web": "2342:2342"},
     "mounts": {"photos": "/photoprism/originals", "config": "/photoprism/storage"}, "env": {"PHOTOPRISM_ADMIN_PASSWORD": "change-me"},
     "about": "Browse and organise photos with on-device AI."},
    {"id": "nextcloud", "title": "Nextcloud", "category": "Cloud", "image": "nextcloud:stable", "ports": {"web": "8082:80"},
     "mounts": {"data": "/var/www/html"}, "about": "Files, calendar, contacts and office in the browser."},
    {"id": "syncthing", "title": "Syncthing", "category": "Sync", "image": "syncthing/syncthing:latest",
     "ports": {"web": "8384:8384", "sync": "22000:22000", "discovery": "21027:21027/udp"}, "mounts": {"data": "/var/syncthing"},
     "about": "Keep folders in sync between devices, peer to peer."},
    {"id": "paperless", "title": "Paperless-ngx", "category": "Documents", "image": "ghcr.io/paperless-ngx/paperless-ngx:latest",
     "ports": {"web": "8000:8000"}, "mounts": {"documents": "/usr/src/paperless/media", "consume": "/usr/src/paperless/consume"},
     "about": "Scan, OCR and search your paper documents (needs Redis: see its docs)."},
    {"id": "vaultwarden", "title": "Vaultwarden", "category": "Security", "image": "vaultwarden/server:latest", "ports": {"web": "8083:80"},
     "mounts": {"data": "/data"}, "about": "A Bitwarden-compatible password manager server."},
    {"id": "homeassistant", "title": "Home Assistant", "category": "Home", "image": "ghcr.io/home-assistant/home-assistant:stable",
     "ports": {"web": "8123:8123"}, "mounts": {"config": "/config"}, "about": "Home automation for every device."},
    {"id": "pihole", "title": "Pi-hole", "category": "Network", "image": "pihole/pihole:latest", "ports": {"web": "8084:80", "dns": "53:53/udp"},
     "mounts": {"config": "/etc/pihole"}, "about": "Network-wide ad blocking DNS."},
    {"id": "adguard", "title": "AdGuard Home", "category": "Network", "image": "adguard/adguardhome:latest",
     "ports": {"web": "3000:3000", "dns": "5353:53/udp"}, "mounts": {"config": "/opt/adguardhome/conf", "work": "/opt/adguardhome/work"},
     "about": "DNS filtering and parental control."},
    {"id": "qbittorrent", "title": "qBittorrent", "category": "Downloads", "image": "lscr.io/linuxserver/qbittorrent:latest",
     "ports": {"web": "8085:8085", "peer": "6881:6881"}, "mounts": {"downloads": "/downloads", "config": "/config"},
     "env": {"WEBUI_PORT": "8085"}, "about": "Torrent client with a web interface."},
    {"id": "sonarr", "title": "Sonarr", "category": "Downloads", "image": "lscr.io/linuxserver/sonarr:latest", "ports": {"web": "8989:8989"},
     "mounts": {"media": "/tv", "downloads": "/downloads", "config": "/config"}, "about": "Organise TV shows."},
    {"id": "radarr", "title": "Radarr", "category": "Downloads", "image": "lscr.io/linuxserver/radarr:latest", "ports": {"web": "7878:7878"},
     "mounts": {"media": "/movies", "downloads": "/downloads", "config": "/config"}, "about": "Organise films."},
    {"id": "gitea", "title": "Gitea", "category": "Developer", "image": "gitea/gitea:latest", "ports": {"web": "3001:3000", "ssh": "2222:22"},
     "mounts": {"data": "/data"}, "about": "Your own Git hosting."},
    {"id": "uptime-kuma", "title": "Uptime Kuma", "category": "Monitoring", "image": "louislam/uptime-kuma:1", "ports": {"web": "3002:3001"},
     "mounts": {"data": "/app/data"}, "about": "Is everything up? Status pages and alerts."},
    {"id": "grafana", "title": "Grafana", "category": "Monitoring", "image": "grafana/grafana-oss:latest", "ports": {"web": "3003:3000"},
     "mounts": {"data": "/var/lib/grafana"}, "about": "Dashboards for everything."},
    {"id": "minecraft", "title": "Minecraft (Java) server", "category": "Games", "image": "itzg/minecraft-server:latest",
     "ports": {"game": "25565:25565"}, "mounts": {"data": "/data"}, "env": {"EULA": "TRUE"},
     "about": "A Minecraft server (EULA=TRUE accepts Mojang's EULA: change it if you do not)."},
    {"id": "mariadb", "title": "MariaDB", "category": "Databases", "image": "mariadb:11", "ports": {"sql": "3306:3306"},
     "mounts": {"data": "/var/lib/mysql"}, "env": {"MARIADB_ROOT_PASSWORD": "change-me"}, "about": "SQL database."},
    {"id": "filebrowser", "title": "File Browser", "category": "Files", "image": "filebrowser/filebrowser:latest",
     "ports": {"web": "8086:80"}, "mounts": {"files": "/srv"}, "about": "Another web file manager over a share."},
]


def catalog() -> list[dict]:
    return CATALOG


def compose_for(app_id: str, name: str, mounts: dict[str, str], ports: Optional[dict[str, str]] = None,
                env: Optional[dict[str, str]] = None) -> str:
    t = next((a for a in CATALOG if a["id"] == app_id), None)
    if not t:
        raise FsError(f"no app {app_id!r} in the catalog")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
        raise FsError("an app name is lowercase letters, digits, - and _")
    vols = []
    for key, inside in t["mounts"].items():
        where = (mounts or {}).get(key, "")
        if not where:
            raise FsError(f"{t['title']} needs a place for {key!r} ({inside}): a share (share:<name>[/sub]) or a folder")
        if where.startswith("share:"):
            sname, _, sub = where[6:].partition("/")
            s = shares.get(sname)
            hit = shares.locate(s, sub) if sub else None
            if s.get("path"):
                host = str(shares.Path(s["path"]) / shares.clean(sub)) if sub else s["path"]
            elif hit:
                host = str(hit[1])
            else:
                br = shares.branches(s)
                if not br:
                    raise FsError(f"share {sname} has nowhere to live")
                host = str(br[0][1] / shares.clean(sub)) if sub else str(br[0][1])
                shares.Path(host).mkdir(parents=True, exist_ok=True)
        else:
            host = where
            shares.Path(host).mkdir(parents=True, exist_ok=True)
        vols.append(f"{host}:{inside}")
    svc = {"image": t["image"], "container_name": name, "restart": "unless-stopped",
           "ports": [(ports or {}).get(k, v) for k, v in t["ports"].items()], "volumes": vols,
           "environment": {**t.get("env", {}), **(env or {})}}
    return yaml.safe_dump({"services": {name: svc}}, sort_keys=False)


def install(app_id: str, name: str, mounts: dict[str, str], ports: Optional[dict[str, str]] = None,
            env: Optional[dict[str, str]] = None) -> dict:
    from bot import docker_mgr
    if not docker_mgr.is_installed():
        raise FsError("Docker is not installed or not running on this machine")
    text = compose_for(app_id, name, mounts, ports, env)
    res = docker_mgr.stack_deploy(name, text)
    return {"name": name, "compose": text, **res}
