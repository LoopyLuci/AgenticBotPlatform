//! Redaction: the last thing between a transcript and a model, and between an export and
//! the world (`bot/agent_runtime/secrets_guard.py`, `session_export.py`, `trajectory.py`).
//!
//! py-spy found this the single largest CPU consumer under a scripted dashboard load
//! (docs/perf/hotspots.md): `known_secrets()` runs one regex per environment variable on
//! every `redact()` call, and `redact()` is called once per text block of every export.
//! Nothing here needs the environment or the vault - the caller passes the values - so all
//! of it is string work and all of it moved.

use regex::Regex;
use std::sync::OnceLock;

/// The names `is_secret_name` looks for, verbatim from `secrets_guard._NAME`.
const SECRET_NAME: &str = r"(?i)(token|secret|passw(or)?d|passwd|api[_-]?key|private[_-]?key|credential|auth(orization)?[_-]?key|access[_-]?key|session[_-]?key|signing[_-]?key)";

/// The eight credential shapes of `session_export._SHAPES`, in order: the shapes have to be
/// replaced before the secret values, or they would mangle the `[secret:NAME]` markers.
const SHAPES: [(&str, &str); 8] = [
    (
        r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "[private key removed]",
    ),
    (r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}", "[key removed]"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "[token removed]"),
    (r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", "[token removed]"),
    (r"\bAKIA[0-9A-Z]{16}\b", "[key removed]"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "[key removed]"),
    (
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}",
        "Bearer [token removed]",
    ),
    (
        r#"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\\?"?\s*[:=]\s*\\?"?)[^\s\\"',;]{6,}"#,
        "${1}${2}[removed]",
    ),
];

const EMAIL: &str = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}";
const IPV4: &str = r"\b(?:\d{1,3}\.){3}\d{1,3}\b";

/// `trajectory._PHONE` with the two assertions left out (the `regex` crate has none) and
/// the run length pinned, so `replace_phone` can walk 16, 15, ... 8 the way Python's engine
/// does. `^` anchors it to the start of the slice it is handed.
const PHONE_RUN: &str = r"^(\+?)\d[\d\s().-]{LENGTH}\d";
const PHONE_MIN_RUN: usize = 8;
const PHONE_MAX_RUN: usize = 16;

/// Compiled once, shared: a `Regex` is immutable and `regex` compiles are the expensive part.
static SECRET_NAME_RE: OnceLock<Regex> = OnceLock::new();
static SHAPE_RES: [OnceLock<Regex>; 8] = [
    OnceLock::new(),
    OnceLock::new(),
    OnceLock::new(),
    OnceLock::new(),
    OnceLock::new(),
    OnceLock::new(),
    OnceLock::new(),
    OnceLock::new(),
];
static EMAIL_RE: OnceLock<Regex> = OnceLock::new();
static IPV4_RE: OnceLock<Regex> = OnceLock::new();
static PHONE_LENS: OnceLock<Vec<Regex>> = OnceLock::new();

fn secret_name() -> &'static Regex {
    SECRET_NAME_RE.get_or_init(|| Regex::new(SECRET_NAME).expect("pattern compiles"))
}

fn shape_pattern(index: usize) -> &'static Regex {
    SHAPE_RES[index].get_or_init(|| Regex::new(SHAPES[index].0).expect("shape compiles"))
}

fn email() -> &'static Regex {
    EMAIL_RE.get_or_init(|| Regex::new(EMAIL).expect("pattern compiles"))
}

fn ipv4() -> &'static Regex {
    IPV4_RE.get_or_init(|| Regex::new(IPV4).expect("pattern compiles"))
}

/// One pattern per run length, longest first: Python's preference order.
fn phone_lens() -> &'static [Regex] {
    PHONE_LENS.get_or_init(|| {
        (PHONE_MIN_RUN..=PHONE_MAX_RUN)
            .rev()
            .map(|length| {
                Regex::new(&PHONE_RUN.replace("LENGTH", &length.to_string()))
                    .expect("phone run compiles")
            })
            .collect()
    })
}

