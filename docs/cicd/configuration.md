# Policy and settings — specification

> **Status: specification, not yet implemented.** Nothing on this page changes
> ABP's behaviour today. It fixes the file format, the names and the precedence
> rules so the implementation, the docs and any host integration agree. What
> ABP does **today** about updates: the desktop app's Updates panel checks
> GitHub Releases and downloads a signed installer on request (signature
> verified before it is written); a submodule host updates ABP with
> `git submodule update` and restarts it. See [../embedding.md](../embedding.md).

## One file, three ways to set it

Settings live in a single policy document. The same keys can come from:

1. **Host policy file** — written by whoever operates the machine or the host
   application. Path: `ABP_POLICY_FILE` (default `ABP_HOME/config/policy.yaml`
   when that exists). Keys set here can be **locked**.
2. **Environment variables** — `ABP_<SECTION>_<KEY>` (e.g. `ABP_UPDATE_MODE`).
3. **User settings** — edited in the desktop app, the dashboard, the CLI
   (`abp-cicd policy set …`) or by an agent through the API. Stored in
   `ABP_HOME/data/` and never written into a host-owned file.

**Precedence, highest first:** locked host policy → other host policy →
environment → user settings → profile defaults.

Anything a locked key covers is **read-only to every client**, including the
GUI, the CLI, the dashboard and agents: the API answers a write with `409
policy_locked` and names the file that owns the key. Every change (or refused
change) is recorded in the audit log.

## Profiles

`profile` selects the defaults. It is detected automatically and can be forced:

| Profile | Chosen when | Meaning |
|---|---|---|
| `desktop` | the desktop app or a plain checkout | An interactive machine with a person at it |
| `server` | headless, no `ABP_HOME` | A standalone unattended install |
| `sidecar` | `ABP_HOME` is set, or `ABP_PROFILE=sidecar` | ABP embedded in a host application (submodule or container) |

| Default | `desktop` | `server` | `sidecar` |
|---|---|---|---|
| `update.mode` | `notify` | `download` | `manual` |
| `update.locked` | no | no | **yes** |
| `standby.mode` | `off` | `off` | **`warm`** |
| `standby.locked` | no | no | **yes** |
| `ml.enabled` | `false` | `false` | `false` |
| `telemetry.export` | `off` | `off` | `off` |

In the `sidecar` profile a locked default means: **ABP will not change its own
version or topology on its own, and no UI or agent can change that.** The host
opts in by writing the policy file (below).

## Reference

```yaml
schema: 1                     # policy format version; readers accept older versions
profile: sidecar              # desktop | server | sidecar
lock: keys                    # none | keys | all
                              #   none: every key is user-editable
                              #   keys: only keys listed under `locked:` are read-only
                              #   all:  the whole file is read-only
locked: [update, standby]     # sections or dotted keys (e.g. update.schedule) to lock

update:
  mode: manual                # auto | download | notify | manual | pinned
                              #   auto      download, verify, and install inside a maintenance window
                              #   download  download and verify, then wait for you to apply
                              #   notify    tell you an update exists; download only on request
                              #   manual    never check unless asked (`abp update --check`)
                              #   pinned    never leave this version (see `pin`)
  channel: stable             # stable | beta | security-only
  auto_apply: [security]      # classes allowed to apply without asking, in `auto` mode:
                              #   security | patch | minor | major
  soak_days: 3                # wait this long after a release before adopting it
  pin:
    min: null                 # never go below (blocks rollback past this)
    max: null                 # never go above, e.g. "0.7.x"
  schedule:                   # used by auto (install) and download (fetch); all optional
    timezone: UTC
    windows: ["Sun 03:00-05:00"]          # allowed install times
    blackout: ["2026-12-20..2027-01-02"]  # never install during these dates
    max_per_week: 1
    require_idle_minutes: 10              # no active jobs/sessions for this long
    jitter_minutes: 30                    # spread a fleet's installs
  verify:
    signature: required       # required (only value permitted for install; may not be weakened)
    provenance: preferred     # off | preferred | required (once published)
  rollback:
    automatic: true           # swap back if the post-update health check fails
    health_check_seconds: 120
  source:                     # where updates come from
    kind: github              # github | mirror | none
    mirror_url: null          # an internal mirror (HTTPS only, still signature-verified)

standby:
  mode: warm                  # off | warm | hot
                              #   off   a single instance
                              #   warm  a second instance of the last-known-good version is
                              #         started and idle; promoted on failure
                              #   hot   as warm, plus continuous state replication
  port: 8788                  # the standby's private port (the guardian owns the public one)
  promote_after_seconds: 5    # health failures this long trigger a swap
  crash_loop: {restarts: 3, window_seconds: 120}   # exceeding this swaps to last-known-good
  snapshot_soak_minutes: 30   # a version becomes last-known-good after this long healthy

ml:
  enabled: false              # master switch; false = deterministic rules only
  workers: []                 # e.g. [failure_classifier, flake_detector]
  audit_sampling_percent: 10  # % of runs that ignore the model and run everything
  shadow_only: true           # models observe and log but never influence a run
  max_timeout_multiplier: 3.0 # bound on any learned timeout, relative to the static value

telemetry:
  export: off                 # off | file | otlp   (local-only unless you opt in)
  export_path: null
  retention_days: 90
  screenshots: milestones     # off | milestones | all

integration:
  api_bind: 127.0.0.1         # loopback or a unix socket; anything else needs a proxy
  api_socket: null            # unix socket path, e.g. /run/abp.sock
  state_dir: null             # same meaning as ABP_HOME
  notify:                     # where policy/update/failover events are sent (in addition to the audit log)
    webhook_url: null
    command: null             # an executable the HOST provides; receives the event as JSON on stdin
```

## Invariants (enforced, not configurable)

- **Signature verification** cannot be disabled for installs; `verify.signature`
  accepts only `required`.
- A **mandatory check** in a release or pipeline cannot be skipped by any
  setting, including `ml.*`.
- Downgrading below `update.pin.min` is refused.
- A policy file with an unknown `schema` newer than the running version is
  **ignored with a loud warning and the profile defaults apply** — never
  partially applied.
- An invalid file never takes effect: the last valid policy stays in force and
  the error is reported (hot-reload with validation).

## Examples

**Host that controls updates itself (recommended for a submodule):**

```yaml
schema: 1
profile: sidecar
lock: all          # nothing here can be changed from ABP; the host updates the submodule
update: {mode: pinned}
standby: {mode: warm}
```
The host runs `abp update --check --json` when it wants to know what exists, and
updates the submodule pin with its own tooling.

**Unattended server that takes security fixes only, at night:**

```yaml
schema: 1
profile: server
lock: keys
locked: [update]
update:
  mode: auto
  channel: security-only
  auto_apply: [security]
  soak_days: 2
  schedule: {timezone: Europe/Berlin, windows: ["Mon-Fri 02:00-04:00"], max_per_week: 2}
  rollback: {automatic: true, health_check_seconds: 180}
```

**Personal desktop that downloads updates and lets you choose when to apply:**

```yaml
schema: 1
profile: desktop
update: {mode: download, channel: stable}
```
(No lock: change it from the Updates panel whenever you like.)

## Checking a policy

Planned commands (all read-only unless stated):

```bash
abp-cicd policy show --json        # the effective policy and, per key, which layer set it and whether it is locked
abp-cicd policy validate FILE      # parse and validate without applying
abp-cicd policy set update.mode download   # user-layer change; refused with 409 if locked
abp update --check --json          # what is available; changes nothing
```
