# WebAssembly plugins

A wasm plugin is a WebAssembly component that ABP loads into its own process and calls. It
starts in microseconds, uses a few MB, and is stopped by a deadline and a memory cap rather
than by a person noticing. Each of its tools appears in the agent's tool list like any other,
with its own JSON schema.

**What is in the box**

| Piece | Where |
|---|---|
| The world every plugin implements | [`wit/abp-plugin.wit`](../wit/abp-plugin.wit) |
| The runtime: compile a component, enforce the limits, mediate every capability | [`native/abp-wasm/`](../native/abp-wasm/) |
| Install / enable / disable / uninstall / invoke, and the audit log | [`bot/wasm_plugins/`](../bot/wasm_plugins/) |
| Each plugin's tools as agent tools | [`bot/wasm_plugins/tools.py`](../bot/wasm_plugins/tools.py) |
| One plugin call as a sandbox cell | [`bot/sandbox_ns/wasm.py`](../bot/sandbox_ns/wasm.py) |
| The dashboard API | [`bot/dashboard/routes/wasm_plugins.py`](../bot/dashboard/routes/wasm_plugins.py) |
| The Rust guest SDK | [`sdk/wasm/rust/`](../sdk/wasm/rust/) |
| Examples: a template, a useful one, and the probes the tests use | [`examples/wasm/`](../examples/wasm/) |

## Building the runtime

```
python native/abp-wasm/build.py --venv <python.exe>     # build and install into that interpreter
python native/abp-wasm/build.py --dest <dir>             # build the artifact and copy it there
python native/abp-wasm/build.py --check                 # cargo fmt, clippy, cargo test
```

**ABP works without it.** `bot/wasm_plugins/runtime.py` says once that the module is missing
and every entry point refuses with the same message; `ABP_NATIVE=0` does the same deliberately,
so both paths can be exercised on one build. `GET /api/wasm-plugins/status` answers the same
question over HTTP.

Requires Rust stable (`rust-version` 1.96), and builds on Windows, Linux and macOS. Windows is
primary: every child process the build starts gets `CREATE_NO_WINDOW`.

## Writing a plugin

A plugin exports two functions: `describe()` once, and `invoke(tool, args_json)` per call.

```rust
use abp_wasm_sdk::{plugin, Manifest, ManifestBuilder, Plugin, Tool, ToolBuilder};

struct Hello;

impl Plugin for Hello {
    fn describe() -> Manifest {
        Manifest::new("hello", "1.0.0", "Greets someone.")
            .with_tool(Tool::new("greet", "Greet someone by name", SCHEMA))
    }

    fn invoke(tool: &str, args_json: &str) -> Result<String, String> {
        match tool {
            "greet" => Ok(format!("{{\"greeting\": \"Hello, {name}!\"}}")),
            other => Err(format!("no tool named {other}")),
        }
    }
}

plugin!(Hello);
```

```
cargo build --target wasm32-wasip2 --release
```

`rustup target add wasm32-wasip2` once per machine (per-user; no admin).

The host services a plugin can call are in the same crate: `log`, `store_get`/`store_set`/
`store_delete`/`store_keys`, `http_fetch`, `now_ms`, `random_bytes`, plus WASI p2 for the clock,
randomness and the preopened directories.

## Installing one

```
POST /api/wasm-plugins/install  {"source": "<path or https URL>"}
```

This compiles the component and reads its manifest **without enabling it**, and returns what it
asked for (`requested`) next to what would be granted (`granted`). Enabling is a second call,
with the approval lists:

```
POST /api/wasm-plugins/hello/enable
```

From Python: `bot.wasm_plugins.manager.install(path, approve_network=[...], enabled=True)`.

Approving something the plugin never asked for is refused rather than ignored: a grant nobody
reviewed is not a grant. After install, `narrow_grants` can only ever take grants away.

## What a plugin can do

Exactly what a person approved, and nothing else:

* **A deadline.** Epoch interruption inside the engine, measured against a ticker whose own
  period is calibrated, so a 250 ms budget stops a spinning guest in about 250 ms. The
  granularity is one tick.
* **A memory cap.** A store limiter that traps on a grow past the cap. Reported as the memory
  error, distinctly from the deadline, because the two need different fixes.
* **An output cap.** A result larger than the cap is refused rather than truncated, so nothing
  half-read is ever handed to the model.
* **Its own key-value store.** One directory per plugin. Another plugin's keys are invisible,
  and ABP's own state is not in it. Keys with a path separator are refused rather than
  sanitised.
* **Only the preopens that were approved**, mapped to a directory ABP owns. With nothing
  preopened the guest has no way in - not a path it guesses, not one it climbs out of with `..`.
* **Only the hosts that were approved**, checked before a request is made. `http-fetch` is the
  only way out; the world has no sockets, and an empty allowlist means no network at all.

A trap, a limit and a tool that reported its own failure are three different things on purpose:
only the first two are the sandbox working, and the third is an answer the model can act on.

Every call is audit-logged (`db.log_audit`): plugin, tool, duration, outcome - including the
ones a deadline stopped.

