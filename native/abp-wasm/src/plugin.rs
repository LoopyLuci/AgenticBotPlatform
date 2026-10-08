//! ABP's WebAssembly component runtime: the host half of the `abp:plugin` world.
//!
//! A component is a plugin. It exports `abp:plugin/plugin@0.1.0` (`describe`, `invoke`)
//! and imports `abp:plugin/host@0.1.0` plus the WASI p2 interfaces ABP grants it. What a
//! plugin may reach is decided here, per plugin, from what its manifest asked for and
//! what a person approved - never from what the guest would like.
//!
//! Three limits stand between a plugin and the machine:
//!
//! * a **deadline**, enforced by epoch interruption: a guest that loops forever is
//!   stopped, not waited on;
//! * a **memory cap**, enforced by a [`StoreLimits`] limiter that traps on a grow past
//!   it rather than letting the host process grow;
//! * an **output cap**, checked before a result is handed back.
//!
//! Speed comes from doing the slow part once: the component is compiled and
//! pre-instantiated when it is loaded, so a call is a fresh store, an instantiate from
//! the cached [`InstancePre`], and the call itself.

use std::collections::BTreeMap;
use std::hash::Hash;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use wasmtime::component::{HasSelf, Linker, ResourceTable};
use wasmtime::{
    Config, Engine, InstanceAllocationStrategy, OptLevel, Store, StoreLimits, StoreLimitsBuilder,
};
use wasmtime_wasi::{FsPerms, WasiCtx, WasiCtxView, WasiView};

wasmtime::component::bindgen!({
    // The world lives at the repository root: one copy of it for the runtime, the SDK and
    // every example, rather than three that can drift apart.
    path: "../../wit",
    world: "plugin",
    imports: { default: trappable },
    with: {
        // WASI p2 already ships bindings generated against this same world version.
        // Mapping the namespace means ABP links those rather than generating a second,
        // subtly different copy of the same interfaces.
        "wasi": wasmtime_wasi::p2::bindings::sync,
    },
});

/// One tick of the epoch ticker, so how precisely a deadline can be honoured.
const TICK: Duration = Duration::from_millis(1);

/// One tool, as the plugin described it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Tool {
    pub name: String,
    pub description: String,
    /// JSON Schema for this tool's arguments, as JSON text.
    pub schema: String,
}

/// What `describe()` said about the plugin.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Manifest {
    pub name: String,
    pub version: String,
    pub description: String,
    pub tools: Vec<Tool>,
    /// Guest paths the plugin asked for. What it gets is `Grants::preopens`, which is
    /// what a person approved - the difference between the two is the whole point of
    /// asking before granting.
    pub preopens: Vec<String>,
    /// `host` or `host:port` entries the plugin asked for.
    pub network: Vec<String>,
}

/// What the host grants one plugin: the capabilities a person approved.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Grants {
    /// Guest path -> host directory. Read-write, and only these.
    pub preopens: BTreeMap<String, PathBuf>,
    /// `host` or `host:port` entries `http-fetch` may reach. Empty means no network.
    pub network: Vec<String>,
    /// Ceiling on what one call may return, in bytes.
    pub max_output_bytes: usize,
}

/// Why a call did not finish. The three a person needs told apart: a guest that ran too
/// long, one that tried to grow past its memory cap, one that failed for any other reason.
#[derive(Clone, Debug)]
pub enum CallError {
    /// The deadline passed and the call was interrupted.
    Deadline,
    /// The plugin tried to use more memory than it was given.
    Memory,
    /// The guest trapped, panicked, or the component was not usable at all.
    Trap(String),
    /// The host itself could not run it: a preopen that would not open, a cache
    /// directory that is not a directory.
    Host(String),
}

impl CallError {
    pub fn message(&self) -> String {
        self.to_string()
    }

    /// A short, stable tag for the audit log.
    pub fn kind(&self) -> &'static str {
        match self {
            CallError::Deadline => "deadline",
            CallError::Memory => "memory",
            CallError::Trap(_) => "trap",
            CallError::Host(_) => "host",
        }
    }
}

impl std::fmt::Display for CallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CallError::Deadline => {
                write!(f, "the plugin's deadline passed and the call was stopped")
            }
            CallError::Memory => {
                write!(f, "the plugin tried to use more memory than it was given")
            }
            CallError::Trap(why) => write!(f, "the plugin trapped: {why}"),
            CallError::Host(why) => write!(f, "the host could not run the plugin: {why}"),
        }
    }
}

