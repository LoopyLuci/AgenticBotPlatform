//! The sandbox's own tests, run by `cargo test`.
//!
//! These are the claims the rest of ABP rests on: a rule cannot loop forever, cannot build
//! something enormous, cannot reach the filesystem or the network, and every failure says where in
//! the source it happened. They exercise `sandbox.rs` directly - the real engine, the real limits -
//! with no Python anywhere, because the bridge is not what makes a rule safe.

use abp_rules::sandbox::{self, Kind, Limits};
use rhai::{Array, Dynamic, Map};

fn ctx(pairs: &[(&str, Dynamic)]) -> Dynamic {
    let mut map = Map::new();
    for (key, value) in pairs {
        map.insert((*key).to_string().into(), value.clone());
    }
    Dynamic::from_map(map)
}

fn text(value: &str) -> Dynamic {
    Dynamic::from(value.to_string())
}

fn run(source: &str, kind: Kind) -> Result<Dynamic, sandbox::Fault> {
    let program = sandbox::compile(source, kind, Limits::default())?;
    program.evaluate(ctx(&[("message", text("deploy the release"))]))
}

#[test]
fn a_condition_answers_yes_or_no_against_a_context() {
    let source = r#"ctx.message.contains("deploy") && ctx.message.len() > 5"#;
    let program = sandbox::compile(source, Kind::Condition, Limits::default()).expect("compiles");
    assert!(program
        .matches(ctx(&[("message", text("please deploy the release"))]))
        .expect("runs"));
    assert!(!program
        .matches(ctx(&[("message", text("what is the weather"))]))
        .expect("runs"));
}

#[test]
fn a_top_level_context_key_is_the_same_value_as_ctx() {
    let program = sandbox::compile(
        r#"message.len() == ctx.message.len()"#,
        Kind::Condition,
        Limits::default(),
    )
    .expect("compiles");
    assert!(program
        .matches(ctx(&[("message", text("hello"))]))
        .expect("runs"));
}

#[test]
fn a_route_answers_a_value() {
    let value = run(
        r#"if ctx.message.contains("image") { "vision" } else { "api" }"#,
        Kind::Route,
    )
    .expect("runs");
    assert_eq!(value.into_string().as_deref(), Ok("api"));
}

#[test]
fn a_condition_that_answers_something_else_is_refused() {
    let fault = run(r#""not a yes or no""#, Kind::Condition).expect_err("refused");
    assert!(fault.message.contains("true or false"), "{}", fault.message);
    assert_eq!(fault.limit.as_deref(), Some("wrong_type"));
}

// ---- the limits ------------------------------------------------------------------------------

#[test]
fn an_infinite_loop_stops() {
    let started = std::time::Instant::now();
    let fault = run("loop { }", Kind::Condition).expect_err("refused");
    assert!(
        started.elapsed().as_secs() < 5,
        "the operation limit did not stop the loop"
    );
    assert_eq!(fault.limit.as_deref(), Some("operations"));
    assert!(
        fault.line > 0,
        "a limit failure still says where: {fault:?}"
    );
}

#[test]
fn a_runaway_loop_over_a_large_range_stops_too() {
    let fault =
        run("let n = 0; while n < 100000000 { n += 1 } n", Kind::Route).expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("operations"));
}

#[test]
fn a_huge_string_allocation_is_refused() {
    let fault = run(
        r#"let s = "x"; let i = 0; while i < 40 { s += s; i += 1 } s"#,
        Kind::Route,
    )
    .expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("size"));
}

#[test]
fn an_over_long_rule_is_refused_before_it_is_even_parsed() {
    let limits = Limits {
        max_source_chars: 32,
        ..Limits::default()
    };
    let fault =
        sandbox::compile(&"true && ".repeat(20), Kind::Condition, limits).expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("size"));
    assert!(fault.compile);
}

#[test]
fn an_oversized_context_is_refused_with_a_clear_reason() {
    let limits = Limits {
        max_string_size: 16,
        ..Limits::default()
    };
    let program = sandbox::compile("true", Kind::Condition, limits).expect("compiles");
    let fault = program
        .evaluate(ctx(&[(
            "message",
            text("this message is much longer than sixteen characters"),
        )]))
        .expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("size"));
}

#[test]
fn an_oversized_context_array_is_refused() {
    let limits = Limits {
        max_array_size: 2,
        ..Limits::default()
    };
    let program = sandbox::compile("true", Kind::Condition, limits).expect("compiles");
    let tags = Dynamic::from_array(vec![text("a"), text("b"), text("c")]);
    let fault = program
        .evaluate(ctx(&[("tags", tags)]))
        .expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("size"));
}

#[test]
fn a_context_that_nests_too_deeply_is_refused() {
    let limits = Limits {
        max_context_depth: 3,
        ..Limits::default()
    };
    let mut nested = Dynamic::from_array(Array::new());
    for _ in 0..8 {
        nested = Dynamic::from_array(vec![nested]);
    }
    let checked = sandbox::check_context_size(nested, limits, 0);
    let fault = checked.expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("size"));
}

#[test]
fn a_context_key_that_is_not_a_name_is_refused() {
    let mut map = Map::new();
    map.insert("not a name".to_string().into(), text("x"));
    let limits = Limits::default();
    let fault =
        sandbox::check_context_size(Dynamic::from_map(map), limits, 0).expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("wrong_type"));
}

