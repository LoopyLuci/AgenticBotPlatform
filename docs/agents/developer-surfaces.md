# Developer surfaces: headless runs, editors, language servers, SDKs, CI

What is here, how to use it, and - because none of it has been used against every real counterpart yet -
what has and has not been checked.

## Headless runs: `python -m abp_run`

```bash
python -m abp_run "fix the failing test in tests/test_x.py" --model anthropic/claude-sonnet-5 --cwd . --json
```

One agent turn, no chat channel; answer on standard output. `--json` prints the reply, token count, the tools
used, the trace run id and an exit code. **Ephemeral by default** (its own database and trace store, deleted
afterwards); `--persist` uses the real ones.

Nobody is present to approve anything, so `--approve deny` is the default: a tool the permission rules would ask
about is refused. `--approve allow` approves those, except in a session that read untrusted web content (still
refused). `--permission-mode plan` makes the run read-only.

Exit status: `0` done, `1` failed, `2` bad usage, `3` stopped at a step / time / token limit, `4` the model's
rate limit or allowance is used up ([models.md](models.md)).

## Editors: `python -m abp_acp`

An [Agent Client Protocol](https://agentclientprotocol.com) server on standard input/output, so an editor that
speaks ACP (Zed and others) can use the agent. Point the editor's custom-agent setting at
`python -m abp_acp --model auto`. Streamed replies, tool activity, and permission requests
(the editor's user is asked, and the answer is obeyed) are supported; cancelling ends the turn.

`--model` takes three forms:
- `auto`: ABP's model router picks the best configured model for each session, from its first prompt. It never
  picks Claude unless you listed Claude in `native_agent.router.candidates`.
- `provider/model`: a model you name.
- `scripted:FILE`: replays a JSON list of steps instead of calling a model. It is meant for testing an editor
  integration without a key: `[{"call": "write_file", "args": {...}}, {"say": "done"}]`.

The model each turn used is reported in the prompt result's `_meta.abp.model`.

**Tested with a real ACP client, but not yet with Zed.** The VS Code extension (below) speaks ACP to the real
program, inside a real VS Code, in its test suite. Not implemented: loading old sessions, terminals, the
client's file methods, images and audio.

## VS Code: `integrations/vscode`

A VS Code extension that uses the agent through `abp_acp`:
- a chat view with streamed replies and each tool listed as it runs;
- permission questions answered in the chat or in a notification;
- commands to ask about the selection and to fix the problems VS Code reports in a file;
- stop, new conversation, and open the dashboard.

**Install:** open ABP Agents → **Editors** and click **Install in VS Code**, or run `abp_cli editors install-vscode`.
Either one runs `code --install-extension` with the package bundled in ABP. The same tab shows when an update is
available, and gives the ACP command to paste into Zed or another editor.

It finds ABP by itself and needs no token. Every ABP server records where its code, state folder and Python are in
`~/.abp/install.json` when it starts, and the extension starts the agent from there. The dashboard injects its
own token for local page loads. It defaults to `--model auto`. See its
[README](../../integrations/vscode/README.md) for settings.

The desktop installer now includes `abp_acp`, `abp_run` and `abp_agenteval`. Before this, an installed ABP could
not run the ACP server at all; the extension recognises such an install and says to update it.

**Verified:**
- the ACP client and conversation logic against the real `abp_acp` with a scripted model (vitest);
- the extension inside a real VS Code (`npm run test:vscode`, using the installed VS Code), where commands make
  real edits after a permission answer, a refusal writes nothing, and "fix problems" carries VS Code's
  diagnostics;
- the chat view's rendering in a browser.

The one-click install was tested in a real browser, and a real `code --install-extension` was run into a throwaway
extensions folder (`tests/test_editors_tab.py`, `tests/test_editor_integrations.py`).

The local pipeline runs these whenever the extension, `abp_acp` or `abp_run` changes. The installer build
(`scripts/stage_bundle.py`) packages the extension when npm is available. **Not done:** publishing to the Visual
Studio Marketplace or Open VSX.

## After an edit: formatters and language servers

`native_agent.code_intel` in `config/backends.yaml` (off until configured; nothing is bundled). After the agent
writes a file it can run your formatter on it and show the agent the errors your language server finds, so a
broken edit is noticed in the same step. An `lsp` tool answers diagnostics, symbols, definition, references and
hover. See the module docstring in `bot/agent_runtime/code_intel.py` for the settings.

Checked against a stand-in server **and by hand against a real rust-analyzer**: that showed modern servers answer
diagnostics on request instead of pushing them, and refuse while loading, both of which are handled. Not yet run
against pyright or typescript-language-server.

## Conversations: export

`/export` in chat (Markdown; `/export json`) and `GET /api/agent/sessions/<key>/export?format=md|json` (needs the
dashboard token itself). Tool output is shortened; secrets the server holds and anything shaped like a key, token,
password or private key are removed. There is no hosted share link: ABP has no public server to host one, so the
file is what you share.

## API, spec and SDKs

`docs/api/openapi.json` is the dashboard API's OpenAPI document, generated by `scripts/export_openapi.py` (a test
fails if it is out of date: run the script). `abp_sdk` is a Python client (`AbpClient`), and `sdk/typescript/` a
generated JavaScript client with type declarations; both call any operation by `"METHOD /path"` or operation id
and have helpers for common calls. Verified: the Python client against the app in-process, the JavaScript client
against a live server in a test. Not published to PyPI or npm.

## Pull request review: `integrations/github-action`

A composite GitHub Action that reviews a PR with an ABP agent and keeps one comment up to date. The agent runs
**read-only with approvals denied and no web tools**; the diff is shown to it as quoted data, and the GitHub token
is used only by the script that posts the comment. It never approves or merges. Tested against a real git
repository, a scripted model and a faked GitHub API; **never run on a GitHub runner**. The Action installs
`requirements.txt`, and a non-Anthropic model needs a provider defined in ABP's `config/providers.yaml`.

## Other agents as engines: `opencode` and `openclaw` backends

A bot instance can hand its turns to OpenCode (`opencode run`) or OpenClaw (`openclaw agent --json`) and return
their answer, keeping ABP's channels, schedules and dashboards. **ABP's own permission rules, taint tracking and
traces do not apply inside them.** The command lines follow each product's docs; neither is installed here, so
they are tested against a stand-in program only. OpenCode's output is taken as plain text; OpenClaw's JSON is
searched for its text field. Put any flag your version needs in the backend's `extra_args`.
