# The toolkit

`abp_toolkit` is ABP's kit for programming, computer science and asset work: 66 actions in 9 groups, usable by any
model or agent. The same actions are available three ways:

- **ABP's agent** gets them as tools, one per group;
- **any MCP client** (Claude Desktop, Cursor, VS Code, another agent) runs `python -m abp_toolkit mcp`;
- **people and scripts** use the command line: `python -m abp_toolkit list | describe | call`.

File paths are always relative to a working folder and cannot leave it (for the agent, its workspace).

## The groups

| Group | What it does |
|---|---|
| **lint** | Lints any language, reporting every finding in one format. It runs the installed linters (ruff, mypy, eslint, tsc, shellcheck, PSScriptAnalyzer, go vet, clippy, yamllint, hadolint, rubocop, php -l, luacheck, cppcheck, stylelint, markdownlint, sqlfluff), plus checks of its own that always run.<br>• Syntax: Python, JSON/JSONC, YAML, TOML, XML/SVG, INI, HTML, CSS, PowerShell (its own parser), JavaScript (node) and shell (bash).<br>• Structure: batch files (missing labels, unbalanced blocks, `set x =`), VBScript (If/Sub/For/Do/Select/With… pairs, Option Explicit), SQL (unbalanced parentheses, UPDATE/DELETE without WHERE), CSV column counts, Dockerfile rules, broken Markdown links, and bracket balance for C-like languages.<br>• Hygiene, for every text file: merge-conflict markers, mixed line endings, mixed indentation, secrets, very long lines.<br>`lint.fix` applies the linters' safe fixes. |
| **format** | Uses the project's own formatter when it is installed: ruff/black, prettier, gofmt, rustfmt, clang-format, shfmt, Invoke-Formatter, sqlfluff. Otherwise it formats JSON and XML itself. It can also normalize whitespace and line endings; CRLF files stay CRLF. |
| **run** | Runs snippets or files with time limits in Python, JavaScript, TypeScript, PowerShell, CMD, VBScript, JScript, Bash, Go, Rust, C, C++, C#, Java, Kotlin, Ruby, PHP, Lua, Perl, R, Dart and SQL (SQLite). It can also run a single program without a shell. `run.languages` says what works on this machine. |
| **analyze** | • Overview: lines of code per language.<br>• Complexity: cyclomatic complexity per function. Python is measured exactly; other languages by counting decision points.<br>• Imports: the graph, external packages, modules nothing imports, and import cycles.<br>• Other checks: duplicated blocks, unused Python functions and classes, risky patterns (eval, shell=True, SQL built from strings, TLS checks off, download-and-run, iex, `curl \| sh`…), TODO notes, and the outline of a file. |
| **python** | Interpreter and packages, venv creation, pip (install/uninstall/list/outdated/show/freeze/check/download), pytest with parsed failures, mypy/pyright, cProfile, timeit, AST dumps, safe arithmetic evaluation, pip-audit, and building wheels. It always acts on the working folder's own venv when there is one. |
| **cs** | Regular expressions (matches, groups, replace), encodings (base64/32, hex, URL, HTML, punycode, quoted-printable, binary, Morse…), hashes and HMACs, number and bit views, text diffs, JSON Schema validation and inference, JSON path queries, time zones and durations, SQL over CSV or JSON data, graph algorithms (shortest path, topological order, cycles, components, BFS), cron schedules, IDs (UUID, ULID, nanoid, tokens, passwords), text statistics (including invisible characters), and measuring a function's big-O. |
| **generate** | 15 project templates that build and pass their own tests as generated: python-package, python-cli, fastapi-service, node-cli, typescript-library, react-vite, electron-app, rust-cli, go-cli, powershell-module, vscode-extension, chrome-extension, pyqt-app, static-site, batch-toolkit. Also project files: .gitignore by language, licenses, .editorconfig, README, Dockerfiles, GitHub CI and Dependabot. |
| **asset** | • Icons as PNG, multi-size ICO or SVG, and complete favicon sets with a web manifest and the HTML to paste.<br>• Images: placeholders, OG cards, gradients. Conversion, resizing, cropping and format changes, and inspecting an image (EXIF, dominant colours).<br>• Colour palettes (harmonies and a 50–950 scale) and WCAG contrast checks.<br>• SVG charts: bar, stacked, horizontal, line, area, pie, donut, scatter.<br>• Diagrams through Graphviz, or a built-in SVG layout, with Mermaid and DOT text as well.<br>• QR codes, README badges, sprite sheets with CSS, synthesized sounds (WAV) and text banners. |
| **scripts** | A library of 35 ready-made scripts. `scripts.list`, `show`, `run` and `install` (copy into the project) work on it.<br>• PowerShell: system info, large files, port owner, kill a port's owner, disk usage, temp cleanup, zip backups with rotation, file hashes, event-log errors, keep awake, Wake-on-LAN, network test, user PATH, installed apps, scheduled tasks, downloads with retries, git branch cleanup.<br>• CMD: sysinfo, robocopy mirror, listening ports, venv bootstrap.<br>• VBScript: notify, speak, shortcut, run hidden, unzip.<br>• Python: duplicates, bulk rename, CSV/JSON/YAML conversion, serve a folder, tree, watch and run.<br>• Bash: sysinfo, rotating backups, port owner. |

Every script starts with a header giving its name, description, parameters and safety (read, changes, executes or
network). Scripts that change something only report what they would do unless told to apply it (`-Apply`, `-Force`,
`--apply`, `--delete`).

## In ABP

Each group is offered as `toolkit_<group>` for the actions that only look. They run without asking. Groups that also
change files or run programs get a second tool, `toolkit_<group>_act`, which asks for approval like any other change.
`toolkit_describe` gives any action's full argument schema. An agent calls, for example:

```json
{"action": "check", "args": {"path": "src", "min_severity": "warning"}}          // toolkit_lint
{"action": "icon", "args": {"path": "assets/app.ico", "text": "A", "shape": "squircle"}}   // toolkit_asset_act
```

## Anywhere else

```bash
python -m abp_toolkit list
python -m abp_toolkit call lint.check path=src min_severity=warning
python -m abp_toolkit call asset.chart path=sales.svg kind=bar --json '{"labels": ["Q1","Q2"], "series": {"2026": [3, 5]}}'
python -m abp_toolkit mcp --workspace Z:/Projects/MyApp            # add --read-only for an MCP client that must not change anything
```
