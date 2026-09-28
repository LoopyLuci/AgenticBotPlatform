# AgenticBotPlatform for VS Code

Use the ABP coding agent in VS Code. Ask it to explain, change or fix something in your workspace; it reads
files, edits them and runs commands there, and **asks you first** before anything that changes something.

It runs on your own ABP: the same models, permission rules, sandbox and secret handling you set up in the ABP
app. Nothing is sent anywhere ABP itself would not send it.

## What you can do

- **Chat** in the ABP view in the activity bar. Replies stream in; each tool the agent uses is listed as it runs.
- **Answer permission questions** in the chat or in the notification that appears, whichever is nearer:
  *Allow once*, *Allow for this session*, or *Reject*. Dismissing the notification leaves the question open in
  the chat; stopping the agent refuses it.
- **Ask about the selection** (right-click in the editor): the selected code goes to the agent as context,
  with its file and line numbers.
- **Fix the problems in this file** (right-click, or the command palette): sends the errors and warnings VS Code
  shows for the file, so the agent can fix them and check its change.
- **Ask the agent** from anywhere with <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>A</kbd> (<kbd>Cmd</kbd>+<kbd>Alt</kbd>+<kbd>A</kbd> on a Mac).
- **Stop**, **New conversation**, **Open the dashboard** and **Show the agent log** from the view's title bar
  or the command palette (all under "ABP:").

## Setup

Install the ABP desktop app, then install this extension from ABP itself: **ABP Agents → Editors → Install in VS
Code**. The extension finds ABP by itself; there is nothing to configure and no token to copy. It looks, in
order, at:
1. the `abp.abpPath` setting;
2. the `ABP_CODE_ROOT` environment variable;
3. an open workspace that is itself an ABP checkout;
4. the ABP you last started, which each ABP records in `~/.abp/install.json` with its own Python and state folder;
5. the desktop app's install folder.

The agent works in the first folder of your workspace. The workspace must be trusted, because the agent edits
files and runs commands in it.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `abp.model` | `auto` | The model. `auto` lets ABP pick the best configured model for each conversation, and it never picks Claude unless you listed Claude in ABP's router candidates. Or name one as `provider/model`, with a provider from ABP's Models page. |
| `abp.permissionMode` | *(ABP's own)* | `default` asks before any change. `plan` is read-only. `accept_edits` edits files without asking but still asks before commands. `bypass` asks nothing, and only works if ABP allows bypass mode. |
| `abp.abpPath` | *(found automatically)* | The folder ABP is installed in, or a checkout of it. |
| `abp.pythonPath` | *(ABP's own)* | The Python that runs ABP. |

Changing a setting restarts the agent before your next message. A conversation in progress finishes first.

## How it works

The extension starts `python -m abp_acp` from your ABP and talks to it over the
[Agent Client Protocol](https://agentclientprotocol.com), the same protocol Zed and other editors use. Each
chat is one ACP session. The agent edits files on disk directly, and VS Code shows the changes as they happen.

## Development

```bash
npm ci
npm run typecheck
npm test               # the ACP client and the conversation logic, against the real abp_acp with a scripted model
npm run test:vscode    # the extension inside a real VS Code (the installed one, else a downloaded build)
npm run package        # dist/abp-vscode.vsix
```

The tests need this ABP checkout's `.venv`. They use `python -m abp_acp --model scripted:FILE`, which replays
fixed steps instead of calling a model, so they need no key and cost nothing.
