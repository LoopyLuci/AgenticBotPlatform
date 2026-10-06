//! `abp_rules` - the sandboxed rule language AgenticBotPlatform runs routines, hook filters and
//! routing decisions on.
//!
//! The whole public surface is small on purpose:
//!
//! ```text
//! abp_rules.limits()            -> dict          what the sandbox enforces, for a UI to show
//! abp_rules.version()           -> str
//! abp_rules.cache_stats()       -> (hits, misses, entries)
//! abp_rules.compile(source, kind="condition") -> Program
//! Program.evaluate(context)     -> value         any Rhai value, for a route
//! Program.matches(context)      -> bool          the answer for a condition or a filter
//! Program.kind / Program.limits
//! ```
//!
//! `kind` is `condition`, `filter` or `route`. A condition or a filter must answer true or false; a
//! route answers a value (usually the name of a backend, a bot instance or a model). A rule that
//! cannot be compiled raises `CompileError`; a rule that compiles and then fails raises
//! `EvalError`. Both carry `line`, `column` and `limit` attributes, so a page can point at the exact
//! character in the source that is wrong.
//!
//! **What a rule cannot do**: there is no filesystem, no network, no clock, no process spawning, no
//! module import and no dynamic evaluation here, because nothing in this crate registers a function
//! that could do any of them and Rhai itself has none. The engine also caps operations, call depth,
//! string, array and map sizes and expression depth (see `sandbox::Limits`), so a rule cannot loop
//! forever, recurse forever, or allocate without bound.
//!
//! The sandbox lives in `sandbox.rs` and knows nothing about Python; this file is the bridge. The
//! bridge is behind the `python` feature so `cargo test` exercises the sandbox with no interpreter
//! linked in at all - `build.py` builds the extension with `--features python`.

pub mod sandbox;

#[cfg(feature = "python")]
mod bindings;

#[cfg(feature = "python")]
use pyo3::prelude::*;

#[cfg(feature = "python")]
#[pymodule]
#[pyo3(name = "abp_rules")]
fn abp_rules(module: &Bound<'_, PyModule>) -> PyResult<()> {
    bindings::register(module)
}
