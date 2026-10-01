"""Servers at cloud providers, created ready to host sites: a user `abp` with ABP's SSH key and passwordless sudo,
Caddy installed, /var/www owned by `abp` (cloud-init, on the server's first boot). A created server is added as an SSH
account at once, so deploying to it is the same as to any server of your own.

    p = vps.provider(account)
    p.options()                      # regions, sizes (with monthly prices), the default image
    p.create(name, region, size, confirm_monthly=<the price shown>)     # costs money: the price must be confirmed
    p.servers()                      # the servers ABP created (tagged/labelled "abp")
    p.destroy(server_id)

Creating a server is a purchase: create() refuses unless `confirm_monthly` equals the size's monthly price, which the
Hosting page shows and the person confirms (the CLI asks). Agents cannot create or destroy servers.
"""
from __future__ import annotations

import base64
import secrets
import time
from pathlib import Path
from typing import Optional

from bot.hosting import accounts
from bot.hosting.dns import Provider as _Http
from bot.hosting.store import HostingError, root

CLOUD_INIT = """#cloud-config
users:
  - default
  - name: abp
    groups: [sudo, wheel]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - {pubkey}
package_update: true
packages: [curl, gnupg, ca-certificates, debian-keyring, debian-archive-keyring, apt-transport-https]
runcmd:
  - curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  - curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  - apt-get update -y && apt-get install -y caddy
  - mkdir -p /etc/caddy/sites /var/www && chown -R abp /var/www
  - "grep -q 'import /etc/caddy/sites' /etc/caddy/Caddyfile || echo 'import /etc/caddy/sites/*.caddy' >> /etc/caddy/Caddyfile"
  - "sed -i 's/^:80 {{/# :80 {{/' /etc/caddy/Caddyfile"
  - systemctl enable --now caddy && systemctl reload caddy
  - "if command -v ufw >/dev/null; then ufw allow OpenSSH; ufw allow 80/tcp; ufw allow 443/tcp; ufw --force enable; fi"
"""


def keypair() -> tuple[Path, str]:
    """ABP's own SSH key for the servers it creates (ed25519), made once: (private key file, public key line)."""
    d = root() / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    key, pub = d / "abp_ed25519", d / "abp_ed25519.pub"
    if not key.exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        k = Ed25519PrivateKey.generate()
        key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
        pub.write_text(k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode() + " abp-hosting\n")
        try:
            key.chmod(0o600)
            _tighten_windows(key)
        except OSError:
            pass
    return key, pub.read_text().strip()


def _tighten_windows(path: Path) -> None:
    """OpenSSH on Windows refuses a key other users can read: limit the file to its owner (the file only)."""
    import os
    import subprocess
    import sys
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME", "")
    if user:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True, timeout=30)


def user_data() -> str:
    return CLOUD_INIT.format(pubkey=keypair()[1])


class Cloud(_Http):
    image = ""

    def _price_check(self, size: str, confirm_monthly: Optional[float]) -> float:
        price = next((s["monthly"] for s in self.options()["sizes"] if s["id"] == size), None)
        if price is None:
            raise HostingError(f"{self.label} has no size {size!r}")
        if confirm_monthly is None or abs(float(confirm_monthly) - float(price)) > 0.005:
            raise HostingError(f"creating this server costs about {price:.2f} a month at {self.label}: confirm that price to go ahead")
        return price

    def after_create(self, name: str, ip: str) -> dict:
        key, _ = keypair()
        acc = accounts.add("ssh", f"{name} ({self.label})", {"host": ip, "user": "abp", "key_path": str(key), "web_root": "/var/www"})
        return acc


