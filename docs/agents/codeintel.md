# Code intelligence (bot/codeintel)

Finding and reading code costs tokens: a `grep` returns hundreds of lines, a whole-file `read_file` returns thousands of
tokens to answer a question about one function. `bot/codeintel` keeps an index of a repository and gives every agent -
ABP's native agent, swarms, local and cloud models, Claude Code through MCP - tools that return only what was asked
for, each answer ending with what it cost (`[~N tokens]`).

## The tools

| Tool | Answers |
|---|---|
| `outline(path)` | signatures and line ranges of a file (no bodies), or the top-level names of each file in a folder |
| `find_symbol(query, kind?)` | exact, qualified (`Class.method`, `pkg.mod.name`), prefix, or fuzzy-by-words (FTS5 over names, split identifiers, docstrings) |
| `read_symbol(target)` | the exact body: `pkg.mod.Class.method`, `Class.method`, `name`, `path::Class.method` or `path:line` (innermost symbol containing the line); `context_lines` adds surrounding lines; an ambiguous name lists the candidates |
| `references(symbol)` / `callers(symbol)` | uses of a name, import-linked first, then by name only; callers grouped by calling symbol; both say when the name has several definitions |
| `related_tests(path or symbol)` | test files (and test functions) by import, by reference, by naming convention, and by per-test coverage when `coverage.json` (with contexts) is present; ends with a `pytest ...` line |
| `impact(diff or paths)` | from `git diff` output or a list of paths: the symbols changed, their callers, the files that import them, the tests to run |
| `context_pack(task, budget_tokens)` | a ranked bundle for a task in words: signatures (22% of the budget), the top bodies (55%), related tests (10%) and rules (13%) |

Every tool takes `max_tokens` (`budget_tokens` for the pack) and says when it cut. `read_file` is unchanged, but a whole-file
read of a big source file now ends with a hint to use `outline` / `read_symbol` / an `offset`+`limit` range. The native
agent's system prompt and tool descriptions tell the model to prefer these tools to whole-file reads.

## The index

One SQLite file per repository, in the agent state folder next to code_search's index (`code_index/<hash>.db`; set
`native_agent.code_index_dir` to move both). The code_search tables (`files`, `chunks`, `vecs`, `vfiles`) are untouched and
shared - the text chunks and embeddings are reused, not duplicated; the code-intelligence tables start with `ci_`:

- `ci_files` path, language, module, size, mtime, content hash, parser used, is-test
- `ci_symbols` kind, name, qualified name, full name (`module.qname`), signature, first doc line, decorators, start/end line, parent, depth; `ci_symfts` (FTS5) over them
- `ci_refs` name, file, line, kind (`call` / `name` / `attr`), enclosing symbol
- `ci_imports` file -> module/name, resolved to a file where it can be (absolute and relative Python imports, relative JS/TS paths)
- `ci_testmap` test file / test function -> source file, with how it was found (`import`, `ref`, `name`, `coverage`)
- `ci_usage` per-tool call and token counters (`python -m bot.codeintel stats`)

**Which files:** in a git work tree `git ls-files --cached --others --exclude-standard` (so `.gitignore` is honoured);
otherwise a walk that reads the root `.gitignore`. Vendored and generated code is skipped (`node_modules`, `vendor`, `dist`,
`build`, `target`, `*.min.js`, `*_pb2.py`, files whose first lines say "generated" / "DO NOT EDIT"); files over 1 MB are skipped.

**Incremental:** a file is re-read only when its size or mtime changed and re-parsed only when its content hash did. Tools
refresh the index first (at most every 2 s) and check the one file they are about to serve, so a read right after an edit
never returns stale lines. Writers take a per-repository lock; every call opens and closes its own connection (WAL mode), so
the library is safe from any number of threads. The agent tools run in `asyncio.to_thread` and never block the event loop.

On ABP's own tree (1,300 source files, 24,000 symbols, 218,000 references; measured on a shared development machine): full
index **12.6 s** with tree-sitter (8.6 s with the `ast`/regex fallbacks), unchanged-tree update **0.4 s**, one edited file a
few milliseconds beyond that. The first `context_pack` also builds code_search's text chunks (up to 40 s once, as `code_search`
does).

## Parsers

