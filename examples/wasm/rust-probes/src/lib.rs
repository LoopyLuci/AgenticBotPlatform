//! A plugin whose whole purpose is to be stopped: it is what the sandbox tests run.
//!
//! Every tool here does one thing that only works if a limit is in force, or one thing that
//! only works if a capability was granted:
//!
//! * `spin` loops forever. ABP's deadline has to interrupt it.
//! * `grow` allocates without end until the memory cap traps it.
//! * `flood` returns a very large result, so the output cap has to refuse it.
//! * `kv` writes and reads the plugin's *own* key-value store, which is how the tests show
//!   two plugins cannot see each other's keys.
//! * `read` reads a file from the directory its manifest asked for - and from anywhere
//!   else, which is how the tests show that a preopen is the only way in.
//! * `fetch` asks the host for a URL, allowed or not.
//!
//! It asks for `/data` and for `127.0.0.1` in its manifest, so a person approving everything
//! gets both and a person approving nothing gets neither; the tests install it both ways.
//!
//! Build it with:
//!
//! ```text
//! cargo build --target wasm32-wasip2 --release
//! ```

use abp_wasm_sdk::{plugin, Manifest, ManifestBuilder, Plugin, Tool, ToolBuilder};

const SPIN_SCHEMA: &str = r#"{"type": "object", "properties": {}}"#;
const GROW_SCHEMA: &str = r#"{"type": "object", "properties": {}}"#;
const FLOOD_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {"bytes": {"type": "integer", "description": "how big a result to return"}},
  "required": ["bytes"]
}"#;
const KV_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {
    "key": {"type": "string"},
    "value": {"type": "string", "description": "omit to only read the key"}
  },
  "required": ["key"]
}"#;
const READ_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {"path": {"type": "string", "description": "a path inside the preopened directory"}},
  "required": ["path"]
}"#;
const FETCH_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {"url": {"type": "string"}},
  "required": ["url"]
}"#;

struct Probes;

impl Plugin for Probes {
    fn describe() -> Manifest {
        Manifest::new(
            "probes",
            "1.0.0",
            "Exercises the sandbox: deadlines, memory caps, its own store, a preopened directory and the network.",
        )
        .with_tool(Tool::new("spin", "Loop forever, until the deadline stops it", SPIN_SCHEMA))
        .with_tool(Tool::new("grow", "Allocate without end, until the memory cap stops it", GROW_SCHEMA))
        .with_tool(Tool::new("flood", "Return a result far larger than the output cap", FLOOD_SCHEMA))
        .with_tool(Tool::new("kv", "Write to or read from this plugin's own key-value store", KV_SCHEMA))
        .with_tool(Tool::new("read", "Read a file from the preopened directory", READ_SCHEMA))
        .with_tool(Tool::new("fetch", "Ask the host to fetch a URL", FETCH_SCHEMA))
        .with_preopen("/data")
        .with_network("127.0.0.1")
    }

    fn invoke(tool: &str, args_json: &str) -> Result<String, String> {
        match tool {
            "spin" => spin(),
            "grow" => grow(),
            "flood" => flood(args_json),
            "kv" => kv(args_json),
            "read" => read(args_json),
            "fetch" => fetch(args_json),
            other => Err(format!("no tool named {other:?}")),
        }
    }
}

/// Never returns on its own. The only way this call ends is the epoch deadline interrupting
/// it, which is the whole point: a guest cannot be trusted to stop itself.
///
/// The `black_box` is not decoration. Without it the optimiser is entitled to notice that a
/// `wrapping_add` loop has no exit, conclude the exit is unreachable, and delete the loop -
/// which is exactly what it did the first time this was written.
fn spin() -> Result<String, String> {
    let mut total: u64 = 0;
    loop {
        total = std::hint::black_box(total).wrapping_add(1);
        total = std::hint::black_box(total);
    }
}