impl std::error::Error for CallError {}

/// The host services a plugin calls back into. Behind a trait so the runtime's own tests
/// can supply one without a Python interpreter, and so ABP's Python side can supply one
/// that reaches that plugin's own store on disk.
pub trait HostServices: Send {
    fn log(&mut self, level: &str, msg: &str);
    fn store_get(&mut self, key: &str) -> Option<String>;
    fn store_set(&mut self, key: &str, value: &str) -> Result<(), String>;
    fn store_delete(&mut self, key: &str);
    fn store_keys(&mut self) -> Vec<String>;
    /// `Err(why)` when the plugin may not make this request, or the request failed.
    fn http_fetch(
        &mut self,
        method: &str,
        url: &str,
        headers: &[(String, String)],
        body: Option<&str>,
    ) -> Result<Response, String>;
    fn now_ms(&mut self) -> u64;
    fn random_bytes(&mut self, len: u32) -> Vec<u8>;
}

/// What `http-fetch` gives back.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: String,
}

/// What one invocation produced: the JSON result, and anything the plugin logged.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CallResult {
    /// The tool's result as JSON, or, when `failed`, the message the tool reported.
    pub json: String,
    pub logs: Vec<String>,
    /// The tool reported its own failure (the world's `err`): an answer, not a trap.
    pub failed: bool,
}

/// The store's data: WASI's own state, the host services, the limits, and this call's log.
pub struct HostState {
    wasi: WasiCtx,
    table: ResourceTable,
    services: SharedServices,
    limits: StoreLimits,
    logs: Vec<String>,
    /// How much log the guest may produce before the host starts dropping it, so a
    /// plugin that logs in a loop cannot grow the host's memory.
    max_log_lines: usize,
}

impl WasiView for HostState {
    fn ctx(&mut self) -> WasiCtxView<'_> {
        WasiCtxView {
            ctx: &mut self.wasi,
            table: &mut self.table,
        }
    }
}

impl abp::plugin::host::Host for HostState {
    fn log(&mut self, level: String, msg: String) -> wasmtime::Result<()> {
        if self.logs.len() < self.max_log_lines {
            self.logs.push(format!("[{level}] {msg}"));
        }
        self.services.log(&level, &msg);
        Ok(())
    }

    fn store_get(&mut self, key: String) -> wasmtime::Result<Option<String>> {
        Ok(self.services.store_get(&key))
    }

    fn store_set(&mut self, key: String, value: String) -> wasmtime::Result<()> {
        self.services
            .store_set(&key, &value)
            .map_err(wasmtime::Error::msg)
    }

    fn store_delete(&mut self, key: String) -> wasmtime::Result<()> {
        self.services.store_delete(&key);
        Ok(())
    }

    fn store_keys(&mut self) -> wasmtime::Result<Vec<String>> {
        Ok(self.services.store_keys())
    }

    fn http_fetch(
        &mut self,
        method: String,
        url: String,
        headers: Vec<(String, String)>,
        body: Option<String>,
    ) -> wasmtime::Result<Result<abp::plugin::host::Response, String>> {
        // The service's `Err` is the WIT `err` the guest sees - a refusal it can report in its
        // own result - rather than a trap, because "you may not go there" is an answer.
        Ok(self
            .services
            .http_fetch(&method, &url, &headers, body.as_deref())
            .map(wit_response))
    }

    fn now_ms(&mut self) -> wasmtime::Result<u64> {
        Ok(self.services.now_ms())
    }

    fn random_bytes(&mut self, len: u32) -> wasmtime::Result<Vec<u8>> {
        Ok(self.services.random_bytes(len))
    }
}

/// One plugin's host services, shared by every store that plugin runs in. A plugin`s
/// key-value store has to outlive the call that wrote to it, so the services live
/// alongside the component rather than inside the store.
#[derive(Clone)]
struct SharedServices(Arc<Mutex<Box<dyn HostServices>>>);

impl SharedServices {
    fn log(&self, level: &str, msg: &str) {
        if let Ok(mut s) = self.0.lock() {
            s.log(level, msg);
        }
    }

    fn store_get(&self, key: &str) -> Option<String> {
        self.0.lock().ok()?.store_get(key)
    }

