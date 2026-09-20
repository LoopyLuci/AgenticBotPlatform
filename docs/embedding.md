# Embedding ABP in another server (submodule / sidecar)

ABP can run headless next to your own service — as a git submodule you start
as a child process, or as a container. This page is what to set so it behaves
in that role.

## 1. Keep ABP's state out of the checkout: `ABP_HOME`

By default ABP writes its state beside its code. As a submodule that means
your repository's working tree gets a `.env`, a database and logs written into
it — and a read-only checkout can't run at all.

Set `ABP_HOME` to a directory you own, **in the process environment** of the
process that starts ABP (not in ABP's `.env`, which lives *inside* it):

```bash
ABP_HOME=/var/lib/abp \
DASHBOARD_TOKEN="$(cat /run/secrets/abp_token)" \
PYTHONPATH=/srv/yourapp/vendor/abp \
python -m bot.main
```

On first run ABP creates the layout and seeds the default routing config:

| Under `ABP_HOME`      | What it is                                                          |
|-----------------------|---------------------------------------------------------------------|
| `.env`                | provider keys, `DASHBOARD_TOKEN`                                    |
| `config/backends.yaml`| routing/backends (copied from the checkout once; yours after that)  |
| `config/providers.yaml`| custom model providers, incl. API keys — created when you add one |
| `data/`               | SQLite database, attachments, snapshots, agent workspaces           |
| `logs/`               | `bot.log`, `crash_reports/`, `support_bundles/`                     |

`ABP_HOME` holds secrets: give it the same permissions you would a secrets
directory (`chmod 700`, owned by the service user).

What changes when `ABP_HOME` is set, beyond where files go:

* the global `~/.claude/.env` is **never** read (an embedded ABP must not pick
  up another tool's secrets);
* **hot reload is off by default.** It re-executes modules from `bot/`, which
  in a submodule is your dependency — a `git submodule update` while running
  would reload a half-updated package. Restart ABP after updating it. (Set
  `hot_reload_enabled: true` in `config/backends.yaml` to force it on.)

Two instances must not share an `ABP_HOME` (they would share one database).
Give each its own `ABP_HOME` **and** its own `DASHBOARD_PORT`.

Already running ABP standalone? Stop it, then copy `.env`, `config/` and
`data/` into the new `ABP_HOME` and start with the variable set.

## 2. Network and authentication

* `DASHBOARD_HOST` (default `127.0.0.1`) and `DASHBOARD_PORT` (default `8787`)
  are read from the environment. Leave the host on loopback and let your own
  service talk to ABP locally, or put an authenticating reverse proxy in front.
* Supply `DASHBOARD_TOKEN` yourself (a long random value). A token provided in
  the environment is used as-is and ABP does not rewrite any file to store it.
  If you provide none, ABP generates one into `ABP_HOME/.env`.
* **Treat the token as root on the host.** Anyone holding it can add agent
  hooks (shell commands), register MCP servers (spawned processes), install
  plugins (Python) and read provider keys. Paired mobile devices hold a weaker
  key: creating/enabling hooks needs the `unrestricted` device tier.
* `GET /healthz` is unauthenticated and reports whether the database answers.
  It does not report whether bot platforms are connected.

## 3. Read-only checkouts

With `ABP_HOME` set, ABP writes nothing under its own directory — with one
exception: the dashboard's **Customize UI** feature edits the dashboard/desktop
UI files in place. On a read-only checkout it will fail; leave it unused.
Python bytecode caching (`__pycache__`) also wants a writable tree — set
`PYTHONDONTWRITEBYTECODE=1` if yours isn't.

## 4. Updating

Update the submodule, then restart ABP. The database schema upgrades itself on
startup; take a copy of `ABP_HOME/data/` first if the data matters.

## Verifying your setup

```bash
ABP_HOME=/tmp/abp-check PYTHONPATH=/path/to/abp python -c \
  "from bot import envfile, db; db.init_db(); print(envfile.PROJECT_ROOT, db.DB_PATH)"
```

Both paths should be under `/tmp/abp-check`, and `git status` in the checkout
should show nothing new.
