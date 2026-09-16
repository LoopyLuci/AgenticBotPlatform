//! A real embedded terminal — ConPTY on Windows via `portable-pty` (the
//! same crate WezTerm uses), not a plain piped-stdio subprocess. Piped
//! stdio can only ever capture line-buffered output and breaks anything
//! that expects a real terminal (colored output, a pager, python's REPL,
//! progress bars) — a pty is what makes those behave normally instead of
//! garbling or hanging waiting for a TTY that was never there.
//!
//! Desktop-app-only by design (see capabilities/default.json — none of
//! these commands are granted to the "remote" 127.0.0.1 origin the plain
//! browser dashboard also loads this same UI from), since this hands
//! whoever can drive it a real shell on the host machine. The browser
//! dashboard gets the separate, scoped `/api/terminal/exec` route
//! instead (bot/dashboard/server.py) — ABP's own slash commands only,
//! nothing that can touch the filesystem or spawn arbitrary processes.
//!
//! One shell session per app window, started lazily on the frontend's
//! first `terminal_start()` call rather than at app boot, so a user who
//! never opens the terminal panel never pays for a spawned shell at all.

use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

use portable_pty::{native_pty_system, Child as PtyChild, CommandBuilder, MasterPty, PtySize};
use serde::Serialize;
use tauri::{AppHandle, Emitter, State};

#[derive(Clone, Serialize)]
struct TerminalOutput {
    data: String,
}

#[derive(Default)]
pub(crate) struct TerminalState {
    inner: Mutex<Option<Session>>,
}

struct Session {
    master: Box<dyn MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    child: Box<dyn PtyChild + Send + Sync>,
    // Flips once on terminal_stop()/window close so the reader thread's
    // next read-error (from the pty closing under it) is treated as a
    // clean shutdown instead of emitting a spurious "shell exited" line.
    stopping: Arc<AtomicBool>,
}

/// Starts the one shell session for this window if it isn't already
/// running (idempotent — a second call while a session is live is a
/// harmless no-op, matching start_server's own "already running" shape).
/// PowerShell, not cmd.exe: it's the modern default on every Windows
/// version this app targets, and — same as any real terminal — nothing
/// stops the user running `bash`/`wsl`/`python` etc. as a command inside
/// it to drop into another shell entirely.
#[tauri::command]
pub(crate) fn terminal_start(app: AppHandle, state: State<TerminalState>) -> Result<(), String> {
    let mut guard = state.inner.lock().map_err(|_| "state poisoned".to_string())?;
    if guard.is_some() {
        return Ok(());
    }

    let pty_system = native_pty_system();
    let pair = pty_system
        .openpty(PtySize {
            rows: 24,
            cols: 80,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| format!("failed to open pty: {e}"))?;

    let cmd = CommandBuilder::new("powershell.exe");
    let child = pair
        .slave
        .spawn_command(cmd)
        .map_err(|e| format!("failed to spawn powershell: {e}"))?;
    // The slave side is only needed to spawn the child — portable-pty's
    // own examples drop it immediately after, and holding it open past
    // this point has caused hangs on Windows in other projects using this
    // crate (ConPTY-specific: the master's read end never sees EOF while
    // any handle to the slave survives).
    drop(pair.slave);

    let mut reader = pair
        .master
        .try_clone_reader()
        .map_err(|e| format!("failed to clone pty reader: {e}"))?;
    let writer = pair
        .master
        .take_writer()
        .map_err(|e| format!("failed to take pty writer: {e}"))?;

    let stopping = Arc::new(AtomicBool::new(false));
    let reader_stopping = stopping.clone();
    let reader_handle = app.clone();
    thread::spawn(move || {
        let mut buf = [0u8; 8192];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break, // real EOF — the shell process exited
                Ok(n) => {
                    // Lossy: a pty's byte stream can split a multi-byte
                    // UTF-8 sequence across two reads. Rare in practice
                    // (mid-character read boundaries are a tiny fraction
                    // of a second wide) and never worth losing the whole
                    // terminal session over a single mangled glyph.
                    let text = String::from_utf8_lossy(&buf[..n]).into_owned();
                    let _ = reader_handle.emit("terminal-output", TerminalOutput { data: text });
                }
                Err(_) => break, // pty closed out from under us — see terminal_stop()
            }
        }
        if !reader_stopping.load(Ordering::SeqCst) {
            let _ = reader_handle.emit(
                "terminal-output",
                TerminalOutput {
                    data: "\r\n[shell exited]\r\n".to_string(),
                },
            );
        }
    });

    *guard = Some(Session {
        master: pair.master,
        writer,
        child,
        stopping,
    });
    Ok(())
}

/// Raw keystrokes/pasted text straight to the pty — the frontend's xterm.js
/// instance forwards its own `onData` callback here verbatim, control
/// characters and all, exactly like a real terminal emulator's PTY layer
/// expects. Not a "run this command and give me the output" call; there is
/// no concept of "one command" at this layer, only a byte stream, same as
/// a real terminal.
#[tauri::command]
pub(crate) fn terminal_write(state: State<TerminalState>, data: String) -> Result<(), String> {
    let mut guard = state.inner.lock().map_err(|_| "state poisoned".to_string())?;
    match guard.as_mut() {
        Some(session) => session
            .writer
            .write_all(data.as_bytes())
            .map_err(|e| format!("write failed: {e}")),
        None => Err("no terminal session running".to_string()),
    }
}

/// Keeps the pty's own idea of its size in sync with the xterm.js panel's
/// actual pixel dimensions (via the fit addon) — needed for anything that
/// draws to specific columns/rows (a progress bar, `git diff`'s pager, a
/// REPL's line editing) to wrap and redraw correctly instead of assuming
/// a stale 80x24.
#[tauri::command]
pub(crate) fn terminal_resize(state: State<TerminalState>, cols: u16, rows: u16) -> Result<(), String> {
    let guard = state.inner.lock().map_err(|_| "state poisoned".to_string())?;
    match guard.as_ref() {
        Some(session) => session
            .master
            .resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| format!("resize failed: {e}")),
        None => Ok(()), // no session yet — the panel's own fit-on-open will resize once one starts
    }
}

/// Kills the shell process. Safe to call with no session running (a no-op,
/// same shape as stop_server on an already-stopped bot server) and safe
/// to call from the window-close handler even if the terminal panel was
/// never opened this session.
pub(crate) fn stop_terminal(state: &TerminalState) {
    let mut guard = match state.inner.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if let Some(mut session) = guard.take() {
        session.stopping.store(true, Ordering::SeqCst);
        let _ = session.child.kill();
    }
}

#[tauri::command]
pub(crate) fn terminal_stop(state: State<TerminalState>) -> Result<(), String> {
    stop_terminal(&state);
    Ok(())
}
