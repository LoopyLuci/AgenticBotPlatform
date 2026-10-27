# Recovery and backup contracts

Shared rules for ABP's trash (agent file removals) and state backup. Each tool works alone. These contracts are what
lets them work together, and with DriveTier's quarantine, without sharing a database.

## C1. Events, not shared data

Each tool keeps its own journal. A tool that changes files or takes an owner-visible action reports it as an event
to the activity log. No tool reads another tool's database.

## C2. Approvals

- An approval is single use, expires after 24 hours, and is bound to a hash of the exact arguments it covers.
- It is refused if the arguments differ when it is used.
- An agent cannot approve, purge, or restore over a live path. These are owner actions: the GUI or a terminal.

## C3. Trash layout

Files an agent removes are moved, not unlinked:

    <data>/trash/<YYYY-MM-DD>/<drive>/<path without its drive>
    <data>/trash/<YYYY-MM-DD>/<drive>/<path without its drive>.trash.json

- `<drive>` is the drive letter of the original path, or `root` where there is none.
- The sidecar holds the original path, workspace, size, SHA-256, session, reason, `trashed_at` (Unix time) and
  `retention_days` (30).
- Restore never overwrites: a path that exists again is refused.
- Purge is owner-only and refused before retention ends.
- DriveTier's quarantine uses the same shape (`_quarantine\<date>\<drive>\<path>`), so either tool can list the other's items.

## C4. State backup manifest

A state backup is a folder staged from the live stores and then stored in a File Server backup repository. The folder
has a `manifest.json` with:

- `format` (1), `created_at` (ISO 8601 UTC).
- `files`: `{path, sha256, size, kind}` for each staged file. `kind` is `file` or `sqlite`. A SQLite entry also carries
  `tables` (row counts).
- `excluded`: `{label, reason}` for each store deliberately left out, such as the vault key.
- `unlisted`: `{path}` for any `*.db` under the data folder that no store claims. This is the coverage audit.

The drill restores the newest state snapshot into a temporary folder and checks every `sha256`, every SQLite
`integrity_check`, and every recorded row count. It never touches live files.

## C5. What can be recovered after a removal

Two mechanisms cover agent removals. They differ in scope.

- **Trash (C3):** per file, restored with `trash_restore` or the GUI. Covers `apply_patch` deletes and renames.
- **Checkpoints:** the agent loop commits the workspace after every dangerous call (`bot/agent_runtime/tool_loop.py`,
  `try_checkpoint`). A rollback resets the whole working tree to an earlier checkpoint. It covers any removal made
  by a dangerous call inside the workspace.

| Removal | Per-file recovery | Whole-tree recovery | Gate |
|---|---|---|---|
| `apply_patch` delete or rename | Trash | Checkpoint rollback (tested) | Approval, unless a rule or mode allows the call |
| `run_shell` `rm` inside the workspace | None | Checkpoint rollback (same path as `apply_patch`; not tested with a real shell) | Approval for each call, unless a rule or the unrestricted tier allows it |
| `run_shell` `rm` outside the workspace | None | None | Approval only |

Decisions:

- Shell commands do not go through the trash. An arbitrary command cannot be intercepted reliably, and checkpoints
  already cover in-workspace removals.
- A removal outside the workspace is not recoverable by ABP. The approval gate is the protection there.
- A checkpoint rollback also discards later edits to other files. When one file is needed, restore it from the trash.
- Checkpoints exist only for the ABP tool loop (the API and custom-model backends). The Claude Code CLI and Hermes
  backends run their own tools and get neither mechanism.