    fn store_set(&self, key: &str, value: &str) -> Result<(), String> {
        self.0
            .lock()
            .map_err(|_| POISONED.to_string())?
            .store_set(key, value)
    }

    fn store_delete(&self, key: &str) {
        if let Ok(mut s) = self.0.lock() {
            s.store_delete(key);
        }
    }

    fn store_keys(&self) -> Vec<String> {
        self.0
            .lock()
            .map(|mut s| s.store_keys())
            .unwrap_or_default()
    }

    fn http_fetch(
        &self,
        method: &str,
        url: &str,
        headers: &[(String, String)],
        body: Option<&str>,
    ) -> Result<Response, String> {
        self.0
            .lock()
            .map_err(|_| POISONED.to_string())?
            .http_fetch(method, url, headers, body)
    }

    fn now_ms(&self) -> u64 {
        self.0
            .lock()
            .map(|mut s| s.now_ms())
            .unwrap_or_else(|_| now_ms())
    }

    fn random_bytes(&self, len: u32) -> Vec<u8> {
        self.0
            .lock()
            .map(|mut s| s.random_bytes(len))
            .unwrap_or_default()
    }
}

const POISONED: &str = "the plugin's host services are poisoned";

/// One plugin, ready to call: the compiled component, the pre-instantiated template, the
/// capabilities it was granted, and the services its `import host` calls land in.
pub struct LoadedPlugin {
    engine: Engine,
    /// The pre-instantiated component. It owns the compiled code and the export
    /// indices, which is what makes a call an instantiate rather than a compile.
    pre: PluginPre<HostState>,
    manifest: Manifest,
    grants: Grants,
    services: SharedServices,
    memory_limit: usize,
}

/// The engines whose epoch this process moves. A deadline is only a number until
/// something increments the epoch past it, and one thread doing that for every engine
/// is cheaper than one per plugin.
static ENGINES: Mutex<Vec<Engine>> = Mutex::new(Vec::new());

/// Whether the epoch ticker is running. It starts with the first engine and never
/// stops: it does nothing at all while no call is in flight.
static BUSY: AtomicUsize = AtomicUsize::new(0);
static TICKER: std::sync::OnceLock<()> = std::sync::OnceLock::new();

/// How long between engine ticks. Not a constant, because it is not one: a
/// `thread::sleep(1ms)` costs about 1 ms on a machine whose clock resolution is fine and
/// closer to 15 ms on one that is not, and a deadline expressed in ticks would then be a
/// deadline expressed in "however long this machine sleeps". The ticker measures its own
/// period and this is the running average, so a deadline means milliseconds.
static TICK_NS: AtomicU64 = AtomicU64::new(1_000_000);

/// Move the epoch for every engine this process has made, once per tick, but only while at
/// least one call is in flight. An idle ABP pays a relaxed load and a sleep, nothing else.
///
/// The sleep comes first and unconditionally: a loop that only slept when idle would spin as
/// fast as it could while a call was running, which turns a deadline counted in ticks into a
/// deadline counted in microseconds. One tick is one tick.
fn start_epoch_ticker() {
    TICKER.get_or_init(|| {
        std::thread::Builder::new()
            .name("abp-wasm-epoch".to_string())
            .spawn(|| {
                let mut last = std::time::Instant::now();
                loop {
                    std::thread::sleep(TICK);
                    let now = std::time::Instant::now();
                    let elapsed = now.duration_since(last).as_nanos() as u64;
                    last = now;
                    // An exponential average: long enough to smooth a scheduler hiccup out of
                    // the deadline, short enough to follow a machine that changes behaviour.
                    let previous = TICK_NS.load(Ordering::Relaxed);
                    let smoothed = (previous * 9 + elapsed) / 10;
                    TICK_NS.store(smoothed.max(1_000), Ordering::Relaxed);
                    if BUSY.load(Ordering::SeqCst) == 0 {
                        continue;
                    }
                    if let Ok(engines) = ENGINES.lock() {
                        for engine in engines.iter() {
                            engine.increment_epoch();
                        }
                    }
                }
            })
            .ok();
    });
}

/// The host's own `Response` as the world's `Response`. They are two types on purpose: the
/// host's is what the services return and knows nothing about the ABI, and the world's is
/// what a guest sees.
fn wit_response(response: Response) -> abp::plugin::host::Response {
    abp::plugin::host::Response {
        status: response.status,
        headers: response.headers,
        body: response.body,
    }
}

