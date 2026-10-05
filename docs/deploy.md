# Deploying this checkout onto your own machine

One command:

```bash
python scripts/deploy_local.py            # build (or reuse), install, restart, verify
python scripts/deploy_local.py --dry-run  # print exactly what it would do, touch nothing
```

It ends with the app you run executing the code in this checkout, or with
the files it was about to install put back and the previous app running
again. `scripts/local_pipeline.py`'s deploy step (i.e. `git push`) is this
same code, so a push and a person cannot deploy differently.

## Why it exists

`cargo tauri build` writes its output where `CARGO_TARGET_DIR` says. Tauri
copies every `bundle.resources` entry into the *build* directory
(`$CARGO_TARGET_DIR/release/`), but the app is launched from
`<checkout>/desktop-app/src-tauri/target/release/` — and a release build runs
the Python **bundled next to its own exe**
(`desktop-app/src-tauri/src/lib.rs`'s `resolve_paths`), not the source tree.

So with `CARGO_TARGET_DIR` set to anything else, the old deploy sequence
(rebuild, relaunch the exe) installed *nothing*: the app came back serving
whatever copy of `bot/` was last mirrored into its own folder, days-old, with
`git` cheerfully reporting HEAD as current and every restart "succeeding".
That is what this script fixes.

## What one run does

1. **Resolve.** The build directory from `CARGO_TARGET_DIR`; the install
   directory from `--install-dir` / `$ABP_INSTALL_DIR`, defaulting to the
   checkout's own `desktop-app/src-tauri/target/release`.
2. **Build.** `cargo tauri build`, or nothing at all when the build already in
   the profile directory is at least as new as every input that goes into it
   (`--force-build` to override, `--no-build` to skip entirely).
3. **Stop.** Everything running out of the install folder — the app, the
   backend it spawned, an MCP server a client left running from the same
   bundled venv — found by the executable's own path, so an orphan whose
   parent is already gone is found too. It is the thing holding `.pyd` files
   open that would otherwise make the copy fail.
4. **Install.** Every `bundle.resources` destination **read from
   `tauri.conf.json`**, plus the app binary, copied out of the build into the
   install folder. The list is never written in the script, so a resource
   added to the app can't leave the deploy behind the build.
5. **Restart.** Through `explorer.exe` on Windows, so the app gets the user's
   own interactive environment exactly as a double-click would (and no console
   window ever appears). A registered OS service — see
   `scripts/install_task.ps1`, `install_service.sh`, `install_service_macos.sh`
   — is restarted through its own service manager instead of starting a second
   copy. If nothing was running out of the install folder to begin with, the
   files are installed and nothing is started: the next launch runs this
   commit, and opening a GUI app you deliberately closed is not a deploy's
   decision. The run says plainly that it could not verify that.
6. **Verify**, inside one deadline:
   - `/healthz` answers `status: ok`;
   - `/openapi.json` has **exactly** the paths in `docs/api/openapi.json`
     (`scripts/export_openapi.py` keeps that honest) — an app serving a
     different API is a failed deploy, however healthy it looks;
   - every **enabled** bot instance is `live_running` again on `/api/bots`,
     authenticated with the install's own `DASHBOARD_TOKEN` (asked of the
     bundled interpreter, so it is the right install's token; never printed).
7. **Roll back** on any failure after the install: the files that were just
   installed are restored from the one previous copy kept beside the install
   folder, and the app is started again on those. `--no-rollback` to leave a
   failed deploy in place for debugging.

## What it will not touch

- **`.env`, `data/`, `logs/`** are never installed into, in any configuration.
- **`config/`** is subtler. When the bundle sits inside a checkout,
  `bot/envfile.py`'s `_checkout_behind_build_output()` pins the state root to
  that checkout — so the running app reads the *checkout's* live
  `config/backends.yaml`, and the copy bundled beside the exe is dead weight.
  Installing it would overwrite your own settings for nothing, so the deploy
  skips it and says so. It is only installed into a folder that **is** an
  install's own state root (a real install outside a checkout), which would
  otherwise have no config at all.
- **A target folder outside every checkout** is refused unless you name it:
  `--install-dir <path> --outside-checkout`. A bundle there is its own install
  with its own `.env`/`config`/`data`, and installing over it replaces a real
  install's state.

## Seeing staleness from anywhere

The build records the commit it was built from in `.abp_build.json`, beside the
staged code and shipped as a `bundle.resources` entry. The running app:

- logs it at every boot, and logs a **WARNING** naming
  `python scripts/deploy_local.py` when the bundle's commit is not the
  checkout's HEAD;
- serves it on `/healthz` (unauthenticated, like the rest of that route):

```console
$ curl -fsS http://127.0.0.1:8787/healthz | python -m json.tool
{
    "status": "ok", "db_ok": true, "server_id": "...",
    "bundle": {
        "commit": "3f1c0a9e", "commit_date": "2026-10-04",
        "built_at": "2026-10-04T12:34:56+00:00", "dirty": false,
        "checkout_commit": "3f1c0a9e", "stale": false
    }
}
```

`stale` is `true`/`false` when both commits are known and `null` when either
side is not — an installed app with no checkout behind it has nothing to be
stale against. `GET /api/overview` reports the same pair as `bundle_commit` /
`bundle_stale`, next to `app_commit` (the checkout's HEAD), which is what the
dashboard header already shows.

The deploy reports the same thing after every run: a stale bundle is a loud
warning rather than a rollback trigger, because rolling back would restore the
same stale files.

## Flags

| Flag | What it does |
|---|---|
| `--dry-run` | print every step, install nothing, start nothing, verify nothing |
| `--install-dir DIR` | install into `DIR` instead of the checkout's `target/release` |
| `--outside-checkout` | permit a `DIR` that is inside no checkout |
| `--no-build` / `--force-build` | skip / always run `cargo tauri build` |
| `--skip-verify` | do not verify (not recommended: an unverified deploy is the failure mode this script exists to remove) |
| `--no-start` | install without starting the app — nothing can then be verified, and the run says so |
| `--skip-bots` | verify health and the API, but not the bot instances |
| `--no-rollback` | leave a failed deploy in place |
| `--port N` / `--timeout S` | dashboard port (8787) and how long to wait for the app to come up (300s) |

Exit code: `0` deployed and verified, `1` failed (and rolled back), `2` refused
before touching anything.

## Debugging a failure

The run prints what it was about to copy and why each verification failed. If
the previous copy itself is the problem it lives in `<install-dir>.previous`
(here: `desktop-app/src-tauri/target/release.previous`) with a
`.abp_deploy_previous.json` manifest — the same shape the app itself was in
before the deploy, ready to copy back by hand.

A failed `cargo tauri build` leaves the install folder exactly as it was: the
build runs before anything is stopped.

## Related

- [install.md](install.md) — installing ABP on a machine for the first time
- [cicd/README.md](cicd/README.md) — the full local CI/CD pipeline and the
  release gate
- [sandbox-nervous-system.md](sandbox-nervous-system.md) — why no spawn from
  these scripts can put a window on your desktop
