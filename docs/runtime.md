# The unit runtime: modular, hot-swappable, parallel

ABP's core is a small kernel (config, database, auth, the dashboard shell, chat routing and this runtime). Everything
else — feature areas, modules, extensions, plugins, WASM plugins, MCP servers — is a **unit**. The runtime loads,
unloads, upgrades and multiplies units while ABP runs, and every one of those changes is one **atomic transaction**:
it either happens completely or not at all, and nothing in flight is dropped.

ABP must run with **zero** units and with **all** of them, and every front end must show exactly the units that are
live, a moment after they change.

## Concepts

| Term | Meaning |
|---|---|
| **Unit** | A pluggable piece with an id (`hosting`, `vm-harness`, `ext:private_project`, `plugin:foo`). Declares what it contributes. |
| **Version** | One build of a unit's code+config. A content hash (`v-3f2a…`) plus an optional human version. |
| **Instance** | A running copy of a unit: `unit@instance` (`hermes-manager@default`, `hermes-manager@bot-7`). Each has its own state namespace, config section and route prefix. The default instance is `@default`. |
| **Slot** | Where an instance's versions live: `active` (serving), `staged` (built, being verified), `standby` (the previous version, kept warm for rollback), `draining` (finishing in-flight work). |
| **Manifest** | The whole desired state of ABP: config, and for every instance its unit, version and settings. Content-addressed (its hash is its id). |
| **State branch** | A named manifest (`live`, `try-new-router`, `minimal`). Several exist at once; one is live. |
| **Generation** | A counter bumped by every commit. Every API answer and UI event carries it. |

## The contract a unit implements

```python
class Unit(Protocol):
    id: str
    def contributions(self, ctx: UnitContext) -> Contributions: ...   # routes, tools, panels, cli, tasks, config overlay
    async def start(self, ctx: UnitContext) -> None: ...             # open resources; may be a no-op
    async def health(self, ctx: UnitContext) -> Health: ...          # ok / degraded / failed + detail
    async def stop(self, ctx: UnitContext, *, deadline: float) -> None: ...
    def export_state(self, ctx) -> dict: ...                          # optional: hand live state to the next version
    def import_state(self, ctx, state: dict) -> None: ...             # optional
```

`Contributions` is data, not side effects: the runtime registers it and can therefore unregister it.

- **routes**: a Starlette/FastAPI router, mounted under the instance's prefix (`/api/<unit>` for `@default`,
  `/api/<unit>@<instance>` otherwise) by the **dynamic dispatcher** (below), never on the app directly.
- **tools**: agent tool specs (registered through `toolspec.register`, removed with `toolspec.unregister`); MCP tools
  are derived from the same list. Non-default instances get suffixed names (`tool@instance`).
- **panels**: UI panel descriptors (id, title, nav group, icon, script, styles, order), served in the UI manifest.
- **cli**: CLI command descriptors.
- **tasks**: background coroutines, run by the runtime's **task supervisor** in the instance's task group, cancelled
  on stop. A unit never starts bare threads or `asyncio.create_task` of its own.
- **config_overlay**: defaults merged into config while the instance is live.

A unit that fails to import, build, start or pass health is marked **failed** with its error. That never stops ABP
or any other unit.

## The transaction engine (the hybrid)

Every change is expressed as "go from the live manifest to a target manifest". Enabling a module, upgrading it,
adding an instance, editing config, hot-reloading changed code, switching branch and rolling back are all the same
operation:

1. **Plan.** Diff live vs target → per-instance ops: `load`, `unload`, `swap` (new version), `reconfigure`.
   `GET /api/runtime/plan?target=<branch>` shows it without doing anything.
2. **Stage, in parallel.** Every `load`/`swap` builds its new instance in the `staged` slot beside whatever is
   active (blue/green): import, `contributions()`, `start()`. Staged routes are reachable only under
   `/api/_staged/<unit>…` for verification; staged tools are not visible to agents yet.
3. **Verify.** `health()` on every staged instance (with a timeout). Any failure → **abort**: staged instances are
   stopped, the live system was never touched, the transaction reports what failed.
4. **Carry state.** For each `swap`: `old.export_state()` → `new.import_state()`. Durable state lives in the
   instance's state namespace (`runtime_state` table keyed by `unit@instance`), which both versions share.
5. **Commit, atomically.** Under one lock: the dispatcher's route table, the tool registry, the panel list and the
   config overlay are replaced by new immutable tables in a single reference swap each, then the generation is
   bumped once. A request sees the old tables or the new ones, never a mix.
6. **Drain.** Replaced instances move to `draining`: requests already inside them finish (the dispatcher counts
   in-flight requests per instance), long-lived connections (WebSockets, SSE) stay pinned to the instance that
   accepted them until they close or the drain deadline (default 30 s) passes, then `stop()`.
7. **Standby.** The previous version of each swapped instance stays as `standby` (stopped but built, its export kept)
   for the rollback window (default 10 min). `POST /api/runtime/rollback` is another transaction back to the previous
   manifest and reuses the standby builds, so it is instant.

Only one transaction commits at a time (a queue); staging work of one transaction runs in parallel.

## Parallel config states (branches)

- `live` is what runs. Any number of other branches are stored manifests.
- `POST /api/runtime/branches {name, from}` forks a branch; edits to a branch (enable/disable units, config, versions,
  instances) do nothing to the live system.