/// How many epoch ticks make up `ms` milliseconds, given how long a tick actually takes
/// here. Never zero: a deadline of no ticks is a deadline that has already passed, and the
/// call it guards would be refused before it started.
fn ticks_for(ms: u64) -> u64 {
    let tick_ns = TICK_NS.load(Ordering::Relaxed).max(1);
    let ticks = (ms.saturating_mul(1_000_000) / tick_ns).max(1);
    ticks.min(u64::from(u32::MAX))
}

impl LoadedPlugin {
    /// Compile a component, satisfy its imports and read its manifest. Everything slow
    /// happens here, so a later call is not the first time anything is compiled.
    pub fn new(
        component_bytes: &[u8],
        services: Box<dyn HostServices>,
        grants: Grants,
        memory_limit: usize,
    ) -> Result<LoadedPlugin, CallError> {
        let engine = new_engine()?;
        Self::with_engine(engine, component_bytes, services, grants, memory_limit)
    }

    /// As `new`, on an engine the caller owns. Plugins sharing an engine also share its
    /// compiled-component cache, which is what makes a warm start fast.
    pub fn with_engine(
        engine: Engine,
        component_bytes: &[u8],
        services: Box<dyn HostServices>,
        grants: Grants,
        memory_limit: usize,
    ) -> Result<LoadedPlugin, CallError> {
        Self::with_engine_and_cache(
            engine,
            component_bytes,
            services,
            grants,
            memory_limit,
            None,
        )
    }

    /// As `with_engine`, with a directory to cache the compiled component in.
    pub fn with_engine_and_cache(
        engine: Engine,
        component_bytes: &[u8],
        services: Box<dyn HostServices>,
        grants: Grants,
        memory_limit: usize,
        cache_dir: Option<&Path>,
    ) -> Result<LoadedPlugin, CallError> {
        let mut linker: Linker<HostState> = Linker::new(&engine);
        wasmtime_wasi::p2::add_to_linker_with_options_sync(&mut linker, &Default::default())
            .map_err(|e| CallError::Host(e.to_string()))?;
        abp::plugin::host::add_to_linker::<HostState, HasSelf<HostState>>(&mut linker, |s| s)
            .map_err(|e| CallError::Host(e.to_string()))?;

        let services = SharedServices(Arc::new(Mutex::new(services)));
        let mut store = Store::new(&engine, make_state(&services, &grants, memory_limit));
        store.limiter(|s| &mut s.limits);
        store.set_epoch_deadline(1);

        let component = compile(&engine, component_bytes, cache_dir)?;
        let pre = PluginPre::new(linker.instantiate_pre(&component).map_err(|e| {
            CallError::Trap(format!("the plugin's imports are not satisfiable: {e}"))
        })?)
        .map_err(classify)?;
        // Instantiate once now, so a component that cannot start is refused at install
        // time rather than at the agent's first tool call.
        let bindings = pre.instantiate(&mut store).map_err(classify)?;
        let manifest = bindings
            .abp_plugin_guest()
            .call_describe(&mut store)
            .map_err(classify)?;
        let manifest = manifest_from(manifest);

        if let Ok(mut engines) = ENGINES.lock() {
            if !engines.iter().any(|e| Engine::same(e, &engine)) {
                engines.push(engine.clone());
            }
        }
        start_epoch_ticker();

        Ok(LoadedPlugin {
            engine,
            pre,
            manifest,
            grants,
            services,
            memory_limit,
        })
    }

    /// What the plugin says about itself.
    pub fn manifest(&self) -> &Manifest {
        &self.manifest
    }

    /// What it was actually granted, which is not what it asked for.
    pub fn grants(&self) -> &Grants {
        &self.grants
    }

    pub fn tool(&self, name: &str) -> Option<&Tool> {
        self.manifest.tools.iter().find(|t| t.name == name)
    }

