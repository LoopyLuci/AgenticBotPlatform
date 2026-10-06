//! The Python bridge: conversions in both directions, the exception types, and the `abp_rules`
//! module itself. Nothing here decides what a rule may do - that is all `super::sandbox`.

use std::collections::BTreeMap;
use std::sync::Arc;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use rhai::{Array, Dynamic, Map};

use crate::sandbox::{self, Fault, Kind, Limits};

create_exception!(
    abp_rules,
    RuleError,
    PyException,
    "Base class for every rule failure: a rule that would not compile, or would not run."
);
create_exception!(
    abp_rules,
    CompileError,
    RuleError,
    "The rule's source could not be compiled. `line` and `column` say where."
);
create_exception!(
    abp_rules,
    EvalError,
    RuleError,
    "The rule compiled but failed while running, including hitting a sandbox limit."
);

/// A context key no rule could read. Names are Rhai identifiers: letters, digits and `_`.
fn key_fault(key: &str) -> Fault {
    Fault {
        compile: false,
        message: format!(
            "{key:?} is not a name a rule could read: a context key is letters, digits and _"
        ),
        line: 0,
        column: 0,
        limit: Some("wrong_type".to_string()),
    }
}

/// A compiled rule. Immutable and shareable: `compile()` caches the parsed AST, so holding one of
/// these for a rule's whole life costs one parse, not one parse per message.
#[pyclass(name = "Program", frozen, module = "abp_rules")]
pub struct PyProgram {
    inner: Arc<sandbox::Program>,
}

#[pymethods]
impl PyProgram {
    /// Evaluate the rule and return whatever it answered. For a route that is the target's name; for
    /// a condition or a filter it is True or False.
    fn evaluate<'py>(
        &self,
        py: Python<'py>,
        context: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        dynamic_to(py, self.run(context).map_err(to_py_err)?)
    }

    /// Evaluate the rule and answer True or False. The one call a condition or a filter makes.
    fn matches(&self, context: &Bound<'_, PyDict>) -> PyResult<bool> {
        match self.run(context) {
            Ok(value) => value.as_bool().map_err(|actual| {
                to_py_err(Fault {
                    compile: false,
                    message: format!(
                        "a {} rule answered a {actual}, not true or false",
                        self.inner.kind().as_str()
                    ),
                    line: 0,
                    column: 0,
                    limit: Some("wrong_type".to_string()),
                })
            }),
            Err(fault) => Err(to_py_err(fault)),
        }
    }

    /// Which shape this rule is: "condition", "filter" or "route".
    #[getter]
    fn kind(&self) -> &'static str {
        self.inner.kind().as_str()
    }

    /// The sandbox limits this rule runs under, so a caller can show them beside the source.
    #[getter]
    fn limits(&self) -> BTreeMap<&'static str, usize> {
        limits_dict(self.inner.limits())
    }

    fn __repr__(&self) -> String {
        format!("<abp_rules.Program kind={}>", self.inner.kind().as_str())
    }
}

impl PyProgram {
    fn run(&self, context: &Bound<'_, PyDict>) -> Result<Dynamic, Fault> {
        self.inner
            .evaluate(dict_to_dynamic(context, self.inner.limits())?)
    }
}

fn limits_dict(limits: Limits) -> BTreeMap<&'static str, usize> {
    BTreeMap::from([
        ("max_operations", limits.max_operations as usize),
        ("max_call_levels", limits.max_call_levels),
        ("max_string_size", limits.max_string_size),
        ("max_array_size", limits.max_array_size),
        ("max_map_size", limits.max_map_size),
        ("max_expr_depth", limits.max_expr_depth),
        ("max_variables", limits.max_variables),
        ("max_source_chars", limits.max_source_chars),
        ("max_context_depth", limits.max_context_depth),
    ])
}

/// Register everything on the `abp_rules` module.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = module.py();
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("RuleError", py.get_type::<RuleError>())?;
    module.add("CompileError", py.get_type::<CompileError>())?;
    module.add("EvalError", py.get_type::<EvalError>())?;
    module.add("KINDS", vec!["condition", "filter", "route"])?;
    module.add_function(wrap_pyfunction!(limits, module)?)?;
    module.add_function(wrap_pyfunction!(compile, module)?)?;
    module.add_function(wrap_pyfunction!(cache_stats, module)?)?;
    Ok(())
}

/// Every limit the sandbox enforces, so a page can show what "sandboxed" means here without having
/// to read this crate's source.
#[pyfunction]
fn limits() -> BTreeMap<&'static str, usize> {
    limits_dict(Limits::default())
}

/// Parse `source` once. The AST is cached by source, so compiling the same rule again costs a
/// dictionary lookup.
#[pyfunction]
#[pyo3(signature = (source, kind = "condition"))]
fn compile(source: &str, kind: &str) -> PyResult<PyProgram> {
    let kind = Kind::parse(kind).ok_or_else(|| {
        PyValueError::new_err(format!(
            "a rule's kind is condition, filter or route, not {kind:?}"
        ))
    })?;
    sandbox::compile(source, kind, Limits::default())
        .map(|program| PyProgram {
            inner: Arc::new(program),
        })
        .map_err(to_py_err)
}

/// (parse hits, parse misses, ASTs held). A page can show these; the tests prove a rule is parsed
/// once rather than once per message.
#[pyfunction]
fn cache_stats() -> (u64, u64, usize) {
    sandbox::cache_stats()
}