Pluggable (`bot/codeintel/parsers.py`): the first backend that handles a file and returns a result wins.

1. **tree-sitter** (`treesitter.py`; packages `tree-sitter` and `tree-sitter-language-pack`, pinned in `requirements.txt`) -
   exact symbols, ranges (decorators and `export` included), signatures, docs, references and imports from the syntax tree for
   Python, JavaScript, TypeScript/TSX, Rust, Go, Java, Kotlin, C#, Swift, Ruby and PHP. The import is guarded: without the
   packages ABP runs as before.
2. **python-ast** - the same data for Python from the standard library.
3. **regex** - repo_map's declaration patterns plus brace/`end` matching for end lines; a heuristic for the other languages.

Known limits: references and callers resolve by name plus import links, not types (`x.run()` cannot tell which class's `run`;
the answer says when a name is ambiguous); the bundled Kotlin grammar mis-parses `object Name { ... }` blocks; dynamic
dispatch, re-exports through `__init__`, star imports and non-Python/JS import resolution are not followed.

## Surfaces

- **Native agent:** the eight tools above, registered like `repo_map` / `code_search` (`bot/codeintel/agent_tools.py`), read-only and concurrency-safe, working in the agent's workspace.
- **MCP** (`bot/mcp_server.py`): `code_outline`, `code_find_symbol`, `code_read_symbol`, `code_references`, `code_callers`, `code_related_tests`, `code_impact`, `code_context_pack`. `root` defaults to the MCP process's working folder (for Claude Code, the project it was started in).
- **HTTP API** (`/api/codeintel/...`, dashboard token): `stats`, `index`, `outline`, `find`, `read`, `refs`, `callers`, `tests`, `impact`, `pack`. `root` must be ABP's own folder or listed in `native_agent.codeintel.roots`.
- **CLI:** `python -m bot.codeintel [--root DIR] index|outline|find|read|refs|callers|tests|impact|pack|stats|bench` (`--json`, `--max-tokens`).
- **Library:** `from bot.codeintel import index_repo, outline, find_symbol, read_symbol, references, callers, related_tests, impact, context_pack, stats` - each returns a `Result(text, tokens, data, truncated)`.

```yaml
native_agent:
  code_index_dir: ""         # where the index files live (default: the agent state folder's code_index/)
  codeintel:
    roots: []                # extra folders the HTTP API / MCP tools may index
    semantic: false          # true: context_pack also uses code_search's ModelMistress embeddings
```

`context_pack` adds a "rules and decisions" section when `bot.context_vault` exists and exposes
`rules_for(task_text, max_tokens) -> str`; without it the section is simply absent.

## Measuring: `python -m bot.codeintel bench`

No model involved. For eight questions about ABP's own code ("where is X cached", "what calls Y", "which tests cover Z", "what
is affected if I change W", "how does the agent skip vendored folders") it compares the tokens of the grep + `read_file`
route (the grep tool's output, capped at 200 lines, plus up to three files at `read_file`'s 20,000-character cap) with the
tokens of the new tools' output, using the same estimate (characters / 3.5). Result on this branch:

```
 baseline   tools   ratio  question
     3446     187   18.4x  Where is the repo_map result cached, and when is it invalidated?
     1063     170    6.3x  How does safe_path decide a path is outside the workspace?
    13339      83  160.7x  What calls refresh_code_index?
     8683      61  142.3x  Which tests cover bot/agent_runtime/repo_map.py?
    10027     436   23.0x  What is defined in bot/agent_runtime/tools.py?
    16318    2604    6.3x  How does the agent decide to skip vendored folders when mapping a repository?
     7636     252   30.3x  What is affected if I change search_index._match_query?
     5887     180   32.7x  Where is the MCP tool get_status defined?
    66399    3973   16.7x  TOTAL
```

The baseline is generous to grep (an agent often reads more than three files, and `read_file` of a long file needs several
calls to reach the function); it also ignores the extra model turns the long route costs. What the bench does not measure is
whether a model finishes the same tasks with fewer tokens - that is the factory bench's job (LF-NAV exit criterion).

## Not built

LSP bridges (pyright, rust-analyzer, tsserver) for exact cross-file definitions; caching results per commit; call-graph
resolution through types. Tested against fakes and ABP's own tree; not yet run by a live model.