    /// Call one tool. `timeout_ms` is a wall-clock deadline: a guest that loops forever
    /// is interrupted rather than waited on.
    pub fn invoke(
        &self,
        tool: &str,
        args_json: &str,
        timeout_ms: u64,
    ) -> Result<CallResult, CallError> {
        if self.tool(tool).is_none() {
            return Err(CallError::Trap(format!(
                "no tool named {tool:?}; this plugin offers {}",
                self.manifest
                    .tools
                    .iter()
                    .map(|t| t.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )));
        }
        let mut store = Store::new(
            &self.engine,
            make_state(&self.services, &self.grants, self.memory_limit),
        );
        store.limiter(|s| &mut s.limits);
        let ticks = ticks_for(timeout_ms);
        store.set_epoch_deadline(ticks);

        BUSY.fetch_add(1, Ordering::SeqCst);
        // Instantiation runs guest code too (a component's start function), so it is
        // inside the deadline rather than assumed to be free.
        let bindings = match self.pre.instantiate(&mut store) {
            Ok(bindings) => bindings,
            Err(e) => {
                BUSY.fetch_sub(1, Ordering::SeqCst);
                return Err(classify(e));
            }
        };
        let outcome = bindings
            .abp_plugin_guest()
            .call_invoke(&mut store, tool, args_json);
        BUSY.fetch_sub(1, Ordering::SeqCst);

        let logs = std::mem::take(&mut store.data_mut().logs);
        match outcome {
            Ok(Ok(json)) => {
                if json.len() > self.grants.max_output_bytes {
                    return Err(CallError::Trap(format!(
                        "the tool returned {} bytes, over the {}-byte cap",
                        json.len(),
                        self.grants.max_output_bytes
                    )));
                }
                Ok(CallResult {
                    json,
                    logs,
                    failed: false,
                })
            }
            // A tool that reports its own failure is a result, not a trap: it decided its
            // arguments were wrong, which is the model's mistake to fix, rather than a
            // sandbox failure to report.
            Ok(Err(why)) => Ok(CallResult {
                json: why,
                logs,
                failed: true,
            }),
            Err(e) => Err(classify(e)),
        }
    }
}

/// The engine every plugin runs on unless the caller brings its own: Cranelift at speed,
/// epoch interruption on (that is the deadline), and the pooling allocator so an
/// instance is a slot rather than fresh mappings.
///
/// There is no cache here. Compiled components are cached by [`compile`], in a directory the
/// caller names, because a cache that silently shares a directory across engines with
/// different settings would hand out an artifact compiled for the wrong one.
pub fn new_engine() -> Result<Engine, CallError> {
    let mut config = Config::new();
    config.wasm_component_model(true);
    config.epoch_interruption(true);
    config.cranelift_opt_level(OptLevel::Speed);
    // One worker: ABP's own build budget is small, and a plugin compiling on two cores
    // is still compiling on one core while ABP is busy with everything else.
    config.parallel_compilation(false);
    let mut pooling = wasmtime::PoolingAllocationConfig::default();
    pooling.total_component_instances(64);
    pooling.total_core_instances(256);
    pooling.total_memories(64);
    pooling.total_tables(64);
    pooling.max_component_instance_size(4 * 1024 * 1024);
    pooling.max_core_instance_size(1024 * 1024);
    config.allocation_strategy(InstanceAllocationStrategy::Pooling(pooling));
    match Engine::new(&config) {
        Ok(engine) => Ok(engine),
        Err(e) => {
            // A machine that cannot give wasmtime a pooling pool still gets a runtime.
            // The pool is a speed choice, and refusing to run plugins at all over it
            // would trade a working feature for a small speedup.
            tracing_warn(&format!(
                "pooling allocation unavailable ({e}); using on-demand allocation"
            ));
            let mut plain = Config::new();
            plain.wasm_component_model(true);
            plain.epoch_interruption(true);
            plain.cranelift_opt_level(OptLevel::Speed);
            plain.parallel_compilation(false);
            Engine::new(&plain)
                .map_err(|e| CallError::Host(format!("cannot start the wasm engine: {e}")))
        }
    }
}

/// Compile a component, from a cache directory when there is one.
///
/// The cache is keyed on the component's bytes *and* on what this engine is: a precompiled
/// artifact is machine- and version-specific, and loading one that was compiled for a
/// different CPU or a different wasmtime is either a failure or, worse, something that runs.
/// `precompile_compatibility_hash` is exactly the value that decides this, so it is in the
/// key rather than in a comment.
fn compile(
    engine: &Engine,
    bytes: &[u8],
    cache_dir: Option<&Path>,
) -> Result<wasmtime::component::Component, CallError> {
    let Some(dir) = cache_dir else {
        return wasmtime::component::Component::new(engine, bytes)
            .map_err(|e| CallError::Trap(format!("not a component ABP can load: {e}")));
    };
    let path = cache_entry(engine, bytes, dir);
    match std::fs::read(&path) {
        Ok(artifact) => {
            match unsafe { wasmtime::component::Component::deserialize(engine, &artifact) } {
                Ok(component) => return Ok(component),
                Err(e) => {
                    // A corrupt or truncated cache entry is not fatal: compile again and replace
                    // it. The cache is an optimisation and is not allowed to be a failure mode.
                    tracing_warn(&format!(
                        "cached component at {} did not load ({e}); recompiling",
                        path.display()
                    ));
                }
            }
        }
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
            tracing_warn(&format!(
                "cannot read the component cache at {}: {e}",
                path.display()
            ));
        }
        Err(_) => {}
    }
    let component = wasmtime::component::Component::new(engine, bytes)
        .map_err(|e| CallError::Trap(format!("not a component ABP can load: {e}")))?;
    match component.serialize() {
        Ok(artifact) => {
            if let Err(e) = write_cache_entry(&path, &artifact) {
                tracing_warn(&format!(
                    "cannot write the component cache at {}: {e}",
                    path.display()
                ));
            }
        }
        Err(e) => tracing_warn(&format!("cannot serialize the compiled component: {e}")),
    }
    Ok(component)
}

