#!/usr/bin/env python
"""Bot Server installer — visual GUI.

A real-time, visual front end for scripts/install.py: same
hardware/software-aware detection and installation logic, no separate
code path — this file only drives that script and renders its progress
live. Built on `tkinter` (Python's standard library) deliberately: this
installer's whole job is to bootstrap everything else (Rust, Tauri, the
venv), so it cannot itself depend on anything that isn't already
guaranteed present on a bare Python 3.11+ install.

Usage:
    python scripts/install_gui.py

Falls back automatically to the plain text installer (scripts/install.py)
if no display is available (e.g. an SSH session with no X11/Wayland, or
tkinter isn't available in this Python build) or if --cli is passed.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
INSTALL_PY = str(ROOT / "scripts" / "install.py")


def _run_cli_fallback(extra_args: list[str]) -> None:
    proc = subprocess.run([PYTHON, INSTALL_PY, *extra_args])
    sys.exit(proc.returncode)


def main() -> None:
    args = sys.argv[1:]
    if "--cli" in args:
        _run_cli_fallback([a for a in args if a != "--cli"])
        return

    try:
        import tkinter as tk
        from tkinter import ttk, scrolledtext, messagebox
    except ImportError:
        print("tkinter isn't available in this Python build — falling back to the text installer.\n")
        _run_cli_fallback(args)
        return

    try:
        app = InstallerApp()
    except tk.TclError as e:
        # No display (headless server, SSH without X forwarding, etc.)
        print(f"No display available ({e}) — falling back to the text installer.\n")
        _run_cli_fallback(args)
        return
    app.mainloop()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

STATUS_GLYPH = {"ok": "✓", "missing": "✗", "doing": "…", "warn": "!", "err": "✗"}
STATUS_COLOR = {"ok": "#2e7d32", "missing": "#9e9e9e", "doing": "#1565c0", "warn": "#e65100", "err": "#c62828"}


def _load_tk():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox
    return tk, ttk, scrolledtext, messagebox


class InstallerApp:
    """Two screens in one window: a detection/options screen, then a
    live-progress screen. Kept as one class (not two Toplevels) so state
    — the environment report, chosen options — flows naturally between
    them without extra plumbing."""

    def __init__(self):
        tk, ttk, scrolledtext, messagebox = _load_tk()
        self.tk, self.ttk, self.scrolledtext, self.messagebox = tk, ttk, scrolledtext, messagebox

        self.root = tk.Tk()
        self.root.title("Bot Server — Installer")
        self.root.geometry("760x560")
        self.root.minsize(640, 480)

        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._section_rows: dict[str, tk.Frame] = {}
        self._section_order: list[str] = []
        self._total_sections = 11  # scripts/install.py's SECTIONS length

        self._build_detect_screen()
        self._start_detection()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def mainloop(self) -> None:
        self.root.mainloop()

    # ---- screen 1: environment detection + options -----------------

    def _build_detect_screen(self) -> None:
        tk, ttk = self.tk, self.ttk
        self.detect_frame = ttk.Frame(self.root, padding=16)
        self.detect_frame.pack(fill="both", expand=True)

        ttk.Label(self.detect_frame, text="Bot Server Installer", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self.detect_frame, text="Detecting this machine's hardware and software environment…",
                  foreground="#666").pack(anchor="w", pady=(2, 12))

        self.env_box = self.scrolledtext.ScrolledText(self.detect_frame, height=9, wrap="word", font=("Consolas", 9))
        self.env_box.pack(fill="x", pady=(0, 12))
        self.env_box.configure(state="disabled")

        self.check_frame = ttk.LabelFrame(self.detect_frame, text="What's already present", padding=10)
        self.check_frame.pack(fill="both", expand=True, pady=(0, 12))
        self.check_rows: dict[str, tk.Label] = {}

        opts = ttk.LabelFrame(self.detect_frame, text="Options", padding=10)
        opts.pack(fill="x", pady=(0, 12))
        self.var_build = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=True)
        self.var_system_deps = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Build the production desktop app when done (cargo tauri build)",
                        variable=self.var_build).pack(anchor="w")
        ttk.Checkbutton(opts, text="Register Bot Server to start at login",
                        variable=self.var_autostart).pack(anchor="w")
        ttk.Checkbutton(opts, text="Install missing system packages (Rust, Tauri CLI, native libs) — uncheck if already installed",
                        variable=self.var_system_deps).pack(anchor="w")

        btnrow = ttk.Frame(self.detect_frame)
        btnrow.pack(fill="x")
        self.start_btn = ttk.Button(btnrow, text="Start Installation", command=self._start_install, state="disabled")
        self.start_btn.pack(side="right")
        ttk.Button(btnrow, text="Re-check", command=self._start_detection).pack(side="right", padx=(0, 8))

    def _start_detection(self) -> None:
        self.start_btn.configure(state="disabled")
        for w in self.check_frame.winfo_children():
            w.destroy()
        self.check_rows.clear()
        self._set_env_text("Running scripts/install.py --check --json …\n")
        threading.Thread(target=self._detect_worker, daemon=True).start()
        self.root.after(100, self._poll_queue)

    def _detect_worker(self) -> None:
        try:
            proc = subprocess.Popen(
                [PYTHON, INSTALL_PY, "--check", "--json"],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._queue.put({"phase": "detect", **evt})
            proc.wait()
        except Exception as e:  # surfaced in the log, not raised on a background thread
            self._queue.put({"phase": "detect", "type": "err", "text": f"detection failed to run: {e}"})
        self._queue.put({"phase": "detect", "type": "_done"})

    def _set_env_text(self, text: str) -> None:
        self.env_box.configure(state="normal")
        self.env_box.delete("1.0", "end")
        self.env_box.insert("1.0", text)
        self.env_box.configure(state="disabled")

    def _render_env_report(self, report: dict) -> None:
        lines = [
            f"OS:            {report.get('system', 'unknown')} ({report.get('platform', '')})",
            f"Architecture:  {report.get('arch', 'unknown')}",
            f"Python:        {report.get('python_version', '')}  at  {report.get('python_executable', '')}",
        ]
        if "distro" in report:
            lines.append(f"Distro:        {report['distro']}")
        if report.get("nixos"):
            lines.append("Note:          NixOS detected — system package steps handled by `nix develop` instead.")
        if report.get("wsl"):
            lines.append("Note:          Running under WSL.")
        self._set_env_text("\n".join(lines) + "\n")

    def _set_check_row(self, name: str, ok: bool) -> None:
        ttk = self.ttk
        if name not in self.check_rows:
            row = ttk.Label(self.check_frame, text="")
            row.pack(anchor="w")
            self.check_rows[name] = row
        glyph = "✓" if ok else "✗"
        color = "#2e7d32" if ok else "#c62828"
        self.check_rows[name].configure(text=f"{glyph}  {name}", foreground=color)

    def _poll_queue(self) -> None:
        try:
            while True:
                evt = self._queue.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        if getattr(self, "_watching", True):
            self.root.after(100, self._poll_queue)

    def _handle_event(self, evt: dict) -> None:
        phase = evt.get("phase")
        etype = evt.get("type")
        if phase == "detect":
            if etype == "env":
                self._render_env_report(evt["report"])
            elif etype == "summary":
                self.summary = evt
                self.start_btn.configure(state="normal")
                self.start_btn.configure(text="Install missing pieces" if not evt.get("all_ok") else "Re-run / Update")
            elif etype == "_done":
                pass
            # step-name inference: install.py's Step.head() text lines up
            # with a human-readable component name closely enough to just
            # show the raw text as the row label — except "Summary", whose
            # own ok/missing lines are per-component re-statements of rows
            # already rendered under their own section, not a new one.
            if etype == "head":
                self._pending_component = evt["text"] if evt["text"] != "Summary" else None
            elif etype in ("ok", "missing") and getattr(self, "_pending_component", None):
                self._set_check_row(self._pending_component, etype == "ok")
                self._pending_component = None
        elif phase == "install":
            self._handle_install_event(evt)

    # ---- screen 2: live install progress ----------------------------

    def _start_install(self) -> None:
        self.detect_frame.pack_forget()
        self._build_progress_screen()
        args = ["--yes", "--json"]
        if not self.var_build.get():
            args.append("--no-build")
        if not self.var_autostart.get():
            args.append("--no-autostart")
        if not self.var_system_deps.get():
            args.append("--no-system-deps")
        threading.Thread(target=self._install_worker, args=(args,), daemon=True).start()

    def _build_progress_screen(self) -> None:
        tk, ttk = self.tk, self.ttk
        self.progress_frame = ttk.Frame(self.root, padding=16)
        self.progress_frame.pack(fill="both", expand=True)

        ttk.Label(self.progress_frame, text="Installing Bot Server…", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        self.current_step_var = tk.StringVar(value="Starting…")
        ttk.Label(self.progress_frame, textvariable=self.current_step_var, foreground="#666").pack(anchor="w", pady=(2, 8))

        self.progressbar = ttk.Progressbar(self.progress_frame, maximum=self._total_sections, value=0)
        self.progressbar.pack(fill="x", pady=(0, 12))

        self.log_box = self.scrolledtext.ScrolledText(self.progress_frame, wrap="word", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_configure("ok", foreground=STATUS_COLOR["ok"])
        self.log_box.tag_configure("missing", foreground=STATUS_COLOR["missing"])
        self.log_box.tag_configure("doing", foreground=STATUS_COLOR["doing"])
        self.log_box.tag_configure("warn", foreground=STATUS_COLOR["warn"])
        self.log_box.tag_configure("err", foreground=STATUS_COLOR["err"])
        self.log_box.tag_configure("head", font=("Consolas", 9, "bold"))

        self.bottom_row = ttk.Frame(self.progress_frame)
        self.bottom_row.pack(fill="x", pady=(12, 0))
        self.close_btn = ttk.Button(self.bottom_row, text="Close", command=self.root.destroy, state="disabled")
        self.close_btn.pack(side="right")

    def _append_log(self, text: str, tag: str | None = None) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n", tag or ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _install_worker(self, args: list[str]) -> None:
        try:
            self._proc = subprocess.Popen(
                [PYTHON, INSTALL_PY, *args],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    evt = {"type": "doing", "text": line}
                self._queue.put({"phase": "install", **evt})
            code = self._proc.wait()
            self._queue.put({"phase": "install", "type": "_exit", "code": code})
        except Exception as e:
            self._queue.put({"phase": "install", "type": "err", "text": f"installer crashed: {e}"})
            self._queue.put({"phase": "install", "type": "_exit", "code": 1})

    def _handle_install_event(self, evt: dict) -> None:
        etype = evt.get("type")
        if etype == "head":
            self._section_order.append(evt["text"])
            self.current_step_var.set(evt["text"])
            self.progressbar.configure(value=len(self._section_order) - 1)
            self._append_log(f"\n=== {evt['text']} ===", "head")
        elif etype in ("ok", "missing", "doing", "warn", "err"):
            glyph = STATUS_GLYPH.get(etype, "")
            self._append_log(f"  {glyph}  {evt['text']}", etype)
        elif etype == "env":
            pass  # already shown on screen 1
        elif etype == "summary":
            ok = evt.get("all_ok")
            self._append_log("")
            self._append_log("Installation complete." if ok else "Installation finished with issues — see above.",
                              "ok" if ok else "warn")
            if evt.get("next_step"):
                self._append_log(evt["next_step"])
        elif etype == "_exit":
            self.progressbar.configure(value=self._total_sections)
            self.current_step_var.set("Done" if evt.get("code") == 0 else f"Exited with code {evt.get('code')}")
            self.close_btn.configure(state="normal")

    def _on_close(self) -> None:
        if self._proc and self._proc.poll() is None:
            if not self.messagebox.askyesno("Installation in progress",
                                             "The installer is still running. Stop it and close anyway?"):
                return
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._watching = False
        self.root.destroy()


if __name__ == "__main__":
    main()