/// Keeps allocating until the store's memory limiter traps the growth. The blocks are kept
/// alive so the allocator cannot reuse what has already been taken, and the size goes
/// through `black_box` so the loop cannot be reasoned away either.
fn grow() -> Result<String, String> {
    let mut blocks: Vec<Vec<u8>> = Vec::new();
    let mut size = 1024 * 1024usize;
    loop {
        size = std::hint::black_box(size);
        blocks.push(vec![0u8; size]);
    }
}

/// Returns `bytes` of text, so a caller can push at the output cap. The cap is the host's;
/// this is only the thing that runs into it.
fn flood(args_json: &str) -> Result<String, String> {
    let want = json_int(args_json, "bytes").unwrap_or(1024).max(0) as usize;
    let text = "x".repeat(want);
    Ok(format!("{{\"text\": \"{text}\"}}"))
}

/// This plugin's own store: write a value, or read it back.
fn kv(args_json: &str) -> Result<String, String> {
    let key =
        json_string(args_json, "key").ok_or_else(|| "kv needs a \"key\" string".to_string())?;
    match json_string(args_json, "value") {
        Some(value) => {
            abp_wasm_sdk::store_set(&key, &value);
            Ok(format!("{{\"key\": \"{key}\", \"written\": true}}"))
        }
        None => {
            let keys = abp_wasm_sdk::store_keys();
            let found = abp_wasm_sdk::store_get(&key).unwrap_or_default();
            Ok(format!(
                "{{\"key\": \"{key}\", \"value\": \"{}\", \"present\": {}, \"keys\": {}}}",
                found.replace('\\', "\\\\").replace('"', "\\\""),
                abp_wasm_sdk::store_get(&key).is_some(),
                keys.len()
            ))
        }
    }
}

/// Read a file from the preopened directory. The path is the guest path (`/data/...`), and
/// it is the *only* way in: with nothing preopened at `/data`, this fails, and a path that
/// climbs out with `..` fails as well, because the guest's resolver only ever starts from a
/// preopen.
fn read(args_json: &str) -> Result<String, String> {
    let path =
        json_string(args_json, "path").ok_or_else(|| "read needs a \"path\" string".to_string())?;
    let text = std::fs::read_to_string(&path).map_err(|e| format!("could not read {path}: {e}"))?;
    Ok(format!(
        "{{\"path\": \"{}\", \"bytes\": {}}}",
        path.replace('\\', "\\\\").replace('"', "\\\""),
        text.len()
    ))
}

/// Ask the host for a URL. A host the plugin was not granted refuses before a request is
/// made, and the refusal comes back as an error the plugin can report.
fn fetch(args_json: &str) -> Result<String, String> {
    let url =
        json_string(args_json, "url").ok_or_else(|| "fetch needs a \"url\" string".to_string())?;
    let response = abp_wasm_sdk::http_fetch("GET", &url, &[], None)
        .map_err(|why| format!("fetch refused: {why}"))?;
    Ok(format!(
        "{{\"status\": {}, \"bytes\": {}}}",
        response.status,
        response.body.len()
    ))
}

// ---- JSON, without a JSON library --------------------------------------------------------

fn json_string(text: &str, field: &str) -> Option<String> {
    let needle = format!("\"{field}\"");
    let after_key = &text[text.find(&needle)? + needle.len()..];
    let after_colon = after_key[after_key.find(':')? + 1..].trim_start();
    let value = after_colon.strip_prefix('"')?;
    let mut out = String::new();
    let mut chars = value.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => out.push(chars.next()?),
            other => out.push(other),
        }
    }
    None
}

fn json_int(text: &str, field: &str) -> Option<i64> {
    let needle = format!("\"{field}\"");
    let after_key = &text[text.find(&needle)? + needle.len()..];
    let after_colon = after_key[after_key.find(':')? + 1..].trim_start();
    let digits: String = after_colon
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    digits.parse().ok()
}

plugin!(Probes);