/// Where a component's compiled form is kept, if anywhere.
fn cache_entry(engine: &Engine, bytes: &[u8], dir: &Path) -> PathBuf {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    Hash::hash(&env!("CARGO_PKG_VERSION"), &mut hasher);
    std::hash::Hash::hash(&wasmtime_version(), &mut hasher);
    Hash::hash(&engine.precompile_compatibility_hash(), &mut hasher);
    Hash::hash(bytes, &mut hasher);
    dir.join(format!("{:016x}.cwasm", std::hash::Hasher::finish(&hasher)))
}

pub fn wasmtime_version() -> &'static str {
    // The wasmtime release this was built against. The crate's version moves with ABP's own
    // version numbers, so it cannot stand in for wasmtime's.
    "49"
}

/// Write a cache entry through a temporary file and rename it, so a crash mid-write cannot
/// leave a half-written artifact that looks like a hit next time.
fn write_cache_entry(path: &Path, artifact: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("cwasm.tmp");
    std::fs::write(&tmp, artifact)?;
    std::fs::rename(&tmp, path)
}

/// wasmtime's own log, without a subscriber: a note ABP wrote rather than a dependency's.
fn tracing_warn(message: &str) {
    eprintln!("abp_wasm: {message}");
}

fn make_state(services: &SharedServices, grants: &Grants, memory_limit: usize) -> HostState {
    HostState {
        wasi: build_wasi(grants),
        table: ResourceTable::new(),
        services: services.clone(),
        limits: StoreLimitsBuilder::new()
            .memory_size(memory_limit)
            .memories(8)
            .tables(16)
            .trap_on_grow_failure(true)
            .build(),
        logs: Vec::new(),
        max_log_lines: 512,
    }
}

/// The WASI context a plugin runs with: no environment, no arguments, no inherited
/// streams, and exactly the directories that were approved. A plugin that was granted
/// nothing gets no preopens, so the guest's `openat` on an absolute path has nothing to
/// resolve against.
fn build_wasi(grants: &Grants) -> WasiCtx {
    let mut builder = WasiCtx::builder();
    builder.arg("abp-wasm-plugin");
    builder.env("ABP_WASM_PLUGIN", "1");
    for (guest, host) in &grants.preopens {
        if let Err(e) = builder.preopened_dir(host, guest, FsPerms::ReadWrite) {
            // A preopen that would not open is recorded rather than fatal: the plugin
            // still runs, with the directories that did open. A half-granted plugin that
            // cannot work at all fails on its own call, with the real error.
            tracing_warn(&format!(
                "cannot give the plugin the directory {guest}: {e}"
            ));
        }
    }
    builder.build()
}

/// Milliseconds since the Unix epoch.
pub fn now_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// A deadline expressed as a `Duration`, for callers that would rather say "2 seconds".
pub fn deadline_ms(d: Duration) -> u64 {
    d.as_millis() as u64
}

