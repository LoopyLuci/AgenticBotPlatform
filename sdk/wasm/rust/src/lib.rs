//! The guest half of ABP's WebAssembly plugin SDK.
//!
//! A plugin is a WebAssembly component that exports `abp:plugin/guest@0.1.0`: `describe`
//! once, `invoke` per tool call. This crate is the Rust way to write one - it holds the
//! bindings to the world in `wit/`, the [`Plugin`] trait a plugin implements, and the
//! `plugin!` macro that turns an implementation into a component.
//!
//! ```ignore
//! use abp_wasm_sdk::{plugin, Manifest, ManifestBuilder, Plugin, Tool, ToolBuilder};
//!
//! struct Greeter;
//!
//! impl Plugin for Greeter {
//!     fn describe() -> Manifest {
//!         Manifest::new("greeter", "1.0.0", "Says hello")
//!             .with_tool(Tool::new("greet", "Say hello", SCHEMA))
//!     }
//!
//!     fn invoke(tool: &str, args_json: &str) -> Result<String, String> {
//!         match tool {
//!             "greet" => Ok(format!("{{\"greeting\":\"hello\"}}")),
//!             other => Err(format!("no tool named {other}")),
//!         }
//!     }
//! }
//!
//! plugin!(Greeter);
//! ```
//!
//! Then `cargo build --target wasm32-wasip2 --release` produces the component.
//!
//! **What a plugin may do is not up to it.** The guest can ask for preopened
//! directories and for network hosts, and ABP shows both to a person at install time;
//! what it actually gets is what they approved. Requesting more than you need is how a
//! plugin gets turned down, so ask for the least that works.

use std::collections::BTreeMap;

// The generated bindings refer to the bindings runtime by this crate's name, so that a
// plugin needs the SDK and not wit-bindgen as well. Inside the SDK itself that name is
// only available if the crate introduces it under its own name.
// The generated bindings reach the bindings runtime through this crate, so a plugin
// depends on the SDK and not on wit-bindgen as well. generate! is expanded here and
// nowhere else, so a crate-relative path is the right one.
pub use wit_bindgen;

// One copy of the world, at the repository root, shared with the host runtime: a second
// copy here is a second thing to keep in step. The WASI p2 bindings are generated rather
// than taken from the `wasip2` crate; they resolve to the same `wasi:*` import names
// either way, and generating them keeps the SDK the only crate a plugin depends on.
wit_bindgen::generate!({
    path: "../../../wit",
    world: "plugin",
    // The generated code refers to the bindings runtime through this crate, so a plugin
    // depends on the SDK and not on wit-bindgen as well.
    runtime_path: "crate::wit_bindgen::rt",
    // WASI's flag types are generated through `bitflags`, which wit-bindgen re-exports
    // from its runtime module. Named explicitly because the default assumes the
    // generated code is in a crate that depends on wit-bindgen directly, and this is the
    // crate that depends on it for the plugin.
    bitflags_path: "crate::wit_bindgen::rt::bitflags",
    pub_export_macro: true,
    generate_all,
});

/// One tool the agent can call, and what the plugin says about itself. These are the
/// world's own types, re-exported: a plugin builds them and the host lifts them out of
/// the component, so there is exactly one definition of what a manifest is.
pub use abp::plugin::manifest::{Manifest, Tool};

/// The builders a plugin writes its manifest with. On the world's own types rather than
/// on a copy, because the copy would be a second thing to convert.
// `new` returning a type other than `Self` is the point here: these are constructors for the
// world's `Manifest` and `Tool`, which bindgen generates and this crate cannot add inherent
// methods to.
#[allow(clippy::new_ret_no_self)]
pub trait ManifestBuilder {
    /// A manifest with no tools yet. Add them with [`ManifestBuilder::with_tool`].
    fn new(name: &str, version: &str, description: &str) -> Manifest;
    fn with_tool(self, tool: Tool) -> Manifest;
    /// Ask for a host directory, by the path the guest will see it at. ABP shows the
    /// request at install time and grants it only if a person says yes.
    fn with_preopen(self, guest_path: &str) -> Manifest;
    /// Ask to reach one host over `http-fetch`, as `host` or `host:port`. Anything not
    /// approved is refused before a request is made.
    fn with_network(self, host: &str) -> Manifest;
}

impl ManifestBuilder for Manifest {
    fn new(name: &str, version: &str, description: &str) -> Manifest {
        Manifest {
            name: name.to_string(),
            version: version.to_string(),
            description: description.to_string(),
            tools: Vec::new(),
            preopens: Vec::new(),
            network: Vec::new(),
        }
    }

    fn with_tool(mut self, tool: Tool) -> Manifest {
        self.tools.push(tool);
        self
    }

    fn with_preopen(mut self, guest_path: &str) -> Manifest {
        self.preopens.push(guest_path.to_string());
        self
    }

    fn with_network(mut self, host: &str) -> Manifest {
        self.network.push(host.to_string());
        self
    }
}

/// The same for a tool, whose fields are three strings.
#[allow(clippy::new_ret_no_self)]
pub trait ToolBuilder {
    fn new(name: &str, description: &str, schema: &str) -> Tool;
}

