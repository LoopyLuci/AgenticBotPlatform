# Making a module: abp_modkit

Any project can be an ABP module. `abp_modkit` (it ships inside ABP, standard library only) reads a project, writes the
two files that describe it, and serves its operations. Nothing has to be written by hand, and nothing in the project
has to change.

```bash
abp modules adopt /srv/projects/MyModule     # through a running ABP (also: Modules page -> Add a project)
python -m abp_modkit adopt . --register       # without one; --register adds it to this ABP's config
python -m abp_modkit adopt . --dry-run        # show both files, write nothing
python -m abp_modkit new ./my-tool --lang python   # a new, empty project that is already a module
python -m abp_modkit check .                  # load both files, start a real hub, list operations, bridge MCP, stop
abp modules candidates /srv/projects          # which folders there are projects, and which are modules already
abp modules publish <id> [--push]             # commit the module files; create its private GitHub repo
```

Agents have the same through `module_adopt` (actions `adopt`, `candidates`, `publish`, `forget`).

## What adopting finds

| In the project | Becomes |
|---|---|
| Cargo workspace / packages with binaries | build step, one op per clap subcommand (`<bin>.<sub>`), `tui` / `gui` windows |
| `package.json` | install + build steps, one op per script; Express / Fastify / Hono / Next / Vite servers |
| `pyproject.toml`, `requirements.txt`, loose `.py` | a `.venv`, ops for `[project.scripts]`, argparse / click / typer subcommands, scripts with a `__main__` |
| FastAPI / Flask / aiohttp, axum / actix, Go handlers | the project's **server** (`service.start` / `stop` / `logs`) and one `http` op per route |
| `/v1/chat/completions` among its routes | `[provider] openai = "service"`: ABP offers it as a model provider while it runs |
| a web UI (Vite, Next, static, templates) | `[ui] web = "service"`: ABP shows it in a pane |
| `.psd1` / `.psm1` | one op per exported function, with its parameters as inputs |
| `boot.py` + `main.py` | MicroPython firmware: deploy / run / test with `mpremote` |
| `wrangler.toml` | worker dev / deploy (a library of many workers gets four parameterised ops) |
| Gradle, Nix flake, Makefile, docker compose, Mix | their usual tasks |
| an MCP server (FastMCP, the MCP SDK) | `[mcp] stdio` points at the project's own server instead of the bridge |

`abp-module.toml` is the manifest ABP reads (docs/modules/README.md). `abp-ops.toml` lists the operations; its
header documents the format. Both are yours to edit: adopting again keeps edited summaries, service settings and ops
you added, and leaves a manifest alone once its first line (the generated marker) is removed.

## The hub

`python -m abp_modkit serve --spec abp-ops.toml --project . --home <data>` is the module's control hub (the module
contract, v1): `GET /v1/health`, `GET /v1/operations`, `POST /v1/call/{op}`, `POST /v1/service/stop`, a random
loopback port and token in `<data>/control.json`. Besides the project's own operations it always has:

- `service.status / start / stop / logs`: the project's server (or any long-running process: a chat bot, a worker),
  started in its own process group and stopped with everything it started;
- `service.set_secret`: tokens the project needs, kept in the hub's data folder (mode 600), handed to the process as
  environment variables (`{secret:NAME}`, `{secret?:NAME}` for optional ones) and never returned;
- `jobs.list / get / cancel`: commands marked `background = true`;
- `api.request`: any request to the project's HTTP API; `project.info`: folder, branch, commit, README.

Commands never go through a shell. Inputs become arguments (and `$ABP_OP_ARGS` as JSON); an input with an `enum`
must be one of its values; `cwd` cannot leave the project.

## Modules that use ABP back

```toml
[abp]
connect = true            # preset = "companion-app" by default
```

ABP then mints the module an integration key of its own (Settings -> Integrations shows and revokes it) and gives
its hub, its MCP server and its windows `ABP_URL` and `ABP_KEY`. With the `companion-app` scopes the module can use
ABP's OpenAI-compatible model gateway (`$ABP_URL/api/browser/v1`, `Authorization: Bearer $ABP_KEY`, model `auto` or
`provider/model`) and ABP's modules (`/api/modules`, `X-Dashboard-Token: $ABP_KEY`). It cannot control bots, Docker
or configuration, and never sees credentials.

