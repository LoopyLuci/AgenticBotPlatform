//! ABP's WebAssembly component runtime, as a Python extension module.
//!
//! The module is deliberately thin. It owns the things only Rust can do - compile a
//! component, enforce a deadline and a memory cap inside the engine, and mediate every
//! capability the guest asks for - and leaves every policy decision (what to install,
//! what to grant, whether a person approved it) to `bot/wasm_plugins/`.
//!
//! ```python
//! import abp_wasm
//!
//! runtime = abp_wasm.Runtime(cache_dir=...)          # one per ABP process
//! plugin = runtime.load(path, grants=..., services=...)  # compiles + reads the manifest
//! plugin.manifest                                     # {name, version, tools, ...}
//! plugin.invoke("tool", "{}", timeout_ms=2000)        # -> dict
//! ```
//!
//! `ABP_NATIVE=0` in the environment means this module is not used at all; the Python
//! side then says so once and disables the feature (see bot/wasm_plugins/runtime.py).

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

pub mod plugin;

use crate::plugin::{
    host_allowed, new_engine, now_ms, wasmtime_version, CallError, Grants, HostServices,
    LoadedPlugin, Manifest, Response, Tool,
};

#[pymodule]
fn abp_wasm(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<Runtime>()?;
    m.add_class::<PluginHandle>()?;
    m.add("WasmError", m.py().get_type::<WasmError>())?;
    m.add_function(wrap_pyfunction!(wasmtime_version_py, m)?)?;
    Ok(())
}

#[pyfunction]
#[pyo3(name = "wasmtime_version")]
fn wasmtime_version_py() -> &'static str {
    // The wasmtime release this was built against, from plugin.rs: a plugin that works on
    // one is not guaranteed to work on another, and an install-time check may want to know.
    wasmtime_version()
}

// The engine, shared by every plugin in the process so a compiled component is compiled
// once and so the epoch ticker has one set of engines to move.
#[pyclass]
struct Runtime {
    engine: wasmtime::Engine,
    #[allow(dead_code)]
    cache_dir: Option<PathBuf>,
}

#[pymethods]
impl Runtime {
    #[new]
    #[pyo3(signature = (cache_dir = None))]
    fn new(cache_dir: Option<PathBuf>) -> PyResult<Self> {
        let engine = new_engine().map_err(|e| wasm_error("host", &e.message()))?;
        if let Some(dir) = cache_dir.as_deref() {
            // Fail here rather than at the first load: a cache directory that cannot be
            // created is a configuration problem, and finding out about it when someone
            // installs a plugin is too late.
            std::fs::create_dir_all(dir).map_err(|e| {
                wasm_error(
                    "host",
                    &format!(
                        "cannot create the component cache at {}: {e}",
                        dir.display()
                    ),
                )
            })?;
        }
        Ok(Runtime { engine, cache_dir })
    }

