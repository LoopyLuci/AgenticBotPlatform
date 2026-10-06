//! The sandbox itself: one Rhai engine built with hard limits, a compiled rule, and the two calls
//! (`compile`, `evaluate`) that everything else in this crate is a wrapper around.
//!
//! Nothing here knows about Python. That is deliberate - the sandbox is the part that has to be
//! right about what a rule may do, so it is the part with its own tests, run by `cargo test` with
//! no Python interpreter in the picture at all (see `build.py`).

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use rhai::{Array, Dynamic, Engine, EvalAltResult, Map, ParseError, Scope, AST};

/// What a rule is for. All three are the same language; they differ only in what the result must be,
/// because "should this run?" and "which model answers?" are different questions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// Answers yes/no: a routine's condition.
    Condition,
    /// Filters a set: same shape as a condition, named for the hook filters it drives.
    Filter,
    /// Answers "which one": a backend, a bot instance, a model.
    Route,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Condition => "condition",
            Kind::Filter => "filter",
            Kind::Route => "route",
        }
    }

    pub fn parse(text: &str) -> Option<Kind> {
        match text {
            "condition" => Some(Kind::Condition),
            "filter" => Some(Kind::Filter),
            "route" => Some(Kind::Route),
            _ => None,
        }
    }

    fn wants_bool(self) -> bool {
        matches!(self, Kind::Condition | Kind::Filter)
    }
}

/// Every hard limit the engine enforces. These are the numbers `docs/rules.md` publishes and the
/// dashboard shows, so changing one is a visible change and not a silent one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Operations one evaluation may take. This is what stops `loop { }`.
    pub max_operations: u64,
    /// Nested function calls. This is what stops unbounded recursion.
    pub max_call_levels: usize,
    /// Characters in any one string.
    pub max_string_size: usize,
    /// Items in any one array.
    pub max_array_size: usize,
    /// Items in any one map.
    pub max_map_size: usize,
    /// Nesting depth of expressions and statements in a rule's source.
    pub max_expr_depth: usize,
    /// Variables one evaluation may declare.
    pub max_variables: usize,
    /// Characters of rule source.
    pub max_source_chars: usize,
    /// Nesting depth of the context handed to a rule.
    pub max_context_depth: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_operations: 200_000,
            max_call_levels: 16,
            max_string_size: 65_536,
            max_array_size: 4_096,
            max_map_size: 1_024,
            max_expr_depth: 64,
            max_variables: 256,
            max_source_chars: 8_192,
            max_context_depth: 8,
        }
    }
}

/// A rule that did not compile or did not run. `line`/`column` are 0 when Rhai could not say where,
/// and `limit` is the machine-readable reason (which limit tripped, or which name was not found) so
/// a caller can react to it without matching on English.
#[derive(Clone, Debug)]
pub struct Fault {
    pub compile: bool,
    pub message: String,
    pub line: usize,
    pub column: usize,
    pub limit: Option<String>,
}

impl Fault {
    fn from_alt(compile: bool, err: &EvalAltResult) -> Fault {
        let pos = err.position();
        Fault {
            compile,
            message: flatten(&err.to_string()),
            line: pos.line().unwrap_or(0),
            column: pos.position().unwrap_or(0),
            limit: reason_of(err).map(str::to_string),
        }
    }

    /// A parse failure, which Rhai reports as its own error type rather than an `EvalAltResult`.
    fn from_parse(err: &ParseError) -> Fault {
        let pos = err.position();
        Fault {
            compile: true,
            message: flatten(&err.to_string()),
            line: pos.line().unwrap_or(0),
            column: pos.position().unwrap_or(0),
            limit: Some("syntax".to_string()),
        }
    }

    fn plain(compile: bool, message: impl Into<String>, limit: Option<&str>) -> Fault {
        Fault {
            compile,
            message: message.into(),
            line: 0,
            column: 0,
            limit: limit.map(str::to_string),
        }
    }
}