/// Turn a sandbox failure into the right Python exception, with its position attached so a page can
/// point at the exact character in the source that is wrong.
fn to_py_err(fault: Fault) -> PyErr {
    Python::attach(|py| {
        let position = if fault.line > 0 {
            format!(" (line {}, column {})", fault.line, fault.column)
        } else {
            String::new()
        };
        let message = format!("{}{position}", fault.message);
        let err = if fault.compile {
            CompileError::new_err(message)
        } else {
            EvalError::new_err(message)
        };
        let instance = err.value(py);
        let _ = instance.setattr("line", fault.line);
        let _ = instance.setattr("column", fault.column);
        let _ = instance.setattr("limit", fault.limit.unwrap_or_default());
        err
    })
}

/// Python value -> Rhai value. Only the shapes a rule context is made of; anything else is refused by
/// name rather than silently stringified. Size limits belong to the sandbox - see
/// `sandbox::Program::evaluate`, which every path into a rule goes through.
fn dict_to_dynamic(context: &Bound<'_, PyDict>, limits: Limits) -> Result<Dynamic, Fault> {
    let mut map = Map::new();
    for (key, value) in context.iter() {
        let key: String = key.extract().map_err(|_| key_fault("<not a string>"))?;
        if !sandbox::is_identifier(&key) {
            return Err(key_fault(&key));
        }
        map.insert(key.into(), value_to_dynamic(&value, limits, 0)?);
    }
    Ok(Dynamic::from_map(map))
}

fn value_to_dynamic(
    value: &Bound<'_, PyAny>,
    limits: Limits,
    depth: usize,
) -> Result<Dynamic, Fault> {
    if value.is_none() {
        return Ok(Dynamic::UNIT);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Dynamic::from(
            value.extract::<bool>().map_err(|_| bad_type(value))?,
        ));
    }
    if value.is_instance_of::<PyInt>() {
        return Ok(match value.extract::<i64>() {
            Ok(number) => Dynamic::from(number),
            // An integer too big for i64 stays usable as a float rather than failing a turn over a
            // message id nobody will ever do arithmetic with.
            Err(_) => Dynamic::from(value.extract::<f64>().map_err(|_| bad_type(value))?),
        });
    }
    if value.is_instance_of::<PyFloat>() {
        return Ok(Dynamic::from(
            value.extract::<f64>().map_err(|_| bad_type(value))?,
        ));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Dynamic::from(
            value.extract::<String>().map_err(|_| bad_type(value))?,
        ));
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        let items = value.try_iter().map_err(|_| bad_type(value))?;
        let mut array = Array::new();
        for item in items {
            let item = item.map_err(|_| bad_type(value))?;
            array.push(value_to_dynamic(&item, limits, depth + 1)?);
        }
        return Ok(Dynamic::from_array(array));
    }
    if let Ok(mapping) = value.cast::<PyDict>() {
        if depth >= limits.max_context_depth {
            return Err(too_big(
                format!(
                    "the context nests deeper than the {} level limit",
                    limits.max_context_depth
                ),
                limits.max_context_depth,
            ));
        }
        let mut map = Map::new();
        for (key, item) in mapping.iter() {
            let key: String = key.extract().map_err(|_| key_fault("<not a string>"))?;
            if !sandbox::is_identifier(&key) {
                return Err(key_fault(&key));
            }
            map.insert(key.into(), value_to_dynamic(&item, limits, depth + 1)?);
        }
        return Ok(Dynamic::from_map(map));
    }
    Err(bad_type(value))
}

fn bad_type(value: &Bound<'_, PyAny>) -> Fault {
    let name = if value.is_instance_of::<PyBytes>() {
        "bytes".to_string()
    } else {
        value
            .get_type()
            .name()
            .map(|name| name.to_string())
            .unwrap_or_else(|_| "this type".to_string())
    };
    Fault {
        compile: false,
        message: format!(
            "a rule context may hold only text, numbers, booleans, lists and maps, not {name}"
        ),
        line: 0,
        column: 0,
        limit: Some("wrong_type".to_string()),
    }
}

fn too_big(message: String, limit: usize) -> Fault {
    Fault {
        compile: false,
        message,
        line: 0,
        column: 0,
        limit: Some(format!("size:{limit}")),
    }
}

/// Rhai value -> Python value. Deliberately total: whatever a rule answered, the caller gets
/// something it can show rather than an exception about a type it has never heard of.
fn dynamic_to<'py>(py: Python<'py>, value: Dynamic) -> PyResult<Bound<'py, PyAny>> {
    if value.is_unit() {
        return Ok(py.None().into_bound(py));
    }
    if value.is_bool() {
        let flag = value
            .as_bool()
            .map_err(|_| PyValueError::new_err("not a boolean"))?;
        return Ok(flag.into_pyobject(py)?.to_owned().into_any());
    }
    if value.is_int() {
        let number = value
            .as_int()
            .map_err(|_| PyValueError::new_err("not an integer"))?;
        return Ok(number.into_pyobject(py)?.into_any());
    }
    if value.is_float() {
        let number = value
            .as_float()
            .map_err(|_| PyValueError::new_err("not a float"))?;
        return Ok(number.into_pyobject(py)?.into_any());
    }
    if value.is_string() {
        let text = value.into_string().unwrap_or_default();
        return Ok(PyString::new(py, &text).into_any());
    }
    if value.is_array() {
        let list = PyList::empty(py);
        for item in value.into_array().unwrap_or_else(|_| Array::new()) {
            list.append(dynamic_to(py, item)?)?;
        }
        return Ok(list.into_any());
    }
    if value.is_map() {
        let dict = PyDict::new(py);
        let mut value = value;
        if let Ok(mut held) = value.as_map_mut() {
            for (key, item) in std::mem::take(&mut *held) {
                dict.set_item(key.to_string(), dynamic_to(py, item)?)?;
            }
        }
        return Ok(dict.into_any());
    }
    Err(PyValueError::new_err(
        "this rule returned a value Python has no name for",
    ))
}