impl ToolBuilder for Tool {
    fn new(name: &str, description: &str, schema: &str) -> Tool {
        Tool {
            name: name.to_string(),
            description: description.to_string(),
            schema: schema.to_string(),
        }
    }
}

/// What a plugin implements. Both methods are static because a component instance is
/// reached through exported symbols rather than through a value the host holds.
///
/// `describe` is called once, at install time and at start-up, before anything is
/// approved. `invoke` is called once per tool call, under a deadline and a memory cap,
/// so it must return rather than wait.
pub trait Plugin {
    /// Who this plugin is and what it can do. Keep it cheap: ABP calls it before
    /// deciding whether to show it to anyone.
    fn describe() -> Manifest;

    /// Run one tool. `args_json` is the arguments object as JSON, matching the schema
    /// `describe` published. `Ok` is the result as JSON; `Err` is a message the caller
    /// will see, so it should say what to do differently rather than what went wrong
    /// internally.
    fn invoke(tool: &str, args_json: &str) -> Result<String, String>;
}

/// The host services a plugin can call: its log, its own key-value store, `http-fetch`
/// (checked against the approved network list), the clock and randomness. ABP
/// implements all of them; a plugin only calls them.
pub fn log(level: &str, msg: &str) {
    abp::plugin::host::log(level, msg)
}

/// Read a value from this plugin's own store. `None` when the key is not set. Another
/// plugin's keys are invisible here, and the host's own state is not in this store.
pub fn store_get(key: &str) -> Option<String> {
    abp::plugin::host::store_get(key)
}

/// Write a value into this plugin's own store. There is nothing to handle: a store the
/// host cannot write to takes the whole call down with it, which is what a store that
/// does not work should do.
pub fn store_set(key: &str, value: &str) {
    abp::plugin::host::store_set(key, value)
}

/// Remove a key from this plugin's own store.
pub fn store_delete(key: &str) {
    abp::plugin::host::store_delete(key)
}

/// Every key in this plugin's own store, sorted.
pub fn store_keys() -> Vec<String> {
    abp::plugin::host::store_keys()
}

/// The answer `http_fetch` gives back: the status, the headers and the body.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: String,
}

/// Make an HTTP request through the host, which checks the URL against this plugin's
/// approved network list first. `Err` is either a refusal or a failed request; both say
/// so in the message.
pub fn http_fetch(
    method: &str,
    url: &str,
    headers: &[(&str, &str)],
    body: Option<&str>,
) -> Result<HttpResponse, String> {
    let headers: Vec<(String, String)> = headers
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    match abp::plugin::host::http_fetch(method, url, &headers, body) {
        Ok(response) => Ok(HttpResponse {
            status: response.status,
            headers: response.headers.into_iter().collect(),
            body: response.body,
        }),
        Err(why) => Err(why),
    }
}

/// Milliseconds since the Unix epoch.
pub fn now_ms() -> u64 {
    abp::plugin::host::now_ms()
}

/// Cryptographically strong random bytes.
pub fn random_bytes(len: u32) -> Vec<u8> {
    abp::plugin::host::random_bytes(len)
}

/// Turn an implementation into a component: implements the world's export trait for the
/// given [`Plugin`] and emits the export symbols a component is made of.
///
/// ```ignore
/// struct MyPlugin;
/// impl Plugin for MyPlugin { /* ... */ }
/// plugin!(MyPlugin);
/// ```
#[macro_export]
macro_rules! plugin {
    ($plugin:ty) => {
        /// The component's entry points, delegating to the plugin implementation.
        struct __AbpPluginGuest;

        impl $crate::exports::abp::plugin::guest::Guest for __AbpPluginGuest {
            fn describe() -> $crate::Manifest {
                <$plugin as $crate::Plugin>::describe()
            }

            fn invoke(tool: String, args_json: String) -> Result<String, String> {
                <$plugin as $crate::Plugin>::invoke(&tool, &args_json)
            }
        }

        $crate::export!(__AbpPluginGuest with_types_in $crate);
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Example;

    impl Plugin for Example {
        fn describe() -> Manifest {
            Manifest::new("example", "1.0.0", "an example")
                .with_tool(Tool::new("echo", "Echo the text", r#"{"type":"string"}"#))
                .with_network("example.com")
                .with_preopen("/data")
        }

        fn invoke(tool: &str, args_json: &str) -> Result<String, String> {
            match tool {
                "echo" => Ok(args_json.to_string()),
                other => Err(format!("no tool named {other}")),
            }
        }
    }

    #[test]
    fn a_manifest_carries_what_the_plugin_asked_for() {
        let manifest = Example::describe();
        assert_eq!(manifest.name, "example");
        assert_eq!(manifest.tools[0].name, "echo");
        assert_eq!(manifest.tools[0].schema, r#"{"type":"string"}"#);
        assert_eq!(manifest.preopens, vec!["/data".to_string()]);
        assert_eq!(manifest.network, vec!["example.com".to_string()]);
    }

    #[test]
    fn invoke_reaches_the_plugin_and_an_unknown_tool_is_an_error() {
        assert_eq!(Example::invoke("echo", "\"hi\"").unwrap(), "\"hi\"");
        assert!(Example::invoke("nope", "{}").is_err());
    }
}