class Hetzner(Cloud):
    label = "Hetzner Cloud"
    base = "https://api.hetzner.cloud/v1"
    image = "ubuntu-24.04"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        n = len(self.req("GET", "/servers?per_page=50").get("servers", []))
        return f"project token works; {n} server(s) in the project"

    def options(self):
        types = self.req("GET", "/server_types?per_page=50").get("server_types", [])
        locs = self.req("GET", "/locations").get("locations", [])
        sizes = []
        for t in types:
            if t.get("deprecation"):
                continue
            for p in t.get("prices", []):
                sizes.append({"id": t["name"], "region": p["location"], "cpus": t["cores"], "ram_gb": t["memory"], "disk_gb": t["disk"],
                              "monthly": round(float(p["price_monthly"]["gross"]), 2), "arch": t.get("architecture")})
        return {"regions": [{"id": l["name"], "name": f"{l['city']}, {l['country']}"} for l in locs], "sizes": sizes, "image": self.image}

    def _price_check(self, size, confirm_monthly, region=None):
        sizes = [s for s in self.options()["sizes"] if s["id"] == size and (region is None or s["region"] == region)]
        if not sizes:
            raise HostingError(f"Hetzner has no size {size} in {region}")
        price = sizes[0]["monthly"]
        if confirm_monthly is None or abs(float(confirm_monthly) - price) > 0.005:
            raise HostingError(f"creating this server costs about {price:.2f} EUR a month: confirm that price to go ahead")
        return price

    def _key_id(self):
        _, pub = keypair()
        for k in self.req("GET", "/ssh_keys").get("ssh_keys", []):
            if k["public_key"].split()[:2] == pub.split()[:2]:
                return k["id"]
        return self.req("POST", "/ssh_keys", json={"name": f"abp-{secrets.token_hex(3)}", "public_key": pub})["ssh_key"]["id"]

    def create(self, name, region, size, confirm_monthly=None, image=None):
        self._price_check(size, confirm_monthly, region)
        r = self.req("POST", "/servers", json={"name": name, "server_type": size, "location": region, "image": image or self.image,
                                               "ssh_keys": [self._key_id()], "user_data": user_data(), "labels": {"abp": "1"}})
        s = r["server"]
        ip = (s.get("public_net", {}).get("ipv4") or {}).get("ip", "")
        return {"id": s["id"], "name": name, "ip": ip, "account": self.after_create(name, ip)}

    def servers(self):
        return [{"id": s["id"], "name": s["name"], "status": s["status"], "ip": (s["public_net"].get("ipv4") or {}).get("ip"),
                 "size": s["server_type"]["name"], "region": s["datacenter"]["location"]["name"]}
                for s in self.req("GET", "/servers?label_selector=abp&per_page=50").get("servers", [])]

    def destroy(self, server_id):
        self.req("DELETE", f"/servers/{int(server_id)}")
        return True


class DigitalOcean(Cloud):
    label = "DigitalOcean"
    base = "https://api.digitalocean.com/v2"
    image = "ubuntu-24-04-x64"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        a = self.req("GET", "/account").get("account", {})
        return f"account {a.get('email', '?')}, droplet limit {a.get('droplet_limit')}"

    def options(self):
        sizes = [{"id": s["slug"], "cpus": s["vcpus"], "ram_gb": s["memory"] / 1024, "disk_gb": s["disk"], "monthly": float(s["price_monthly"]),
                  "regions": s["regions"]} for s in self.req("GET", "/sizes?per_page=200").get("sizes", []) if s.get("available")]
        regions = [{"id": r["slug"], "name": r["name"]} for r in self.req("GET", "/regions").get("regions", []) if r.get("available")]
        return {"regions": regions, "sizes": sizes, "image": self.image}

    def _key_id(self):
        _, pub = keypair()
        for k in self.req("GET", "/account/keys?per_page=200").get("ssh_keys", []):
            if k["public_key"].split()[:2] == pub.split()[:2]:
                return k["id"]
        return self.req("POST", "/account/keys", json={"name": f"abp-{secrets.token_hex(3)}", "public_key": pub})["ssh_key"]["id"]

    def create(self, name, region, size, confirm_monthly=None, image=None):
        self._price_check(size, confirm_monthly)
        d = self.req("POST", "/droplets", json={"name": name, "region": region, "size": size, "image": image or self.image,
                                                "ssh_keys": [self._key_id()], "user_data": user_data(), "tags": ["abp"], "ipv6": True})["droplet"]
        ip = ""
        for _ in range(60):                  # the address arrives a few seconds after the droplet
            d = self.req("GET", f"/droplets/{d['id']}")["droplet"]
            ip = next((n["ip_address"] for n in d["networks"].get("v4", []) if n["type"] == "public"), "")
            if ip:
                break
            time.sleep(3)
        return {"id": d["id"], "name": name, "ip": ip, "account": self.after_create(name, ip) if ip else None}

    def servers(self):
        return [{"id": d["id"], "name": d["name"], "status": d["status"], "size": d["size_slug"], "region": d["region"]["slug"],
                 "ip": next((n["ip_address"] for n in d["networks"].get("v4", []) if n["type"] == "public"), None)}
                for d in self.req("GET", "/droplets?tag_name=abp&per_page=200").get("droplets", [])]

    def destroy(self, server_id):
        self.req("DELETE", f"/droplets/{int(server_id)}")
        return True