/// Rhai's own error text is indented over several lines and ends with its position; one line keeps a
/// traceback readable and a log line greppable, and the position travels separately as `line` and
/// `column`, so it is stripped off the message rather than printed twice.
fn flatten(text: &str) -> String {
    let flat = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    match flat.rfind("(line ") {
        Some(at) if flat[at..].ends_with(')') => flat[..at].trim_end().to_string(),
        _ => flat,
    }
}

fn reason_of(err: &EvalAltResult) -> Option<&'static str> {
    match err {
        EvalAltResult::ErrorTooManyOperations(_) => Some("operations"),
        EvalAltResult::ErrorTooManyVariables(_) => Some("variables"),
        EvalAltResult::ErrorTooManyModules(_) => Some("modules"),
        EvalAltResult::ErrorStackOverflow(_) => Some("call_depth"),
        EvalAltResult::ErrorDataTooLarge(..) => Some("size"),
        EvalAltResult::ErrorTerminated(..) => Some("terminated"),
        EvalAltResult::ErrorFunctionNotFound(..) => Some("unknown_function"),
        EvalAltResult::ErrorModuleNotFound(..) => Some("unknown_module"),
        EvalAltResult::ErrorPropertyNotFound(..) => Some("unknown_property"),
        EvalAltResult::ErrorVariableNotFound(..) => Some("unknown_variable"),
        EvalAltResult::ErrorMismatchDataType(..) | EvalAltResult::ErrorMismatchOutputType(..) => {
            Some("wrong_type")
        }
        EvalAltResult::ErrorParsing(..) => Some("syntax"),
        _ => None,
    }
}

/// One compiled rule, ready to run against any number of contexts.
pub struct Program {
    ast: Arc<AST>,
    kind: Kind,
    limits: Limits,
}

impl std::fmt::Debug for Program {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Program")
            .field("kind", &self.kind.as_str())
            .finish_non_exhaustive()
    }
}

/// Cache of parsed rule source. A rule is compiled once and evaluated on every message it touches,
/// so this is the difference between parsing on the hot path and not.
static ASTS: Mutex<Vec<(String, Arc<AST>)>> = Mutex::new(Vec::new());
/// Source is the cache key and its length is already capped, so a small cache cannot grow without
/// bound and cannot collide: a collision would hand one rule another rule's AST.
const AST_CACHE_MAX: usize = 256;
static AST_HITS: AtomicU64 = AtomicU64::new(0);
static AST_MISSES: AtomicU64 = AtomicU64::new(0);

/// The engine, rebuilt only when the limits change. `Engine::new()` is not free and rules are
/// evaluated on every turn.
static ENGINE: Mutex<Option<(Limits, Arc<Engine>)>> = Mutex::new(None);

fn engine_for(limits: Limits) -> Arc<Engine> {
    let mut slot = ENGINE.lock().expect("the rule engine cache is poisoned");
    if let Some((cached, engine)) = slot.as_ref() {
        if *cached == limits {
            return Arc::clone(engine);
        }
    }
    let mut engine = Engine::new();
    engine.set_max_operations(limits.max_operations);
    engine.set_max_call_levels(limits.max_call_levels);
    engine.set_max_string_size(limits.max_string_size);
    engine.set_max_array_size(limits.max_array_size);
    engine.set_max_map_size(limits.max_map_size);
    engine.set_max_expr_depths(limits.max_expr_depth, limits.max_expr_depth);
    engine.set_max_variables(limits.max_variables);
    // `print` is the one builtin that writes anywhere, and stdout is shared with every other part of
    // ABP. A rule has no business writing to it; a rule that means to say something returns a value.
    engine.disable_symbol("print");
    engine.disable_symbol("debug");
    let engine = Arc::new(engine);
    *slot = Some((limits, Arc::clone(&engine)));
    engine
}