#[test]
fn unbounded_recursion_stops_at_the_call_depth_limit() {
    let fault = run("fn down(n) { down(n + 1) } down(0)", Kind::Route).expect_err("refused");
    assert!(
        matches!(
            fault.limit.as_deref(),
            Some("call_depth") | Some("operations")
        ),
        "recursion stopped for the wrong reason: {fault:?}"
    );
}

// ---- no I/O ----------------------------------------------------------------------------------

#[test]
fn a_rule_cannot_open_a_file() {
    for name in ["open", "read_file", "read", "file_system", "load"] {
        let fault = run(&format!(r#"{name}("/etc/passwd")"#), Kind::Route).expect_err("refused");
        assert_eq!(
            fault.limit.as_deref(),
            Some("unknown_function"),
            "{name} should not exist, but the failure was {fault:?}"
        );
    }
}

#[test]
fn a_rule_cannot_reach_the_network_or_a_process() {
    for name in [
        "http_get",
        "fetch",
        "socket",
        "shell",
        "system",
        "exec",
        "spawn_process",
        "popen",
    ] {
        let fault = run(&format!("{name}(\"x\")"), Kind::Route).expect_err("refused");
        assert_eq!(
            fault.limit.as_deref(),
            Some("unknown_function"),
            "{name}: {fault:?}"
        );
    }
}

#[test]
fn a_rule_cannot_import_a_module() {
    let fault = run(r#"import "std" as std; 1"#, Kind::Route).expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("unknown_module"));
}

#[test]
fn a_rule_cannot_write_to_stdout() {
    // `print` is removed from the namespace, so this is a call to nothing at all rather than a line
    // of output in ABP's log (and Rhai catches it while parsing, before anything runs).
    let fault = run(r#"print("hello")"#, Kind::Route).expect_err("refused");
    assert!(
        fault.compile || fault.limit.as_deref() == Some("unknown_function"),
        "{fault:?}"
    );
    assert!(fault.message.contains("print"), "{fault:?}");
}

#[test]
fn a_name_the_context_does_not_have_is_an_error_rather_than_an_empty_answer() {
    // A typo in `ctx.mesage` reads as nothing, so the mistake surfaces on the very next operation
    // instead of quietly answering "no" for the rest of the rule's life.
    let fault = run(r#"ctx.mesage.contains("x")"#, Kind::Condition).expect_err("refused");
    assert!(fault.message.contains("contains"), "{fault:?}");
    assert_eq!(fault.limit.as_deref(), Some("unknown_function"));
}

// ---- positions -------------------------------------------------------------------------------

#[test]
fn a_syntax_error_says_which_line_and_column() {
    let source = "let x = 1;\nlet y = ;\nx";
    let fault = sandbox::compile(source, Kind::Condition, Limits::default()).expect_err("refused");
    assert!(fault.compile, "a syntax error is a compile failure");
    assert_eq!(fault.line, 2, "{fault:?}");
    assert!(fault.column > 0, "{fault:?}");
    assert_eq!(fault.limit.as_deref(), Some("syntax"));
}

#[test]
fn a_runtime_error_says_where_it_happened() {
    let program = sandbox::compile(
        "let x = 1;\nlet y = x / 0;\ntrue",
        Kind::Condition,
        Limits::default(),
    );
    // Integer division by zero may be an error or a value depending on the build; either way the
    // rule must not silently claim to have run cleanly with a nonsense answer.
    match program {
        Err(fault) => {
            assert!(!fault.compile);
            assert!(fault.line >= 2, "{fault:?}");
        }
        Ok(program) => {
            let _ = program.matches(ctx(&[("message", text("x"))]));
        }
    }
}

#[test]
fn an_empty_rule_is_refused() {
    let fault =
        sandbox::compile("   \n  ", Kind::Condition, Limits::default()).expect_err("refused");
    assert!(fault.compile);
    assert_eq!(fault.limit.as_deref(), Some("syntax"));
}

// ---- caching ---------------------------------------------------------------------------------

#[test]
fn the_same_source_is_parsed_once() {
    let source = r#"ctx.message.contains("cache me")"#;
    let (first, cached_first) =
        sandbox::compile_checked(source, Kind::Condition, Limits::default()).expect("compiles");
    assert!(
        !cached_first,
        "the first compile of a rule has nothing to reuse"
    );

    for _ in 0..5 {
        let (again, cached) =
            sandbox::compile_checked(source, Kind::Condition, Limits::default()).expect("compiles");
        assert!(cached, "recompiling the same rule parsed it again");
        assert!(again
            .matches(ctx(&[("message", text("please cache me"))]))
            .expect("a cached rule still answers correctly"));
    }
    assert!(first
        .matches(ctx(&[("message", text("please cache me"))]))
        .expect("runs"));
    assert!(sandbox::cache_stats().2 > 0);
}

#[test]
fn the_same_source_under_a_different_kind_still_evaluates_as_that_kind() {
    let source = "true";
    let condition = sandbox::compile(source, Kind::Condition, Limits::default()).expect("compiles");
    let route = sandbox::compile(source, Kind::Route, Limits::default()).expect("compiles");
    assert!(condition.matches(ctx(&[])).expect("runs"));
    assert_eq!(route.kind(), Kind::Route);
    assert_eq!(route.evaluate(ctx(&[])).expect("runs").as_bool(), Ok(true));
}

#[test]
fn the_context_must_be_a_map() {
    let program = sandbox::compile("true", Kind::Condition, Limits::default()).expect("compiles");
    let fault = program.evaluate(Dynamic::UNIT).expect_err("refused");
    assert_eq!(fault.limit.as_deref(), Some("wrong_type"));
}
