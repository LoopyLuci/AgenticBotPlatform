//! ABP's Rust fast paths.
//!
//! Every function here has a pure-Python twin in `abp_native/_py.py` that the Python
//! package falls back to when this extension is missing (or when `ABP_NATIVE=0`), and
//! `tests/test_native_parity.py` proves the two agree on random inputs. Nothing here
//! talks to the filesystem, the network or the clock: what moved to Rust was measured
//! to be CPU-bound string work (docs/perf/hotspots.md), and that is all this crate does.

use pyo3::prelude::*;

mod memdup;
mod redact;
mod vectors;

/// Python's character classes, which are not the ones Rust's `char` methods implement.
///
/// `str.isalnum()` is "general category Letter or Number" and `\w` is that plus the
/// underscore - neither is `char::is_alphanumeric`, which is built on the Alphabetic
/// property and so includes combining marks Python does not count. `\p{L}` and `\p{N}`
/// are the general categories, so these are exactly Python's rules:
/// ALNUM_RUN is `[\w']+`, ALNUM is `isalnum`, WORD is `\w`.
static ALNUM_RUN: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
static NOT_ALNUM: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
static WORD_ONE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();

fn regex_cell(
    cell: &'static std::sync::OnceLock<regex::Regex>,
    pattern: &str,
) -> &'static regex::Regex {
    cell.get_or_init(|| regex::Regex::new(pattern).expect("pattern compiles"))
}

/// Every run of word characters and apostrophes: what Python's `re.findall(r"[\w']+")` returns.
pub(crate) fn word_runs() -> &'static regex::Regex {
    regex_cell(&ALNUM_RUN, r"[\p{L}\p{N}_']+")
}

/// Everything `str.isalnum()` says no to.
pub(crate) fn not_alnum() -> &'static regex::Regex {
    regex_cell(&NOT_ALNUM, r"[^\p{L}\p{N}]")
}

/// One word character, anchored: what `\w` and `\b` mean to Python.
pub(crate) fn word_one() -> &'static regex::Regex {
    regex_cell(&WORD_ONE, r"\A[\p{L}\p{N}_]\z")
}

/// What Python's `re` treats as a word character in `\w` and `\b`.
pub(crate) fn is_word_char(ch: char) -> bool {
    if ch.is_ascii() {
        return ch == '_' || ch.is_ascii_alphanumeric();
    }
    let mut buf = [0u8; 4];
    word_one().is_match(ch.encode_utf8(&mut buf))
}

/// The pure-Python twin of `abp_native.hash_vectors`.
///
/// `texts[i]` becomes `DIM` little-endian float32 values: the signed feature hashing of
/// hashed TF-IDF (words, word pairs and character trigrams) that
/// `bot/fileserver/index.py` stores per file, per memory and per knowledge chunk.
///
/// Returns a list of `bytes`, one `DIM * 4` blob per input text.
#[pyfunction]
#[pyo3(signature = (texts))]
fn hash_vectors(texts: Vec<String>) -> PyResult<Vec<Vec<u8>>> {
    Ok(vectors::hash_vectors(&texts))
}

/// The dimension of every vector [`hash_vectors`] returns.
#[pyfunction]
fn vector_dim() -> usize {
    vectors::DIM
}

/// `words` of `text` lowercased, as `abp_native._normalise_words` splits them: apostrophes
/// dropped, everything that is not alphanumeric a space, runs of spaces collapsed.
#[pyfunction]
fn normalise_words(text: &str) -> Vec<String> {
    memdup::normalise_words(text)
}

/// The index of the first row of `rows` that says the same thing as `target`, or -1.
///
/// `rows` is `(status, content)` in the order the caller read them. A row counts as the
/// same memory when its normalised text is identical, or when the two share at least
/// `overlap` of their words and their digit-bearing tokens match exactly. `bot/memory.py`
/// calls this once per memory written instead of comparing every pair in Python.
#[pyfunction]
#[pyo3(signature = (target, rows, overlap))]
fn memory_duplicate(target: &str, rows: Vec<(String, String)>, overlap: f64) -> i64 {
    memdup::memory_duplicate(target, &rows, overlap)
}

/// Does this environment variable's name look like a credential? (`secrets_guard._NAME`.)
///
/// One name at a time, because that is how `known_secrets` asks: it filters the
/// environment a variable at a time and callers replace this predicate (a test does), so
/// the loop has to keep seeing a Python-level call. The twin is `_py.is_secret_name`.
#[pyfunction]
fn is_secret_name(name: &str) -> bool {
    redact::is_secret_name(name)
}

/// `text` with anything that looks like a credential replaced: the eight shapes in
/// `bot/agent_runtime/session_export._SHAPES`.
#[pyfunction]
fn scrub_credentials(text: &str) -> String {
    redact::scrub_credentials(text)
}

/// `text` with e-mail addresses, IPv4 addresses and phone numbers masked, the three
/// patterns in `bot/agent_runtime/trajectory.scrub_pii`.
#[pyfunction]
fn scrub_pii(text: &str) -> String {
    redact::scrub_pii(text)
}

/// What this build is: the version, and the functions it provides. Printed by
/// `python -m abp_native`.
#[pyfunction]
fn build_info() -> Vec<String> {
    vec![
        env!("CARGO_PKG_VERSION").to_string(),
        concat!("pyo3 ", "abi3").to_string(),
        "hash_vectors".to_string(),
        "memory_duplicate".to_string(),
        "is_secret_name".to_string(),
        "scrub_credentials".to_string(),
        "scrub_pii".to_string(),
    ]
}

/// The compiled extension module. Named `abp_native_core` because `abp_native` is the
/// Python package that loads it; see `pyproject.toml`.
#[pymodule]
fn abp_native_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hash_vectors, m)?)?;
    m.add_function(wrap_pyfunction!(vector_dim, m)?)?;
    m.add_function(wrap_pyfunction!(normalise_words, m)?)?;
    m.add_function(wrap_pyfunction!(memory_duplicate, m)?)?;
    m.add_function(wrap_pyfunction!(is_secret_name, m)?)?;
    m.add_function(wrap_pyfunction!(scrub_credentials, m)?)?;
    m.add_function(wrap_pyfunction!(scrub_pii, m)?)?;
    m.add_function(wrap_pyfunction!(build_info, m)?)?;
    Ok(())
}
