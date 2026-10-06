# Rules

A **rule** is a decision someone wrote down, in a language with no filesystem, no network, no clock
and no process access:

```
ctx.weekday < 5 && ctx.hour >= 9 && ctx.message.contains("digest")
```

That is a complete rule: *only run this on a weekday morning, when the message is about a digest*.
It is not Python, and it is not a shell command, because both of those would mean giving a rule the
agent's own rights to answer a question that ought to have one line in it.

Rules answer three kinds of question, and which kind a rule is decides what it must answer:

| kind | answers | used for |
|---|---|---|
| `condition` | `true` or `false` | whether a routine runs now |
| `filter` | `true` or `false` | whether a hook runs |
| `route` | a value, usually a name | which backend or model answers |

Rules live in one table beside routines and hooks, are edited from `/api/rules`, and are consulted in
exactly three places. **A rule that is not set changes nothing** — see
[No rule, no change](#no-rule-no-change).

## Building it

Rules run in `abp_rules`, a small Rust extension that embeds [Rhai](https://rhai.rs/) (a
self-contained scripting language for Rust). It is an ordinary optional native module:

```bash
python native/abp-rules/build.py --venv /path/to/venv/bin/python.exe   # build and install it
python native/abp-rules/build.py --dest dist/                          # or just build the wheel
python native/abp-rules/build.py --check                               # fmt, clippy and the tests
```

`GET /api/rules/status` says whether it is installed and what it enforces. **Without it, rules are
off and the rest of ABP is untouched**: every write answers `503` with the reason, every read still
works, and no integration point changes behaviour. `ABP_NATIVE=0` turns it (and every other native
module) off, which is how both paths are tested.

The engine is optional in the Cargo sense too: PyO3 sits behind the crate's `python` feature, so
`cargo test` runs the sandbox's own tests with no interpreter linked in at all, and
`native/abp-rules/build.py --check` lints the Python bridge separately.

## What a rule can see

A rule never sees ABP. It sees a flat map of names to plain values, and nothing else:

| name | what it is |
|---|---|
| `message` | the text of this message or prompt |
| `sender` | who sent it — a name, a handle, or an id as text |
| `sender_id` | their id, when there is one |
| `platform` | `telegram`, `discord`, `sms`, `dashboard`, `scheduled`, … |
| `action_type` | `quick_question`, `scheduled`, `tool_result`, … |
| `instance_id` | the bot instance this turn is for |
| `bot` | that instance's name |
| `tags` | a list of strings attached to this turn |
| `hour` `minute` `weekday` `weekend` `date` `time` `timestamp` | the clock, in ABP's local time (`weekday` is 0 for Monday) |

The whole map is bound as `ctx`, and every top-level key is also bound under its own name, so
`ctx.message` and `message` are the same string. **Write `ctx.`** — it is the form that cannot collide
with a function name.

A hook's filter gets the event too: `event` (`PreToolUse`, `PostToolUse`, `SessionStart`, …), `matcher`,
`tool`, and whatever the event carries (`tool_input`, `output`, `error`, `reply`, `kind`, `reason`).
A routine's condition gets `routine` and `schedule_id`.

The clock values are supplied by ABP, not read by the rule: the sandbox has no clock, so "only after
17:00" is answered from something ABP decided rather than from the machine the rule landed on.

Values are trimmed rather than refused where that is sensible (a 40 000-character paste becomes the
first 32 000), and anything that is not plain data — an object, a file handle, a socket — is refused
by name. There is no bridge out of the sandbox: a rule cannot walk from a string to the process that
owns it.

## What a rule cannot do

There is no filesystem, no network, no process spawning, no clock, no module import and no dynamic
evaluation, because nothing in the crate registers a function that could do any of them and Rhai
itself has none. `print` is removed as well, so a rule cannot write to ABP's stdout.

```
ctx.read_file("/etc/passwd")     -> "Function not found: read_file"
http_get("http://127.0.0.1/")    -> "Function not found: http_get"
import "std" as std              -> no such module
loop { }                         -> stopped by the operation limit, not by your patience
```

Every limit is enforced at the operation boundary:

| limit | default | what it stops |
|---|---|---|
| `max_operations` | 200 000 | an infinite loop, and a runaway one |
| `max_call_levels` | 16 | unbounded recursion |
| `max_string_size` | 65 536 | building a huge string, or being handed one |
| `max_array_size` | 4 096 | a huge array, in a rule or in the context |
| `max_map_size` | 1 024 | the same for maps |
| `max_expr_depth` | 64 | a source too deeply nested to be readable |
| `max_variables` | 256 | a rule that declares more than anyone could follow |
| `max_source_chars` | 8 192 | a pasted file saved as a "rule" |
| `max_context_depth` | 8 | a context that nests into something unreadable |

These are enforced by the engine and published at `/api/rules/status`, so they are the same numbers
in the docs, in the API and in the code.

## When a rule is wrong

A rule that does not compile is not stored: it is refused with the line and the column, which is what
lets an editor put the caret in the right place.

```json
{"detail": {"message": "Unexpected ';'",
            "phase": "compile", "line": 2, "column": 9, "limit": "syntax"}}
```

`limit` is machine-readable and stable: `operations`, `call_depth`, `size`, `variables`,
`unknown_function`, `unknown_module`, `unknown_property`, `wrong_type`, `syntax`, `terminated`. In
Python the same failure is a `bot.rules.RuleError` with `.line`, `.column`, `.limit` and `.phase`.

A rule that *compiles* and then fails at run time — a limit, a name the context does not have — is
**logged and ignored**. That is the same fail-open stance `bot/agent_runtime/hooks.py` already takes
for a broken hook command, and for the same reason: a typo in a rule must never be the reason a turn,
a routine or a hook silently stops happening.

Rules are compiled once and cached, twice over: this process keeps the compiled program, and the
engine keeps the parsed AST keyed by source. A rule consulted on every message costs one parse, not
one per message.

## Binding a rule to something

A rule's **target** is the one thing that makes it reach anywhere:

| target | meaning |
|---|---|
| `""` (empty) | a rule nobody has bound to anything: testable, inert |
| `routine:<name>` | this routine only runs when the rule answers yes |
| `hook:<event>` | every hook on this event runs only when the rule answers yes |
| `hook:<event>:<matcher>` | the same, narrowed to one tool |
| `backend` | a route: the first rule that answers names the backend |
| `model` | a route: the first rule that answers names the model |

`instance_id` scopes a rule to one bot instance the same way `agent_hooks` does: unset means every
instance is affected by it.

**Two rules on the same target must both agree.** A routine with two conditions runs only when both
say yes; a hook with two filters runs only when both say yes. One rule saying no is a decision, not a
silent veto — which is why it is logged with the reason and visible in the rule's own history.

### Routine conditions

```
rule      ctx.weekday < 5 && ctx.hour >= 9
kind      condition
target    routine:digest
```

The schedule still decides *when*, `enabled` still decides *whether*, and the rule adds the thing a
schedule cannot express. `bot/scheduler.py` asks the rule before it dispatches a due routine, so the
condition is part of deciding *whether* to run: when the rule says no, the row is re-armed exactly
like a successful run and nothing is recorded, because nothing ran.

```rhai
ctx.routine == "digest" && ctx.weekday < 5
```

### Hook filters

```
rule      ctx.tool != "Bash" || ctx.tool_input.command.contains("--dry-run")
kind      filter
target    hook:PreToolUse:Bash
```

A hook runs only when every rule bound to it says yes. That is how "only run this hook in
production", or "skip the Bash hook while a deploy is in flight", is expressed without a shell script
that has to parse the event itself. The filter runs *before* the hook, so a hook's `deny` cannot be
reached — which is what makes a filter a real gate on a permission hook.

### Routing

```
rule      if ctx.message.contains("image") { "cli" } else { "api" }
kind      route
target    backend
```

A route rule goes to the **front** of the chain `Router.resolve_chain()` produced, and the chain stays
behind it, so a backend a rule sent a turn to that turns out to be down still falls back exactly as it
would have. `target: model` works the same way and moves the model for that one turn, which is how a
rule can send a long task to a bigger model without changing the bot's default.

A route rule answers `""` (or nothing useful) for messages it does not care about, which is why the
example is an `if`. A rule naming something that is not a backend is logged and ignored.

Routing precedence is unchanged in shape, with the rule inserted where a rule belongs — after the
explicit `--backend=` flag, which a rule must not be able to overrule or the flag would be a lie:

```
1. an explicit --backend= flag
2. the instance's own action_overrides
3. the instance's own default backend
4. config.action_overrides, then config.default_backend
5. a routing rule naming a backend   <- new
6. the backup chain on failure
```

## The API

Everything is here; there is no page. A later UI is a pure client of these.

| | |
|---|---|
| `GET /api/rules/status` | is an engine installed, and what it enforces |
| `GET /api/rules` | every rule, each saying whether it can run here and why not |
| `GET /api/rules/{id}` | one rule, plus what has happened to it |
| `POST /api/rules` | `{name, source, kind, target?, description?, instance_id?, enabled?}` |
| `PUT /api/rules/{id}` | any of the same fields; only those change |
| `DELETE /api/rules/{id}` | delete it — whatever it gated goes back to ungated |
| `POST /api/rules/{id}/enabled` | `{enabled}` — turn it off without deleting it |
| `POST /api/rules/validate` | `{source, kind}` — compile without saving |
| `POST /api/rules/test` | `{source, kind, context}` — run an unsaved rule |
| `POST /api/rules/{id}/test` | `{context}` — run a saved rule |

Reading follows the dashboard's normal auth (the desktop token or a paired device's key), so a phone
can see which rules exist. Everything else needs the dashboard token itself: a rule is operator
configuration that changes what runs unattended, which is the same trust boundary hooks and routines
already sit behind.

