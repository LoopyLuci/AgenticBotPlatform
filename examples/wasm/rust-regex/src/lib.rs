//! A plugin that pulls things out of text, which is the kind of work ABP keeps paying
//! Python processes for: an email address in a log line, every URL in a page, the IPv4
//! addresses in a firewall dump. As a component it starts in microseconds and runs under a
//! deadline and a memory cap, so a 40 MB log can be scanned without a 30 MB interpreter and
//! a hundred milliseconds of start-up per call.
//!
//! No regex crate. The five patterns below are each a few lines of scanning, and pulling in a
//! regex engine for them would add more to the component than the rest of it put together.
//!
//! Build it with:
//!
//! ```text
//! cargo build --target wasm32-wasip2 --release
//! ```

use abp_wasm_sdk::{plugin, Manifest, ManifestBuilder, Plugin, Tool, ToolBuilder};

const EXTRACT_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {
    "text": {"type": "string", "description": "the text to scan"},
    "pattern": {
      "type": "string",
      "enum": ["email", "url", "ipv4", "tag", "quoted"],
      "description": "what to pull out of it"
    },
    "unique": {"type": "boolean", "description": "drop duplicates, keeping first-seen order"}
  },
  "required": ["text", "pattern"]
}"#;

const COUNT_SCHEMA: &str = r#"{
  "type": "object",
  "properties": {
    "text": {"type": "string"},
    "pattern": {
      "type": "string",
      "enum": ["email", "url", "ipv4", "tag", "quoted"]
    }
  },
  "required": ["text", "pattern"]
}"#;

struct Extractor;

impl Plugin for Extractor {
    fn describe() -> Manifest {
        Manifest::new(
            "regex",
            "1.0.0",
            "Pulls emails, URLs, IPv4 addresses, #tags and quoted strings out of text.",
        )
        .with_tool(Tool::new(
            "extract",
            "Find every occurrence of a pattern in some text and return them as a JSON array",
            EXTRACT_SCHEMA,
        ))
        .with_tool(Tool::new(
            "count",
            "Count the occurrences of a pattern in some text, without returning them",
            COUNT_SCHEMA,
        ))
    }

    fn invoke(tool: &str, args_json: &str) -> Result<String, String> {
        let args = Arguments::parse(args_json)?;
        match tool {
            "extract" => Ok(extract(&args)),
            "count" => Ok(count(&args)),
            other => Err(format!(
                "this plugin has two tools, extract and count; {other:?} is not one of them"
            )),
        }
    }
}