- **Preview:** a branch can be started in parallel as a **shadow**: its differing instances are staged with
  copy-on-write state namespaces and reachable under `/api/_branch/<name>/…`, so it can be tried for real.
- **Switch:** `POST /api/runtime/switch {branch}` = one transaction from live to that branch. The old live manifest is
  kept as the branch `previous` for rollback.

## The dynamic dispatcher

One ASGI app mounted once at startup routes `/api/<unit>[@instance]/…` to the live instance's router via an immutable
table (swapped at commit). An unknown or unloaded unit answers `404 {"error": {"code": "unit_unavailable", "unit": …,
"state": "unloaded|failed|loading"}}` — never a stack trace. In-flight counting and connection pinning live here.

## Events and the UI

- `GET /api/runtime` → generation, units, instances, slots, health, last transaction.
- `GET /api/ui/manifest` → generation + nav entries + panels of live instances (for every front end).
- WebSocket `/api/ws` event `{"type": "runtime.changed", "generation": N, "added": [...], "removed": [...],
  "changed": [...]}` after every commit; `GET /api/runtime/events?since=N` as the polling fallback, answering
  `{"generation": N, "events": [{"generation": n, "added": [...], "removed": [...], "changed": [...]}, ...]}` (events
  newer than `since`, oldest first; a bounded log, so a client too far behind gets the current generation and simply
  refetches the UI manifest).
- Front ends render nav and panel sections **only** from the UI manifest. Panels use a lifecycle:
  `ABP.panels.define(id, {mount(root, ctx), unmount()})`; `ctx` provides `api()`, `every(ms, fn)`,
  `on(target, event, fn)` so the host can remove every timer and listener on unmount. Older panels (a script that
  looks up its `-root` element) are wrapped: unmount removes the section and the script tag, and a new version is
  loaded with a cache-busting `?v=<version>`.
- The UI manifest's shape:
  ```json
  {"generation": 12,
   "panels": [{"id": "vm-harness", "unit": "vm-harness", "instance": "default", "version": "v-3f2a",
               "title": "VM-Harness", "group": "modules", "icon": "server", "order": 40,
               "kind": "builtin", "section": "vm-harness"},
              {"id": "private_project", "unit": "ext:private_project", "instance": "default", "version": "v-91c0",
               "title": "PrivateProject", "group": "extensions", "icon": "shield", "order": 100,
               "kind": "script", "script": "/extensions/private_project/panel.js",
               "styles": ["/extensions/private_project/panel.css"]}]}
  ```
  `builtin` panels already have their nav link and `<section>` in the page, marked `data-unit="<unit>"`; the host shows
  them only while the unit is live. `script` panels are created and removed by the host. Core pages (overview, chat,
  settings, …) carry no `data-unit` and are always shown. Paths are relative to the API base, so the desktop app
  (whose page is not served by ABP) prefixes its `API_BASE`.
- A panel whose unit vanishes while open is unmounted and replaced by a "this module was unloaded" note; a script that
  fails to load shows its error instead of "Loading…".

## Guarantees (each is a test)

1. ABP starts and serves the dashboard, chat and the runtime API with **no units** (`ABP_UNITS=none`).
2. ABP starts with **every** unit, and a unit that fails is reported, not fatal.
3. Load → use → unload → load again, for every unit, leaves no routes, tools, panels, tasks or threads behind.
4. A swap under load drops no request: a client hammering a unit's route during a swap sees only successes.
5. A failed stage or health check leaves the live generation unchanged.
6. Rollback restores the previous behaviour within one transaction.
7. Two instances of one unit keep separate state and can be swapped independently.
8. A branch switch is all-or-nothing; a shadow branch never changes live behaviour.
9. Every front end reflects a commit within a second (event) or one poll (fallback), with no console errors.

## Hot reload and snapshots

- **Hot reload is a swap.** When a changed file belongs to a unit (a feature area's package, an extension's folder, a
  plugin's file), `bot/hotreload.py` hands it to `bot/runtime/reload.py`, which runs the usual transaction: the new
  version id is a hash of the unit's source; an area's code is imported afresh into private module objects beside the
  running ones, health-checked, and installed together with its tools and tasks in the single commit; the old version
  drains and stays as standby, so `POST /api/runtime/rollback` works. If the new source does not compile, import or
  pass health, the old version keeps serving, the failure is the engine's last transaction (`GET /api/runtime`) and a
  `units` entry of `GET /api/hotreload/status`, and the **next** edit reloads normally: a failed unit swap never
  degrades hot reload. A unit that is not running is not imported and registers nothing; its cached code is dropped so
  its next load uses the new source. Kernel and other denylisted modules still need a restart (reported as such).
  Not covered: routes already registered on the app by `bot/dashboard/*_api.py` keep the functions they imported, so a
  change to what a route handler calls needs a restart; only tools, tasks and panels move with the swap.
- **Snapshots include the runtime.** `bot/snapshots.py` saves the live manifest and stored branches (`runtime.json`).
  A restore switches to the saved manifest as one engine transaction (the pre-restore manifest becomes `previous`), so
  it is all-or-nothing and emits `runtime.changed`; it fails, leaving what runs untouched, if a saved unit version can
  no longer be built (its code changed). Config files and the database are replaced with tmp-write + `os.replace`;
  the three parts (config, database, runtime) are not one atomic step.