## The `wasm` cell

```python
from bot.sandbox_ns import wasm

with wasm.new_cell(plugin="hello", tool="greet", timeout_ms=1000) as cell:
    result = cell.invoke("hello", "greet", {"name": "world"})
```

A wasm cell is not a `Cell`: there is no process to put in a job object or a cgroup, and a
status page claiming a containment that is not there is exactly the lie this package refuses to
tell. What the cell does is account - `status()` reports `os: "wasm"`, `pids: []`, the call
count, and the limits with `enforced_by: wasmtime` - and take the smaller of its own deadline
and the plugin's, so a caller can shorten a call and never lengthen one.

## Examples

* [`examples/wasm/rust-hello`](../examples/wasm/rust-hello/) - the template: one tool, no
  capabilities, so it installs without a conversation.
* [`examples/wasm/rust-regex`](../examples/wasm/rust-regex/) - a plugin worth having: pulls
  emails, URLs, IPv4 addresses, `#tags` and quoted strings out of text, with a matching
  `count`. No regex crate; the patterns are a few lines each and a regex engine would be more
  than the rest of the component.
* [`examples/wasm/rust-probes`](../examples/wasm/rust-probes/) - the tools the sandbox tests
  run: `spin` (loops forever), `grow` (allocates without end), `flood` (returns more than the
  output cap), `kv`, `read` (from a preopen) and `fetch`. Asks for `/data` and `127.0.0.1`, so
  the tests can install it granted and ungranted.
* [`examples/wasm/py-hello`](../examples/wasm/py-hello/) - the same plugin as `rust-hello`, in
  Python, for `componentize-py`.
* [`examples/wasm/js-hello`](../examples/wasm/js-hello/) - the same plugin in JavaScript, for
  `jco componentize`.

### JavaScript

`examples/wasm/js-hello/abp_plugin.js` exports the world's `guest` interface as an object and
imports `log` from `abp:plugin/host@0.1.0`. [jco](https://github.com/bytecodealliance/jco)
(1.37, with `@bytecodealliance/componentize-js`; `npm install` per-user, no admin) compiles it:

```
npx jco componentize abp_plugin.js --wit ../../../wit --world-name plugin --disable http fetch-event -o abp_js_hello.wasm
```

`--disable http fetch-event` is required: by default componentize-js imports `wasi:http` for
`fetch`, which ABP does not provide (a plugin reaches the network only through the host's
checked `http-fetch`), and the component would not load. In JavaScript a WIT record is a plain
object with camelCase fields and `result<string, string>` is a returned string for `ok` and a
thrown value for `err`. About 13 MB; the first load compiles it (about 11 s here), after that it
comes from the compiled-component cache. A call takes about 4 ms.

### Python

`examples/wasm/py-hello/abp_plugin.py` implements the world's exports and
[`componentize-py`](https://pypi.org/project/componentize-py/) (0.25.1, installed per-user with
`pip install --target <dir>`; no admin) compiles it:

```
componentize-py -d ../../../wit -w plugin componentize abp_plugin -o abp_py_hello.wasm
```

The types matter: `describe` returns the generated `Manifest` record (a plain dict cannot be
lowered, and the component traps with `unreachable` - which is what the first version of this
example did), the class subclasses the generated `exports.Guest` and is called on an instance, and
`result<string, string>` is a returned `str` for `ok` and a raised `Err(str)` for `err`. About
18 MB; first load about 10 s, a call about 4 ms.

A tool that reports its own failure (the world's `err`) comes back from the runtime as
`{"error": ...}`, never as `{"ok": ...}`, so a refusal is never mistaken for a result.

## Tests

```
python -m pytest tests/test_wasm_plugins.py tests/test_wasm_disabled.py -q -p no:cacheprovider
```

Nothing is mocked: the components under test are the examples above, built for
`wasm32-wasip2`; the HTTP server the network test reaches is a real one on 127.0.0.1 that the
test starts and stops; the files the filesystem test reads are real; the audit rows are real
rows. `tests/test_wasm_plugins.py` builds the example components on first use (the Python and
JavaScript ones only when `componentize-py` or `jco` is installed, and skips them otherwise) (found already
built under `$CARGO_TARGET_DIR`, otherwise `cargo build --target wasm32-wasip2` through
`bot.sandbox_ns.spawn`, so it is windowless and killable). Without cargo it skips with the
reason rather than passing vacuously.

`tests/test_wasm_disabled.py` runs the same entry points with `ABP_NATIVE=0` and asserts the
feature says so and gets out of the way.

## Limits and defaults

| Setting | Default | Notes |
|---|---|---|
| `timeout_ms` per call | 2000 | Ceiling 30000; a plugin's own may only be lowered |
| memory per plugin | 16 MB | Ceiling 512 MB |
| output cap | 256 KB | Refused, not truncated |
| log lines kept per call | 512 | Beyond that the guest's log is dropped |
| pool | 64 component instances | Falls back to on-demand allocation if unavailable |
| compiled-component cache | under the platform data dir | Keyed on the component and on the engine's compatibility hash |