/// Turn a wasmtime error into the reason a person wants to read. The distinction that
/// matters is which limit stopped it: a deadline and a memory cap are the sandbox
/// working, anything else is the guest failing.
pub fn classify(err: wasmtime::Error) -> CallError {
    use wasmtime::Trap;
    let root = err.root_cause();
    let text = root.to_string();
    if let Some(trap) = root.downcast_ref::<Trap>() {
        if matches!(trap, Trap::Interrupt) {
            return CallError::Deadline;
        }
    }
    if is_memory_failure(&text) {
        return CallError::Memory;
    }
    CallError::Trap(text)
}

/// Whether an error is the memory cap rather than the guest failing.
///
/// The limiter's trap arrives as an ordinary error, so the only way to tell "this plugin
/// tried to grow past its cap" from "this plugin trapped" is the wording. That is worth the
/// fragility: a caller reporting "the plugin ran out of memory" when it in fact trapped is
/// the difference between a fixable limit and a mystery, and both messages have to come from
/// something.
fn is_memory_failure(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    lower.contains("forcing trap when growing memory")
        || lower.contains("exceeds memory limits")
        || lower.contains("memory minimum size")
        || lower.contains("memory limit")
        || lower.contains("out of memory")
        || lower.contains("allocation of")
        || lower.contains("memory allocation")
}

fn manifest_from(m: abp::plugin::manifest::Manifest) -> Manifest {
    Manifest {
        name: m.name,
        version: m.version,
        description: m.description,
        tools: m
            .tools
            .iter()
            .map(|t| Tool {
                name: t.name.clone(),
                description: t.description.clone(),
                schema: t.schema.clone(),
            })
            .collect(),
        preopens: m.preopens,
        network: m.network,
    }
}

/// Whether `url`'s host is on an allowlist. An entry is `host` (any port) or
/// `host:port`. An empty allowlist allows nothing, and only http and https are ever
/// considered - a plugin's `http-fetch` is not a way to open `file:///etc/passwd`.
pub fn host_allowed(url: &str, allow: &[String]) -> bool {
    let Some((scheme, rest)) = url.split_once("://") else {
        return false;
    };
    if !matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https") {
        return false;
    }
    let authority = rest
        .split(['/', '?', '#'])
        .next()
        .unwrap_or("")
        .rsplit_once('@')
        .map(|(_, host)| host)
        .unwrap_or_else(|| rest.split(['/', '?', '#']).next().unwrap_or(""));
    if authority.is_empty() {
        return false;
    }
    let (host, port) = split_host_port(authority);
    allow.iter().any(|entry| {
        let (entry_host, entry_port) = split_host_port(entry);
        // An entry with a port means that port; one without means any port.
        host.eq_ignore_ascii_case(entry_host) && (entry_port.is_none() || port == entry_port)
    })
}

/// Split `host` or `host:port` into the host and an optional port. A bracketed IPv6
/// literal is one host, with the brackets stripped on both sides so `[::1]` in an
/// allowlist entry matches `::1` in a URL.
fn split_host_port(authority: &str) -> (&str, Option<&str>) {
    if let Some(rest) = authority.strip_prefix('[') {
        let (literal, after) = rest.split_once(']').unwrap_or((rest, ""));
        return match after.strip_prefix(':') {
            Some(port) if !port.is_empty() && port.chars().all(|c| c.is_ascii_digit()) => {
                (literal, Some(port))
            }
            _ => (literal, None),
        };
    }
    match authority.rsplit_once(':') {
        Some((host, port)) if !port.is_empty() && port.chars().all(|c| c.is_ascii_digit()) => {
            (host, Some(port))
        }
        _ => (authority, None),
    }
}

/// A `HostServices` backed by an in-memory map: the runtime's own tests, and any caller
/// whose store the host has not put on disk. One of these per plugin, so two plugins
/// never see each other's keys.
#[derive(Default)]
pub struct MemoryStore {
    entries: Mutex<BTreeMap<String, String>>,
    logs: Mutex<Vec<String>>,
}

impl MemoryStore {
    pub fn keys(&self) -> Vec<String> {
        self.entries
            .lock()
            .map(|e| e.keys().cloned().collect())
            .unwrap_or_default()
    }

    pub fn logs(&self) -> Vec<String> {
        self.logs.lock().map(|l| l.clone()).unwrap_or_default()
    }
}

