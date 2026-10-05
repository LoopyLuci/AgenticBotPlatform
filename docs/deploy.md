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

**If [abp_gate](always-on.md) owns port 8787, the same command is a hot
swap instead of a restart**: the build is installed into a new versioned
folder beside the install folder — never over the files the running instance
has open — and the gate is asked to swap to it, so nothing goes offline while
the code is replaced. [What that changes](#when-a-gate-owns-the-port) below.

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

Steps 3 and 5 — stop, restart — only exist because a running app has its own
folder's files open. With a gate in front, neither happens; see below.

## When a gate owns the port

[abp_gate](always-on.md) is one process that owns 8787 and reverse-proxies to
whichever ABP instance is active. While it is up, a deploy has somewhere better
to put the new code than on top of the running instance, and a way to start it
that does not need the old one to go away first. The same `deploy_local.py`
notices and takes that path — the detection is the one the desktop app already
uses: the gate's own unauthenticated `/healthz` has a `gate` field in it, and
the `gate.json` the gate wrote has to name the port being deployed.

There is no flag for this and no opt-in: with a gate in front of the port, a
deploy is a hot swap, because that is what deploying *is* there. To deploy the
old stop-and-restart way, stop the gate first (`abp gate stop`) — which is also
the only way to get the install folder itself refreshed.

What changes:

1. **Install beside, not over.** The build goes into a *versioned folder* next
   to the install folder — `<install-dir>.v1`, `.v2`, … — so the files the
   running instance has open are never the ones being written. Nothing is
   stopped, no lock is waited for, and no `.previous` copy is kept: a new
   folder has nothing to put back to, and the whole point of keeping the old
   folder is that it is still there.
2. **Ask the gate to swap.** `POST /api/instance/swap` on the gate's control
   port (8788), authenticated with the install's own `DASHBOARD_TOKEN` read
   exactly the way the CLI and the desktop app read it (never printed), with
   the same state root the running instance uses — a swap keeps the same data.
   The deploy waits for the whole handover: standby, health, lease release,
   lease take, routing flip, drain, stop of the old instance.
3. **Verify exactly as before**, through the same public port: `/healthz` ok,
   `/openapi.json` identical to `docs/api/openapi.json`, every enabled bot
   instance `live_running` again. Nothing downstream can tell a hot deploy from
   a stopped one — which is the point.
4. **Roll back by swapping, not by copying.** If the swap itself fails, the
   gate has already rolled it back (the outgoing instance is still serving, and
   the 409 says which step failed and why); the deploy reports the gate's own
   words. If a deploy that *did* swap then fails verification — new code, right
   API shape, wrong content — the previous code root is still on disk, so the
   deploy swaps back to it and says the deploy was undone. That is the recovery
   a stop-and-restart deploy gets from its `.previous` copy, and the reason the
   versions are folders rather than copies.
5. **Keep the last two.** `KEEP_VERSIONS = 2`: the version running and the one
   it replaced. Older folders are deleted only after the gate's own registry
   says no live instance has its code root; one that still does is kept past the
   limit and the deploy says so.

What is *not* zero-downtime, with a gate in front:

- **The desktop window itself restarts** — no. It does not restart at all, and
  that is the point: closing or leaving the app open changes nothing. But the
  app binary is a different matter. The window was launched from the install
  folder and is still running from it, so a change to
  `agentic-bot-platform.exe` is installed into the new version's folder and
  **reaches the open window only when the person restarts the app**. The deploy
  says so in a note whenever the exe bytes actually changed, because "the
  deploy passed" is not a claim about the window.
- **The install folder itself is left alone** during a gate deploy. It is the
  folder the app is launched from, and it is the one the versioned folders exist
  to stop being written over. The consequence worth knowing: stop the gate and
  launch the app directly, and it serves whatever bundle was last installed
  *there* — which `/healthz` will report as a `stale` bundle, naming both
  commits. Run the deploy again with the gate stopped to refresh it.
- **A database migration is still not covered.** Both instances run the same
  schema code path during a swap; a release that needs a real migration is a
  different piece of work (see [always-on.md](always-on.md#what-is-not-covered)).
- **`--no-start` and a registered OS service** are overridden when a gate owns
  the port, and the deploy says so: the gate *is* running and serving, so the
  swap is what deploys the build. The service is not what is answering 8787, and
  starting a second copy of it could only fail to bind.

The dry run prints the whole plan either way, including which of the two paths
it would take and that it would stop nothing.

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
| `--install-dir DIR` | install into `DIR` instead of the checkout's `target/release` (the versioned folders sit beside it) |
| `--outside-checkout` | permit a `DIR` that is inside no checkout |
| `--no-build` / `--force-build` | skip / always run `cargo tauri build` |
| `--skip-verify` | do not verify (not recommended: an unverified deploy is the failure mode this script exists to remove) |
| `--no-start` | install without starting the app — nothing can then be verified, and the run says so. With a gate in front of the port the swap is what deploys the build, and the run says that too |
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

With a gate in front there is no `.previous`: the previous *version* is a whole
folder beside the install folder, which is a better thing to look at (it is
complete, and it is a code root the gate can be pointed back at):

```bash
abp_cli instance list                     # which instance is active, from which code root
abp_cli instance logs <name> --follow     # that instance's own stdout/stderr
python scripts/deploy_local.py --dry-run  # what the next deploy would do
```

A failed `cargo tauri build` leaves the install folder exactly as it was: the
build runs before anything is stopped.

## Related

- [install.md](install.md) — installing ABP on a machine for the first time
- [always-on.md](always-on.md) — the gate: one process owning the public port,
  hot swaps, and starting it at logon
- [cicd/README.md](cicd/README.md) — the full local CI/CD pipeline and the
  release gate
- [sandbox-nervous-system.md](sandbox-nervous-system.md) — why no spawn from
  these scripts can put a window on your desktop