    /// Compile a component and return the handle ABP calls tools through. The bytes are
    /// read and compiled here, so a malformed component fails at install time rather
    /// than at the agent's first tool call.
    #[pyo3(signature = (path, *, grants, services, memory_limit_bytes = 16 * 1024 * 1024))]
    fn load<'py>(
        &self,
        py: Python<'py>,
        path: PathBuf,
        grants: &Bound<'py, PyDict>,
        services: Py<PyAny>,
        memory_limit_bytes: usize,
    ) -> PyResult<PluginHandle> {
        let bytes = std::fs::read(&path).map_err(|e| {
            wasm_error(
                "host",
                &format!("cannot read the component at {}: {e}", path.display()),
            )
        })?;
        self.compile(py, bytes, grants, services, memory_limit_bytes)
    }

    /// As `load`, from bytes already in hand: a download, an upload, a test fixture.
    #[pyo3(signature = (data, *, grants, services, memory_limit_bytes = 16 * 1024 * 1024))]
    fn load_bytes<'py>(
        &self,
        py: Python<'py>,
        data: Vec<u8>,
        grants: &Bound<'py, PyDict>,
        services: Py<PyAny>,
        memory_limit_bytes: usize,
    ) -> PyResult<PluginHandle> {
        self.compile(py, data, grants, services, memory_limit_bytes)
    }

    /// The compile `load` and `load_bytes` share.
    #[pyo3(signature = (data, *, grants, services, memory_limit_bytes))]
    fn compile<'py>(
        &self,
        py: Python<'py>,
        data: Vec<u8>,
        grants: &Bound<'py, PyDict>,
        services: Py<PyAny>,
        memory_limit_bytes: usize,
    ) -> PyResult<PluginHandle> {
        let grants = parse_grants(grants)?;
        let services = PyServices::new(services)?;
        let network = grants.network.clone();
        let cache_dir = self.cache_dir.clone();
        py.detach(|| {
            LoadedPlugin::with_engine_and_cache(
                self.engine.clone(),
                &data,
                Box::new(services),
                grants,
                memory_limit_bytes,
                cache_dir.as_deref(),
            )
        })
        .map(|plugin| PluginHandle {
            plugin: Arc::new(plugin),
            network,
        })
        .map_err(|e| wasm_error(e.kind(), &e.message()))
    }
}

// One loaded plugin: its manifest, and the call that runs a tool.
#[pyclass]
struct PluginHandle {
    plugin: Arc<LoadedPlugin>,
    network: Vec<String>,
}

#[pymethods]
impl PluginHandle {
    // What `describe()` said: name, version, description, tools, and what it asked for.
    #[getter]
    fn manifest(&self) -> PyResult<Py<PyDict>> {
        Python::attach(|py| manifest_to_py(py, self.plugin.manifest()))
    }

    // The tool names, for the plugin list without paying for the whole manifest.
    fn tool_names(&self) -> Vec<String> {
        self.plugin
            .manifest()
            .tools
            .iter()
            .map(|t| t.name.clone())
            .collect()
    }

    // Run one tool. Returns `{"ok": json}` or `{"error": message}`; a sandbox failure
    // (a deadline, a memory cap) raises `WasmError` instead, because those are not
    // answers the model can do anything with.
    #[pyo3(signature = (tool, args_json = "{}", *, timeout_ms = 2000))]
    fn invoke(&self, tool: &str, args_json: &str, timeout_ms: u64) -> PyResult<Py<PyDict>> {
        let started = Instant::now();
        let outcome = self.plugin.invoke(tool, args_json, timeout_ms);
        let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        match outcome {
            Ok(result) => Python::attach(|py| {
                let out = PyDict::new(py);
                // The world's `err` is the tool's own failure: reported as `error`, so it is never
                // mistaken for a result.
                out.set_item(if result.failed { "error" } else { "ok" }, result.json)?;
                out.set_item("logs", result.logs)?;
                out.set_item("elapsed_ms", elapsed_ms)?;
                Ok(out.unbind())
            }),
            Err(e) if matches!(e, CallError::Deadline | CallError::Memory) => {
                Err(wasm_error(e.kind(), &e.message()))
            }
            Err(CallError::Trap(why)) => Python::attach(|py| {
                // A trap is reported as a failed call rather than raised: the plugin ran
                // and failed, and the model may be able to call it differently.
                let out = PyDict::new(py);
                out.set_item("error", format!("the plugin trapped: {why}"))?;
                out.set_item("kind", "trap")?;
                out.set_item("elapsed_ms", elapsed_ms)?;
                Ok(out.unbind())
            }),
            Err(e) => Err(wasm_error(e.kind(), &e.message())),
        }
    }

    // Whether a URL is on what this plugin was actually granted. ABP checks before it
    // makes a request, so a refusal is a refusal rather than a round trip.
    fn allows_url(&self, url: &str) -> bool {
        host_allowed(url, &self.network)
    }
}

