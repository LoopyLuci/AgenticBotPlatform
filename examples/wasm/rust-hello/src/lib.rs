//! The smallest useful ABP plugin: one tool, no capabilities.
//!
//! Build it with
//!
//! ```text
//! cargo build --target wasm32-wasip2 --release
//! ```
//!
//! and install the resulting `target/wasm32-wasip2/release/abp_wasm_example_hello.wasm`
//! with `abp_wasm_plugins.install(...)`. ABP reads `describe()` at install time and shows
//! what the plugin can do before anything is approved.
//!
//! Note what is *not* here: no `with_preopen`, no `with_network`. A plugin that asks for
//! nothing gets nothing, which is the point - the capabilities a plugin has are the ones
//! a person granted it, and asking for less is how a plugin gets installed without a
//! conversation.

use abp_wasm_sdk::{plugin, Manifest, ManifestBuilder, Plugin, Tool, ToolBuilder};

/// The schema for `greet`'s arguments. ABP hands it to the model verbatim, so it is a real
/// JSON Schema: one required string, and one optional.
const GREET_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {
    "name": {"type": "string", "description": "who to greet"},
    "greeting": {"type": "string", "description": "the greeting to use; defaults to Hello"}
  },
  "required": ["name"]
}"#;

struct Hello;

impl Plugin for Hello {
    fn describe() -> Manifest {
        Manifest::new(
            "hello",
            "1.0.0",
            "Greets someone. The smallest example that does something.",
        )
        .with_tool(Tool::new(
            "greet",
            "Greet someone by name, optionally with a greeting of your choosing",
            GREET_SCHEMA,
        ))
    }

    fn invoke(tool: &str, args_json: &str) -> Result<String, String> {
        match tool {
            "greet" => greet(args_json),
            other => Err(format!(
                "this plugin has one tool, greet; {other:?} is not one of them"
            )),
        }
    }
}

fn greet(args_json: &str) -> Result<String, String> {
    let args = Arguments::parse(args_json)?;
    abp_wasm_sdk::log("info", &format!("greeted {}", args.name));
    Ok(json_string_field(
        "greeting",
        &format!("{}, {}!", args.greeting, args.name),
    ))
}

/// `greet`'s arguments, read out of the JSON ABP passed. An argument the plugin's own
/// schema said was required, and which is not there, is the model's mistake to fix - so
/// the error says exactly which field and why.
struct Arguments {
    name: String,
    greeting: String,
}

impl Arguments {
    fn parse(args_json: &str) -> Result<Arguments, String> {
        let name = json_string(args_json, "name").ok_or_else(|| {
            "greet needs a \"name\" string, and the arguments it was called with had none"
                .to_string()
        })?;
        Ok(Arguments {
            name,
            greeting: json_string(args_json, "greeting").unwrap_or_else(|| "Hello".to_string()),
        })
    }
}

/// One string field's value, if the flat JSON object has one. A plugin gets the whole
/// arguments object as text and reads what it needs; a full JSON parser is for a plugin
/// whose entire job is JSON, and this one is not.
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

/// A one-field JSON object. Written out rather than pulled from a JSON library: a plugin
/// that depends on serde_json to return one field is heavier than the field.
fn json_string_field(key: &str, value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
    format!("{{\"{key}\": \"{escaped}\"}}")
}

plugin!(Hello);