`validate` and `test` are separate from `create` on purpose, so an editor can check as you type and a
"try it" button can run something that was never saved. `POST /api/rules/{id}/test` fills in whatever
the request left out with a real context — `message`, `sender`, `platform`, `hour`, `weekday`, `tags`,
… — so what a page tries is the same evaluation the scheduler will do.

In Python, without the HTTP layer:

```python
from bot import rules

rules.validate('ctx.weekday < 5', "condition")
rules.matches('ctx.weekday < 5', rules.message_context("standup", platform="slack"))
rules.test('if ctx.hour > 17 { "big-model" } else { "" }', "route", {"hour": 20})
rules.store.create("weekday-only", "ctx.weekday < 5", "condition", target="routine:digest")
```

## No rule, no change

Every integration point asks whether any rule is bound to it before it does anything, and answers
"carry on" if none is. An install that has never written a rule pays one indexed lookup per turn and
nothing else. That is what makes the following true, and each of them has a test:

- a routine with no rule runs at exactly the interval its schedule says, through the same dispatch
- a hook with no filter runs exactly as it did, including its `deny`
- a router with no route rule resolves to exactly the chain it resolved to before

## Where this lives

| | |
|---|---|
| `native/abp-rules/` | the sandbox: the engine, the limits, the AST cache, the PyO3 bridge |
| `bot/rules/engine.py` | the bridge, and what happens when the module is missing |
| `bot/rules/store.py` | the rules themselves, in one small table |
| `bot/rules/context.py` | the only things a rule can see |
| `bot/rules/integration.py` | the three places a rule is consulted |
| `bot/dashboard/routes/rules.py` | the API above |
| `bot/scheduler.py`, `bot/agent_runtime/hooks.py`, `bot/router.py` | the three call sites |
| `tests/test_rules_language.py` | the language, the limits, the errors, the no-I/O claims |
| `tests/test_rules_api.py` | CRUD, validate, test, over the real app |
| `tests/test_rules_integration.py` | each integration point, with a rule and without one |
| `tests/test_rules_without_engine.py` | the same three points with no engine installed |