// A sandbox-level failure: the deadline, the memory cap, or the host's own inability
// to run the plugin. Distinct from a tool that returned an error, which is data.
//
// The kind is in the message (`wasm deadline: ...`) rather than on the exception, so
// `except WasmError as e:` and a message that survives a log both say which limit it
// was - and so no Python-3.12-only exception subclassing is needed on an abi3 build.
pyo3::create_exception!(abp_wasm, WasmError, PyRuntimeError, "...");

fn wasm_error(kind: &str, message: &str) -> PyErr {
    WasmError::new_err(format!("wasm {kind}: {message}"))
}

fn manifest_to_py(py: Python<'_>, manifest: &Manifest) -> PyResult<Py<PyDict>> {
    let out = PyDict::new(py);
    out.set_item("name", &manifest.name)?;
    out.set_item("version", &manifest.version)?;
    out.set_item("description", &manifest.description)?;
    let tools = PyList::empty(py);
    for tool in &manifest.tools {
        tools.append(tool_to_py(py, tool)?)?;
    }
    out.set_item("tools", tools)?;
    out.set_item("preopens", manifest.preopens.clone())?;
    out.set_item("network", manifest.network.clone())?;
    Ok(out.unbind())
}

fn tool_to_py(py: Python<'_>, tool: &Tool) -> PyResult<Py<PyDict>> {
    let out = PyDict::new(py);
    out.set_item("name", &tool.name)?;
    out.set_item("description", &tool.description)?;
    out.set_item("schema", &tool.schema)?;
    Ok(out.unbind())
}

// The approved capabilities, as `bot/wasm_plugins` hands them over: exactly what a
// person agreed to, never the manifest's own wishes.
fn parse_grants(grants: &Bound<'_, PyDict>) -> PyResult<Grants> {
    let mut out = Grants::default();
    if let Some(preopens) = grants.get_item("preopens")? {
        let preopens = preopens.cast::<PyDict>().map_err(|_| {
            PyValueError::new_err(
                "grants['preopens'] must be a dict of guest path -> host directory",
            )
        })?;
        for (guest, host) in preopens.iter() {
            out.preopens.insert(
                guest.extract::<String>()?,
                PathBuf::from(host.extract::<String>()?),
            );
        }
    }
    if let Some(network) = grants.get_item("network")? {
        out.network = network.extract::<Vec<String>>()?;
    }
    out.max_output_bytes = grants
        .get_item("max_output_bytes")?
        .map(|v| v.extract::<usize>())
        .transpose()?
        .unwrap_or(256 * 1024);
    Ok(out)
}

// The Python object ABP passes as `services`: where a plugin's `import host` calls
// land. The methods are looked up once, at load, so a call costs a dict lookup rather
// than an attribute lookup on a Python object per guest call.
struct PyServices {
    log: Py<PyAny>,
    store_get: Py<PyAny>,
    store_set: Py<PyAny>,
    store_delete: Py<PyAny>,
    store_keys: Py<PyAny>,
    http_fetch: Py<PyAny>,
    now: Py<PyAny>,
    random_bytes: Py<PyAny>,
}

impl PyServices {
    fn new(services: Py<PyAny>) -> PyResult<Self> {
        let need = |name: &str| -> PyResult<Py<PyAny>> {
            Python::attach(|py| {
                services
                    .bind(py)
                    .getattr(name)
                    .map(|attr| attr.unbind())
                    .map_err(|_| {
                        PyValueError::new_err(format!("the services object has no {name}()"))
                    })
            })
        };
        Ok(PyServices {
            log: need("log")?,
            store_get: need("store_get")?,
            store_set: need("store_set")?,
            store_delete: need("store_delete")?,
            store_keys: need("store_keys")?,
            http_fetch: need("http_fetch")?,
            now: need("now_ms")?,
            random_bytes: need("random_bytes")?,
        })
    }
}

impl HostServices for PyServices {
    fn log(&mut self, level: &str, msg: &str) {
        Python::attach(|py| {
            let _ = self.log.bind(py).call1((level, msg));
        });
    }