/// Cache counters: parse hits, parse misses, and how many ASTs are held.
pub fn cache_stats() -> (u64, u64, usize) {
    let entries = ASTS.lock().map(|cache| cache.len()).unwrap_or(0);
    (
        AST_HITS.load(Ordering::Relaxed),
        AST_MISSES.load(Ordering::Relaxed),
        entries,
    )
}

/// Compile `source`, reusing a cached parse when this exact source was compiled before.
pub fn compile(source: &str, kind: Kind, limits: Limits) -> Result<Program, Fault> {
    compile_checked(source, kind, limits).map(|(program, _)| program)
}

/// The same, but also saying whether the parse came from the cache. `compile` is what callers want;
/// this is what lets the tests prove a rule is parsed once rather than once per message, without
/// having to trust a global counter that other threads are moving at the same time.
pub fn compile_checked(source: &str, kind: Kind, limits: Limits) -> Result<(Program, bool), Fault> {
    if source.trim().is_empty() {
        return Err(Fault::plain(true, "the rule is empty", Some("syntax")));
    }
    let length = source.chars().count();
    if length > limits.max_source_chars {
        return Err(Fault::plain(
            true,
            format!(
                "the rule is {length} characters long, over the {} character limit",
                limits.max_source_chars
            ),
            Some("size"),
        ));
    }
    let engine = engine_for(limits);
    let (ast, from_cache) = match cached_ast(source) {
        Some(ast) => (ast, true),
        None => {
            AST_MISSES.fetch_add(1, Ordering::Relaxed);
            match engine.compile(source) {
                Ok(ast) => {
                    let ast = Arc::new(ast);
                    store_ast(source, Arc::clone(&ast));
                    (ast, false)
                }
                Err(err) => return Err(Fault::from_parse(&err)),
            }
        }
    };
    Ok((Program { ast, kind, limits }, from_cache))
}

fn cached_ast(source: &str) -> Option<Arc<AST>> {
    let cache = ASTS.lock().ok()?;
    let found = cache
        .iter()
        .find(|(held, _)| held == source)
        .map(|(_, ast)| Arc::clone(ast));
    if found.is_some() {
        AST_HITS.fetch_add(1, Ordering::Relaxed);
    }
    found
}

fn store_ast(source: &str, ast: Arc<AST>) {
    if let Ok(mut cache) = ASTS.lock() {
        if cache.len() >= AST_CACHE_MAX {
            cache.remove(0);
        }
        cache.push((source.to_string(), ast));
    }
}

impl Program {
    pub fn kind(&self) -> Kind {
        self.kind
    }

    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// The variables a rule can read: the whole context as `ctx`, and every top-level key of it under
    /// its own name, so both `ctx.message` and `message` are the same string.
    fn scope(&self, mut context: Dynamic, limits: Limits) -> Result<Scope<'static>, Fault> {
        let map = take_map(&mut context).ok_or_else(|| {
            Fault::plain(
                false,
                "the rule context must be a map of names to values",
                Some("wrong_type"),
            )
        })?;
        if map.len() > limits.max_map_size {
            return Err(Fault::plain(
                false,
                format!(
                    "the context has {} keys, over the {} key limit",
                    map.len(),
                    limits.max_map_size
                ),
                Some("size"),
            ));
        }
        let mut scope = Scope::new();
        scope.push_constant_dynamic("ctx", Dynamic::from_map(map.clone()));
        for (key, value) in map {
            if is_identifier(&key) {
                scope.push_constant_dynamic(key, value);
            }
        }
        Ok(scope)
    }

    /// Run the rule against `context` and return whatever it answered. The context's own size is
    /// checked here, not by the caller, so every path into a rule gets the same limits.
    pub fn evaluate(&self, context: Dynamic) -> Result<Dynamic, Fault> {
        let context = check_context_size(context, self.limits, 0)?;
        let mut scope = self.scope(context, self.limits)?;
        let engine = engine_for(self.limits);
        let raw = engine
            .eval_ast_with_scope::<Dynamic>(&mut scope, &self.ast)
            .map_err(|err| Fault::from_alt(false, &err))?;
        if self.kind.wants_bool() && !raw.is_bool() {
            return Err(Fault::plain(
                false,
                format!(
                    "a {} rule must answer true or false, but this one answered a {}",
                    self.kind.as_str(),
                    type_name_of(&raw)
                ),
                Some("wrong_type"),
            ));
        }
        Ok(raw)
    }

    /// Run the rule and answer the only question a condition or a filter asks.
    pub fn matches(&self, context: Dynamic) -> Result<bool, Fault> {
        let raw = self.evaluate(context)?;
        raw.as_bool().map_err(|actual| {
            Fault::plain(
                false,
                format!(
                    "a {} rule must answer true or false, but this one answered a {actual}",
                    self.kind.as_str()
                ),
                Some("wrong_type"),
            )
        })
    }
}