## Refreshing after upstream changes

`abp modules refresh <id>` (also `GET /api/modules/<id>/refresh`, the `module_refresh` tool, and "Check for updates
(what changed in its operations)" on the module's page) works for a module added from a URL (an overlay) or an adopted
project. It fetches upstream and shows what a refresh would change, writing nothing: operations added, removed and
changed (signature = kind/method/path/inputs, command = argv/cwd/env/timeouts, description = summary) and the manifest
and service fields that changed. `--apply` (`POST`, write auth) fast-forwards the checkout and writes the merged files.

Nothing is reset or discarded: tracked local changes in the checkout (the module files of an adopted project aside) or a
branch that diverged from upstream stop the refresh with a message, everything untouched. Merge rules, on top of the
re-adopt merge above: a hand-written summary, inputs, flags and service settings win over detection; operations you added
stay; an `abp-ops.toml` or `abp-module.toml` without the generated marker is kept exactly as it is (the report lists what
detection finds that it does not list); an operation whose script or route upstream dropped is removed, unless you edited
it (it stays and is reported as `kept_stale`); a summary still as detection wrote it follows upstream's new text. A
manifest with the generated marker is regenerated: delete the marker line to own it.

## The catalog (sharing overlays between machines)

A catalog is a git repo that holds module overlays, so another machine can install them. One folder per entry:

```
<entry>/entry.toml        name (= the folder), upstream (https git URL), ref (optional: tag, branch or commit to pin),
                          description, platforms, tools
<entry>/abp-module.toml   the overlay files, exactly as in catalog/ or data/module-overlays/
<entry>/abp-ops.toml
index.json                generated from the entries on every publish; never edit it
```

Catalogs are listed in config; there are none by default (a private catalog's URL belongs in your private config):

```yaml
modules:
  catalogs:
    - {name: mine, url: "https://github.com/<owner>/<catalog-repo>.git", branch: main}
```

- `abp modules catalog sync` fetches each catalog (shallow) into `data/module-catalogs/<name>`, which ABP only reads.
  `abp modules catalog list` shows the entries (also `GET /api/modules/catalog`, and the Catalog section of the Modules page).
- `abp modules catalog install <entry> [--catalog <name>]` (`POST /api/modules/catalog/install`, the Install button after a
  confirm; a job) clones the entry's upstream shallowly into the normal module location (`data/modules/<Name>`), at its
  pinned `ref` if it has one, copies the overlay to `data/module-overlays/<id>`, runs `abp_modkit check --project` and
  registers it. The upstream must be https on an allowed host (the skills fetch's list: github.com, gitlab.com, codeberg.org,
  bitbucket.org, plus `native_agent.skills.allowed_hosts`). It refuses if a module with that id, or a non-empty folder where
  its checkout goes, exists, and removes what it made if the clone fails. A platform or tool the entry names that this
  machine lacks is a warning in the log.
- `abp modules catalog publish <module> --catalog <name> [--ref <ref>]` copies the module's overlay files (only those: for an
  adopted project, only its abp-module.toml and abp-ops.toml; `[checkout] dir` is dropped as it is a machine path), writes
  `entry.toml` from its manifest, regenerates `index.json` and commits on a new branch `publish/<id>-<time>` of a working
  clone (`data/module-catalogs/<name>.work`), printing the diff. It refuses, naming file and kind but never the value,
  if the files hold a machine path (the leak guard's rules), a secret-shaped string, a secrets file, a link, a binary or a
  file over 1 MB. The API and the page only prepare the commit. Pushing the branch (never `main`, never forced) is the CLI's
  separate `--push`; merging it is up to the catalog's owner.

## Publishing

`abp modules publish` (and `python -m abp_modkit adopt --publish`) puts a project in its own private GitHub repo,
safely:

- a `.gitignore` for its stacks (new repositories only; an established one keeps its own);
- a scan of everything about to be committed, and of the whole history when a repository is published for the first
  time: secret-shaped strings, key files, `.env`, files over 50 MB. Any finding stops it, naming the file and the
  kind of finding, never the value;
- in a repository with history, only the module files are committed; other staged or unstaged work is left alone;
- build output staged before (a no-commit repo's index) is unstaged, and an empty nested `.git` (from `cargo new`)
  is set aside, not deleted;
- nothing is force-pushed, and the repository's own pre-push hook runs.