/// Both tools take the same two required fields, so they are parsed once.
struct Arguments {
    text: String,
    pattern: Pattern,
    unique: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Pattern {
    Email,
    Url,
    Ipv4,
    Tag,
    Quoted,
}

impl Arguments {
    fn parse(args_json: &str) -> Result<Arguments, String> {
        let text = json_string(args_json, "text")
            .ok_or_else(|| "both tools need a \"text\" string".to_string())?;
        let named = json_string(args_json, "pattern").ok_or_else(|| {
            "both tools need a \"pattern\": email, url, ipv4, tag or quoted".to_string()
        })?;
        let pattern = match named.as_str() {
            "email" => Pattern::Email,
            "url" => Pattern::Url,
            "ipv4" => Pattern::Ipv4,
            "tag" => Pattern::Tag,
            "quoted" => Pattern::Quoted,
            other => {
                return Err(format!(
                "{other:?} is not a pattern this plugin knows; use email, url, ipv4, tag or quoted"
            ))
            }
        };
        Ok(Arguments {
            text,
            pattern,
            unique: json_bool(args_json, "unique"),
        })
    }
}

/// Every match, in order, optionally with duplicates dropped.
fn extract(args: &Arguments) -> String {
    let found = scan(&args.text, args.pattern);
    let mut out: Vec<String> = Vec::new();
    for value in found {
        if args.unique && out.contains(&value) {
            continue;
        }
        out.push(value);
    }
    json_string_array("matches", &out)
}

fn count(args: &Arguments) -> String {
    let found = scan(&args.text, args.pattern);
    format!("{{\"count\": {}}}", found.len())
}

fn scan(text: &str, pattern: Pattern) -> Vec<String> {
    match pattern {
        Pattern::Email => emails(text),
        Pattern::Url => urls(text),
        Pattern::Ipv4 => ipv4(text),
        Pattern::Tag => tags(text),
        Pattern::Quoted => quoted(text),
    }
}

// ---- the patterns ---------------------------------------------------------------------

/// `local@domain.tld`: the local part is anything without a space or an `@`, the domain is
/// at least two dot-separated labels with a letter in the last one.
fn emails(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for token in
        text.split(|c: char| c.is_whitespace() || c == ',' || c == ';' || c == '<' || c == '>')
    {
        let token = token.trim_matches(|c: char| matches!(c, '"' | '\'' | '(' | ')' | '[' | ']'));
        let Some((local, domain)) = token.split_once('@') else {
            continue;
        };
        if local.is_empty() || local.contains('@') || !is_domain(domain) {
            continue;
        }
        out.push(token.to_string());
    }
    out
}

fn is_domain(domain: &str) -> bool {
    let labels: Vec<&str> = domain.split('.').collect();
    labels.len() >= 2
        && labels
            .iter()
            .all(|l| !l.is_empty() && l.chars().all(|c| c.is_alphanumeric() || c == '-'))
        && labels
            .last()
            .is_some_and(|tld| tld.chars().any(|c| c.is_alphabetic()))
}

/// `scheme://...`: a scheme, `://`, and then everything up to whitespace or a closing
/// bracket or quote.
fn urls(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for token in text.split_whitespace() {
        let token = token
            .trim_matches(|c: char| matches!(c, '"' | '\'' | '(' | ')' | '[' | ']' | ',' | '.'));
        if let Some(rest) = token
            .strip_prefix("https://")
            .or_else(|| token.strip_prefix("http://"))
        {
            if !rest.is_empty() && rest.contains('.') {
                out.push(token.to_string());
            }
        }
    }
    out
}

/// Four dot-separated numbers, each 0-255, with no leading zeros beyond a single digit.
fn ipv4(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for token in text.split(|c: char| !c.is_ascii_digit() && c != '.') {
        if is_ipv4(token) {
            out.push(token.to_string());
        }
    }
    out
}

fn is_ipv4(token: &str) -> bool {
    let parts: Vec<&str> = token.split('.').collect();
    parts.len() == 4
        && parts.iter().all(|p| {
            !p.is_empty()
                && p.len() <= 3
                && p.chars().all(|c| c.is_ascii_digit())
                && p.parse::<u16>().is_ok_and(|n| n <= 255)
                && (p.len() == 1 || !p.starts_with('0'))
        })
}

/// `#tag`: a `#` followed by letters, digits and underscores, at least one letter.
fn tags(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let bytes: Vec<char> = text.chars().collect();
    for (i, c) in bytes.iter().enumerate() {
        if *c != '#' {
            continue;
        }
        let mut end = i + 1;
        while end < bytes.len() && (bytes[end].is_alphanumeric() || bytes[end] == '_') {
            end += 1;
        }
        let tag: String = bytes[i + 1..end].iter().collect();
        if tag.chars().any(|c| c.is_alphabetic()) {
            out.push(format!("#{tag}"));
        }
    }
    out
}

/// Anything between a matching pair of single or double quotes.
fn quoted(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let quote = chars[i];
        if quote != '"' && quote != '\'' {
            i += 1;
            continue;
        }
        let start = i + 1;
        let mut j = start;
        while j < chars.len() && chars[j] != quote {
            j += 1;
        }
        if j < chars.len() && j > start {
            out.push(chars[start..j].iter().collect());
        }
        i = j + 1;
    }
    out
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
            '\\' => out.push(match chars.next()? {
                'n' => '\n',
                't' => '\t',
                'r' => '\r',
                other => other,
            }),
            other => out.push(other),
        }
    }
    None
}

fn json_bool(text: &str, field: &str) -> bool {
    let needle = format!("\"{field}\"");
    let Some(at) = text.find(&needle) else {
        return false;
    };
    let after = text[at + needle.len()..].trim_start();
    match after.strip_prefix(':') {
        Some(rest) => rest.trim_start().starts_with("true"),
        None => false,
    }
}

fn json_string_array(key: &str, values: &[String]) -> String {
    let items: Vec<String> = values
        .iter()
        .map(|v| format!("\"{}\"", v.replace('\\', "\\\\").replace('"', "\\\"")))
        .collect();
    format!("{{\"{key}\": [{}]}}", items.join(", "))
}

plugin!(Extractor);