impl HostServices for MemoryStore {
    fn log(&mut self, _level: &str, msg: &str) {
        if let Ok(mut logs) = self.logs.lock() {
            logs.push(msg.to_string());
        }
    }

    fn store_get(&mut self, key: &str) -> Option<String> {
        self.entries.lock().ok()?.get(key).cloned()
    }

    fn store_set(&mut self, key: &str, value: &str) -> Result<(), String> {
        self.entries
            .lock()
            .map_err(|_| POISONED.to_string())?
            .insert(key.to_string(), value.to_string());
        Ok(())
    }

    fn store_delete(&mut self, key: &str) {
        if let Ok(mut entries) = self.entries.lock() {
            entries.remove(key);
        }
    }

    fn store_keys(&mut self) -> Vec<String> {
        self.entries
            .lock()
            .map(|e| e.keys().cloned().collect())
            .unwrap_or_default()
    }

    fn http_fetch(
        &mut self,
        _method: &str,
        url: &str,
        _headers: &[(String, String)],
        _body: Option<&str>,
    ) -> Result<Response, String> {
        Err(format!(
            "no network was granted to this plugin (it asked for {url})"
        ))
    }

    fn now_ms(&mut self) -> u64 {
        now_ms()
    }

    fn random_bytes(&mut self, len: u32) -> Vec<u8> {
        (0..len)
            .map(|i| (i.wrapping_mul(2654435761) >> 13) as u8)
            .collect()
    }
}

/// Where one plugin's store lives. Two plugins never share one, because the name is part
/// of the path and the runtime is told the path rather than being asked to remember.
pub fn store_dir_for(root: &Path, plugin: &str) -> PathBuf {
    root.join(plugin)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_empty_allowlist_allows_nothing() {
        assert!(!host_allowed("https://example.com/x", &[]));
    }

    #[test]
    fn a_bare_host_allows_any_port_but_not_another_host() {
        let allow = vec!["example.com".to_string()];
        assert!(host_allowed("http://example.com/x", &allow));
        assert!(host_allowed("https://example.com:8443/x", &allow));
        assert!(!host_allowed("https://other.example/x", &allow));
        assert!(!host_allowed("https://example.com.evil.test/x", &allow));
    }

    #[test]
    fn host_and_port_must_both_match() {
        let allow = vec!["example.com:8443".to_string()];
        assert!(host_allowed("https://example.com:8443/x", &allow));
        assert!(!host_allowed("https://example.com/x", &allow));
        assert!(!host_allowed("https://example.com:9443/x", &allow));
    }

    #[test]
    fn only_http_and_https_are_considered() {
        let allow = vec!["example.com".to_string()];
        assert!(!host_allowed("file:///etc/passwd", &allow));
        assert!(!host_allowed("gopher://example.com", &allow));
        assert!(!host_allowed("example.com/x", &allow));
    }

    #[test]
    fn credentials_in_the_url_do_not_hide_the_host() {
        let allow = vec!["example.com".to_string()];
        assert!(host_allowed("https://user:pw@example.com/x", &allow));
        assert!(!host_allowed("https://user:pw@evil.test/x", &allow));
    }

    #[test]
    fn an_ipv6_literal_is_one_host() {
        let allow = vec!["[::1]".to_string()];
        assert!(host_allowed("http://[::1]:8080/x", &allow));
        let exact = vec!["[::1]:8080".to_string()];
        assert!(host_allowed("http://[::1]:8080/x", &exact));
        assert!(!host_allowed("http://[::1]:9090/x", &exact));
    }

    #[test]
    fn a_deadline_is_never_zero_ticks() {
        // A deadline of no ticks has already passed, and would refuse the call before it
        // started rather than run it. Every deadline is at least one tick.
        assert_eq!(ticks_for(0), 1);
        assert!(ticks_for(250) >= 1);
        assert!(ticks_for(30_000) >= ticks_for(250));
    }

    #[test]
    fn two_stores_do_not_see_each_other() {
        let mut a = MemoryStore::default();
        let mut b = MemoryStore::default();
        a.store_set("k", "a").unwrap();
        assert_eq!(a.store_get("k").as_deref(), Some("a"));
        assert_eq!(b.store_get("k"), None);
        b.store_set("k", "b").unwrap();
        assert_eq!(a.store_get("k").as_deref(), Some("a"));
    }
}