/// Rhai identifiers: ASCII letters, digits and `_`, not starting with a digit.
pub fn is_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    match chars.next() {
        Some(first) if first.is_ascii_alphabetic() || first == '_' => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// Move a map out of a value we own. Rhai only lends maps out by reference, and the context is ours
/// to consume either way.
fn take_map(value: &mut Dynamic) -> Option<Map> {
    if !value.is_map() {
        return None;
    }
    value
        .as_map_mut()
        .ok()
        .map(|mut held| std::mem::take(&mut *held))
}

pub fn type_name_of(value: &Dynamic) -> &'static str {
    if value.is_bool() {
        "boolean"
    } else if value.is_int() {
        "integer"
    } else if value.is_float() {
        "float"
    } else if value.is_string() {
        "string"
    } else if value.is_array() {
        "array"
    } else if value.is_map() {
        "map"
    } else if value.is_unit() {
        "nothing"
    } else {
        "value"
    }
}

/// Check a value a caller wants to hand to a rule against the size limits *before* the engine sees
/// it, so an oversized context is refused with a clear reason instead of failing obscurely later.
pub fn check_context_size(value: Dynamic, limits: Limits, depth: usize) -> Result<Dynamic, Fault> {
    if depth > limits.max_context_depth {
        return Err(Fault::plain(
            false,
            format!(
                "the context nests deeper than the {} level limit",
                limits.max_context_depth
            ),
            Some("size"),
        ));
    }
    if value.is_unit() {
        return Ok(value);
    }
    if value.is_string() {
        let text = value.clone().into_string().unwrap_or_default();
        if text.chars().count() > limits.max_string_size {
            return Err(Fault::plain(
                false,
                format!(
                    "a context string is over the {} character limit",
                    limits.max_string_size
                ),
                Some("size"),
            ));
        }
        return Ok(Dynamic::from(text));
    }
    if value.is_array() {
        let array = value.into_array().unwrap_or_else(|_| Array::new());
        if array.len() > limits.max_array_size {
            return Err(Fault::plain(
                false,
                format!(
                    "a context array has {} items, over the {} item limit",
                    array.len(),
                    limits.max_array_size
                ),
                Some("size"),
            ));
        }
        let mut clipped: Array = Array::new();
        for item in array {
            clipped.push(check_context_size(item, limits, depth + 1)?);
        }
        return Ok(Dynamic::from_array(clipped));
    }
    if value.is_map() {
        let mut value = value;
        let map = take_map(&mut value).unwrap_or_default();
        if map.len() > limits.max_map_size {
            return Err(Fault::plain(
                false,
                format!(
                    "a context map has {} keys, over the {} key limit",
                    map.len(),
                    limits.max_map_size
                ),
                Some("size"),
            ));
        }
        let mut checked = Map::new();
        for (key, item) in map {
            if !is_identifier(&key) {
                return Err(Fault::plain(
                    false,
                    format!("{key:?} is not a name a rule could read"),
                    Some("wrong_type"),
                ));
            }
            checked.insert(key, check_context_size(item, limits, depth + 1)?);
        }
        return Ok(Dynamic::from_map(checked));
    }
    Ok(value)
}
