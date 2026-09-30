# Making a module: abp_modkit

Any project can be an ABP module. `abp_modkit` (it ships inside ABP, standard library only) reads a project, writes the
two files that describe it, and serves its operations. Nothing has to be written by hand, and nothing in the project
has to change.

```bash
abp modules adopt Z:/Projects/VMStream        # through a running ABP (also: Modules page -> Add a project)
python -m abp_modkit adopt . --register       # without one; --register adds it to this ABP's config
python -m abp_modkit adopt . --dry-run        # show both files, write nothing
python -m abp_modkit new ./my-tool --lang python   # a new, empty project that is already a module
python -m abp_modkit check .                  # load both files, start a real hub, list operations, bridge MCP, stop
abp modules candidates Z:/Projects            # which folders there are projects, and which are modules already
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