    fn store_get(&mut self, key: &str) -> Option<String> {
        Python::attach(|py| {
            self.store_get
                .bind(py)
                .call1((key,))
                .ok()
                .and_then(|v| v.extract::<Option<String>>().ok())
                .flatten()
        })
    }

    fn store_set(&mut self, key: &str, value: &str) -> Result<(), String> {
        Python::attach(|py| {
            self.store_set
                .bind(py)
                .call1((key, value))
                .map(|_| ())
                .map_err(python_error)
        })
    }

    fn store_delete(&mut self, key: &str) {
        Python::attach(|py| {
            let _ = self.store_delete.bind(py).call1((key,));
        });
    }

    fn store_keys(&mut self) -> Vec<String> {
        Python::attach(|py| {
            self.store_keys
                .bind(py)
                .call0()
                .ok()
                .and_then(|v| v.extract::<Vec<String>>().ok())
                .unwrap_or_default()
        })
    }

    fn http_fetch(
        &mut self,
        method: &str,
        url: &str,
        headers: &[(String, String)],
        body: Option<&str>,
    ) -> Result<Response, String> {
        Python::attach(|py| {
            let headers: Vec<(String, String)> = headers.to_vec();
            let raw = self
                .http_fetch
                .bind(py)
                .call1((method, url, headers, body))
                .map_err(python_error)?;
            // Either a dict {status, headers, body} or a string explaining the refusal.
            if let Ok(text) = raw.extract::<String>() {
                return Err(text);
            }
            let dict = raw.cast::<PyDict>().map_err(|_| {
                "http_fetch must return a dict {status, headers, body} or a string explaining why not"
                    .to_string()
            })?;
            let status = dict
                .get_item("status")
                .map_err(python_error)?
                .map(|v| v.extract::<u16>())
                .transpose()
                .map_err(python_error)?
                .unwrap_or(200);
            let body = dict
                .get_item("body")
                .map_err(python_error)?
                .map(|v| v.extract::<String>())
                .transpose()
                .map_err(python_error)?
                .unwrap_or_default();
            let headers = dict
                .get_item("headers")
                .map_err(python_error)?
                .map(|v| headers_to_vec(&v))
                .transpose()
                .map_err(python_error)?
                .unwrap_or_default();
            Ok(Response {
                status,
                headers,
                body,
            })
        })
    }

    fn now_ms(&mut self) -> u64 {
        Python::attach(|py| {
            self.now
                .bind(py)
                .call0()
                .ok()
                .and_then(|v| v.extract::<u64>().ok())
                .unwrap_or_else(now_ms)
        })
    }

    fn random_bytes(&mut self, len: u32) -> Vec<u8> {
        Python::attach(|py| {
            self.random_bytes
                .bind(py)
                .call1((len,))
                .ok()
                .and_then(|v| v.extract::<Vec<u8>>().ok())
                .unwrap_or_default()
        })
    }
}

/// The `headers` of a fetch response, as the world wants them: a list of pairs. A Python
/// caller naturally has a dict, so both are accepted - HTTP has no duplicate header names
/// that matter here, and refusing a dict would make the obvious Python call the wrong one.
fn headers_to_vec(value: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String)>> {
    if let Ok(pairs) = value.extract::<Vec<(String, String)>>() {
        return Ok(pairs);
    }
    let dict = value.cast::<PyDict>().map_err(|_| {
        PyValueError::new_err(
            "fetch response headers must be a dict or a list of (name, value) pairs",
        )
    })?;
    let mut out = Vec::new();
    for (name, header) in dict.iter() {
        out.push((
            name.extract::<String>()?,
            header.extract::<String>().unwrap_or_default(),
        ));
    }
    Ok(out)
}

/// A Python exception as a line of text: what a plugin's `http-fetch` refusal should
/// read like in the plugin's own error, and what goes in ABP's log.
fn python_error(err: PyErr) -> String {
    Python::attach(|py| {
        err.value(py)
            .str()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|_| err.to_string())
    })
}