class Vultr(Cloud):
    label = "Vultr"
    base = "https://api.vultr.com/v2"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'api_key')}"}

    def verify(self):
        a = self.req("GET", "/account").get("account", {})
        return f"account {a.get('email', '?')}, balance {a.get('balance')}"

    def options(self):
        plans = self.req("GET", "/plans?per_page=500").get("plans", [])
        regions = self.req("GET", "/regions?per_page=500").get("regions", [])
        oses = self.req("GET", "/os?per_page=500").get("os", [])
        ubuntu = next((o["id"] for o in oses if o["name"].startswith("Ubuntu 24.04") and "x64" in o["name"]), None)
        return {"regions": [{"id": r["id"], "name": f"{r['city']}, {r['country']}"} for r in regions],
                "sizes": [{"id": p["id"], "cpus": p["vcpu_count"], "ram_gb": p["ram"] / 1024, "disk_gb": p["disk"],
                           "monthly": float(p["monthly_cost"]), "regions": p.get("locations", [])} for p in plans],
                "image": ubuntu}

    def _key_id(self):
        _, pub = keypair()
        for k in self.req("GET", "/ssh-keys?per_page=500").get("ssh_keys", []):
            if k["ssh_key"].split()[:2] == pub.split()[:2]:
                return k["id"]
        return self.req("POST", "/ssh-keys", json={"name": f"abp-{secrets.token_hex(3)}", "ssh_key": pub})["ssh_key"]["id"]

    def create(self, name, region, size, confirm_monthly=None, image=None):
        self._price_check(size, confirm_monthly)
        i = self.req("POST", "/instances", json={"region": region, "plan": size, "os_id": image or self.options()["image"], "label": name,
                                                 "hostname": name, "sshkey_id": [self._key_id()], "tags": ["abp"],
                                                 "user_data": base64.b64encode(user_data().encode()).decode()})["instance"]
        ip = i.get("main_ip", "")
        for _ in range(60):
            if ip and ip != "0.0.0.0":
                break
            time.sleep(3)
            ip = self.req("GET", f"/instances/{i['id']}")["instance"].get("main_ip", "")
        return {"id": i["id"], "name": name, "ip": ip, "account": self.after_create(name, ip)}

    def servers(self):
        return [{"id": i["id"], "name": i["label"], "status": i["status"], "ip": i["main_ip"], "size": i["plan"], "region": i["region"]}
                for i in self.req("GET", "/instances?tag=abp&per_page=500").get("instances", [])]

    def destroy(self, server_id):
        self.req("DELETE", f"/instances/{server_id}")
        return True


class Linode(Cloud):
    label = "Linode"
    base = "https://api.linode.com/v4"
    image = "linode/ubuntu24.04"

    def headers(self):
        return {"Authorization": f"Bearer {accounts.secret(self.acc, 'token')}"}

    def verify(self):
        return f"user {self.req('GET', '/profile').get('username', '?')}"

    def options(self):
        types = self.req("GET", "/linode/types").get("data", [])
        regions = self.req("GET", "/regions").get("data", [])
        return {"regions": [{"id": r["id"], "name": r.get("label", r["id"])} for r in regions],
                "sizes": [{"id": t["id"], "cpus": t["vcpus"], "ram_gb": t["memory"] / 1024, "disk_gb": t["disk"] / 1024,
                           "monthly": float(t["price"]["monthly"])} for t in types], "image": self.image}

    def create(self, name, region, size, confirm_monthly=None, image=None):
        self._price_check(size, confirm_monthly)
        _, pub = keypair()
        i = self.req("POST", "/linode/instances", json={
            "label": name, "region": region, "type": size, "image": image or self.image, "authorized_keys": [pub],
            "root_pass": secrets.token_urlsafe(24) + "aA1!",   # required by Linode; nobody uses it (key login only)
            "tags": ["abp"], "metadata": {"user_data": base64.b64encode(user_data().encode()).decode()}})
        ip = (i.get("ipv4") or [""])[0]
        return {"id": i["id"], "name": name, "ip": ip, "account": self.after_create(name, ip)}

    def servers(self):
        return [{"id": i["id"], "name": i["label"], "status": i["status"], "ip": (i.get("ipv4") or [None])[0], "size": i["type"],
                 "region": i["region"]} for i in self.req("GET", "/linode/instances?page_size=500").get("data", []) if "abp" in (i.get("tags") or [])]

    def destroy(self, server_id):
        self.req("DELETE", f"/linode/instances/{int(server_id)}")
        return True


_CLASSES = {"hetzner": Hetzner, "digitalocean": DigitalOcean, "vultr": Vultr, "linode": Linode}


def provider(acc: dict | str, transport=None) -> Cloud:
    if isinstance(acc, str):
        acc = accounts.get(acc)
    cls = _CLASSES.get(acc["provider"])
    if not cls:
        raise HostingError(f"{acc['provider']} does not create servers here (Hetzner, DigitalOcean, Vultr and Linode do)")
    return cls(acc, transport)