/// `_PHONE.sub("[phone]", text)`.
///
/// The `regex` crate has no look-around, so the two assertions are decided here - and the
/// search has to walk the same decisions Python's backtracking engine walks, in the same
/// order: take the optional '+' (greedy), then as many of the 8..16 run characters as
/// possible, then the final digit, then check the assertion after it; on failure give back
/// one run character and try again, and only when every length has failed move on. Stopping
/// at the first match the regex crate offers would pick a different, longer span.
fn replace_phone(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut copied = 0;
    let mut at = 0;
    while at < text.len() {
        let here = text[at..].chars().next().expect("at a character boundary");
        let before_ok = at == 0 || {
            let previous = text[..at].chars().next_back().expect("a character");
            !crate::is_word_char(previous) && previous != '.'
        };
        if before_ok && (here == '+' || here.is_numeric()) {
            if let Some(end) = phone_match(text, at, here == '+') {
                out.push_str(&text[copied..at]);
                out.push_str("[phone]");
                at = end;
                copied = end;
                continue;
            }
        }
        at += here.len_utf8();
    }
    out.push_str(&text[copied..]);
    out
}

/// The end of the phone-shaped match at `at`, longest first, or None.
fn phone_match(text: &str, at: usize, at_sign: bool) -> Option<usize> {
    let slice = &text[at..];
    for take_sign in [true, false] {
        if take_sign != at_sign {
            continue; // without the '+' there is nothing for `\d` to match here
        }
        for pattern in phone_lens() {
            let Some(hit) = pattern.captures(slice) else {
                continue;
            };
            let whole = hit.get(0).expect("group 0");
            let end = at + whole.end();
            let after_ok = end == text.len() || {
                let next = text[end..].chars().next().expect("a character");
                !crate::is_word_char(next) && next != '.'
            };
            if after_ok {
                return Some(end);
            }
        }
    }
    None
}

/// `secrets_guard.is_secret_name`: does this name look like a credential?
pub fn is_secret_name(name: &str) -> bool {
    secret_name().is_match(name)
}

/// `text` with anything shaped like a credential replaced.
pub fn scrub_credentials(text: &str) -> String {
    let mut out = text.to_string();
    for (index, (_, replacement)) in SHAPES.iter().enumerate() {
        out = shape_pattern(index)
            .replace_all(&out, *replacement)
            .into_owned();
    }
    out
}

/// `text` with e-mail addresses, IPv4 addresses and phone numbers masked.
pub fn scrub_pii(text: &str) -> String {
    let after_email = email().replace_all(text, "[email]");
    let after_ip = ipv4().replace_all(&after_email, "[ip]");
    replace_phone(&after_ip)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secret_names_are_recognised_and_ordinary_ones_are_not() {
        for name in [
            "DASHBOARD_TOKEN",
            "MY_api-key",
            "auth_key",
            "PASSWORD",
            "private_key",
            "SessionKey",
        ] {
            assert!(is_secret_name(name), "{name}");
        }
        for name in ["PATH", "EMPTY", "", "HOME", "PYTHONPATH", "PIN"] {
            assert!(!is_secret_name(name), "{name}");
        }
    }

    #[test]
    fn credential_shapes_are_replaced() {
        assert_eq!(
            scrub_credentials("key sk-abcdefghijklmnopqrst here"),
            "key [key removed] here"
        );
        assert_eq!(scrub_credentials("AKIA0123456789ABCDEF"), "[key removed]");
        assert_eq!(
            scrub_credentials("Authorization: Bearer abcdefghijklmnopqrstuvwx"),
            "Authorization: Bearer [token removed]"
        );
        assert_eq!(
            scrub_credentials(r##"api_key: "hunter2hunter""##),
            r##"api_key: "[removed]""##
        );
    }

    #[test]
    fn a_phone_is_masked_from_the_leftmost_position_that_works() {
        // The greedy run takes 16 characters, then needs a digit and a non-word character
        // after it; when that fails the engine walks on, and the match it settles on is the
        // one Python's re would settle on too.
        assert_eq!(replace_phone("44 20 7946 25412345"), "[phone] 25412345");
        assert_eq!(
            replace_phone("call +44 20 7946 0958 now"),
            "call [phone] now"
        );
        assert_eq!(
            replace_phone("version 1.2.3.4 here"),
            "version 1.2.3.4 here"
        );
        assert_eq!(replace_phone("+1 555 123 4567"), "[phone]");
    }

    #[test]
    fn pii_is_masked_in_the_python_order() {
        assert_eq!(
            scrub_pii("mail me at ada@example.test please"),
            "mail me at [email] please"
        );
        assert_eq!(scrub_pii("from 10.0.0.12"), "from [ip]");
        assert_eq!(scrub_pii("call +1 555 123 4567 now"), "call [phone] now");
    }

    #[test]
    fn an_ip_inside_an_email_is_masked_as_an_ip_not_an_email() {
        // The e-mail pattern runs first, so this is an e-mail; the point is that both are tried.
        assert_eq!(scrub_pii("a@b.test"), "[email]");
    }
}
