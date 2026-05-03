import tkinter as tk
from tkinter import ttk, scrolledtext
import threading
import queue
import re
import json
import os
import random
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from marlin_prober import MarlinProber

SETTINGS_FILE = "prober_settings.json"
ALL_AXES = ['X', 'Y', 'Z']

DEFAULT_SETTINGS = {
    'l_ax':       'X',
    'start':      '0.0',
    'end':        '100.0',
    'count':      '10',
    'p_ax':       'Y',
    'dir_pos':    '1',
    'offset':     '5.0',
    'probe_dist': '10.0',
    'feed_move':  '2000',
    'feed_probe': '100',
    'host':       '192.168.1.50',
    'debug_mode': '1',
    'fixed_X':    '0.0',
    'fixed_Y':    '50.0',
    'fixed_Z':    '-10.0',
}


# ── Backend wrapper ────────────────────────────────────────────────────────────

class GUIMarlinProber(MarlinProber):
    """MarlinProber that routes all I/O through thread-safe callbacks."""

    def __init__(self, debug_mode, log_cb, result_cb, stop_event):
        super().__init__(debug_mode=debug_mode)
        self._log_cb    = log_cb
        self._result_cb = result_cb
        self._stop      = stop_event

    def run(self):
        tn = None
        if not self.debug_mode:
            try:
                import telnetlib
                tn = telnetlib.Telnet(self.host, 23, timeout=5)
            except Exception as e:
                self._log_cb(f"[ERROR]: Connection failed: {e}", "error")
                return

        point_index = 0

        for cmd in self.recipe:
            if self._stop.is_set():
                self._log_cb("[CANCELLED]: Sequence aborted by user.", "warn")
                break

            if self.debug_mode:
                response = self._simulate_marlin_response(cmd)
                self._log_cb(f"[SIM SEND]: {cmd}", "send")
                self._log_cb(f"[SIM RECV]: {response.strip()}", "recv")
            else:
                self._log_cb(f"[SEND]: {cmd}", "send")
                tn.write(f"{cmd}\n".encode('ascii'))
                response = ""
                # For M114 D we must keep reading until the "Logical:" position
                # line is present in the buffer, because Marlin may send stale
                # "ok" acknowledgements from previously queued commands before
                # the actual M114 D payload arrives.
                need_logical = "M114 D" in cmd
                while True:
                    if self._stop.is_set():
                        break
                    line = tn.read_until(b"\n", timeout=5).decode('ascii', errors='replace')
                    response += line
                    self._log_cb(f"[RECV]: {line.strip()}", "recv")
                    if line.startswith("!!"):
                        self._log_cb(f"[KILL]: {line.strip()}", "error")
                        self._log_cb("[ABORT]: Marlin kill signal — sequence stopped.", "error")
                        self._log_cb("[INFO]: Use M112 to command an emergency stop.", "warn")
                        if tn:
                            tn.close()
                        return
                    # Exit condition: received ok AND (not M114 D, or Logical: line is present)
                    has_ok      = "ok" in response.lower()
                    has_logical = "logical:" in response.lower()
                    if has_ok and (not need_logical or has_logical):
                        break

            if "M114 D" in cmd:
                # Real Marlin format: "Logical: X: 0.000 Y: 3.430 Z: 0.000"
                # Regex skips past "Logical:" and any preceding axes to reach p_ax.
                pattern = rf"Logical:.*?\b{self.config['p_ax']}:\s*([-+]?\d*\.\d+|\d+)"
                match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
                if match:
                    abs_val = float(match.group(1))
                    self.abs_results.append(abs_val)
                    rel_val = abs_val - self.abs_results[0]
                    self.rel_results.append(rel_val)
                    self._result_cb(point_index, abs_val, rel_val)
                    point_index += 1

        if tn:
            tn.close()
        self._log_cb("[DONE]: Sequence complete.", "info")


# ── Main GUI ───────────────────────────────────────────────────────────────────

class ProberGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Marlin Prober")
        self.root.geometry("1280x860")
        self.root.minsize(900, 600)

        self._msg_queue  = queue.Queue()
        self._stop_event = threading.Event()
        self._run_thread = None
        self._debounce   = None

        # Live result accumulators
        self._res_x   = []
        self._res_abs = []
        self._res_rel = []

        self._load_settings()
        self._build_ui()
        self._setup_traces()
        self._schedule_plan_update()
        self._poll_queue()

    # ── Settings ──────────────────────────────────────────────────────────────

    def _load_settings(self):
        self.vars = {k: tk.StringVar(value=v) for k, v in DEFAULT_SETTINGS.items()}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE) as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in self.vars:
                        self.vars[k].set(str(v))
            except Exception:
                pass

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump({k: v.get() for k, v in self.vars.items()}, f, indent=2)
        except Exception:
            pass

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        main_pw = tk.PanedWindow(self.root, orient=tk.HORIZONTAL,
                                 sashwidth=6, sashrelief=tk.FLAT, bg='#aaaaaa')
        main_pw.pack(fill=tk.BOTH, expand=True)

        # ── Left: config panel ────────────────────────────────────────────────
        left = ttk.Frame(main_pw, width=310)
        left.pack_propagate(False)
        main_pw.add(left, minsize=280)
        self._build_config_panel(left)

        # ── Right: plot on top, terminal on bottom ────────────────────────────
        right_pw = tk.PanedWindow(main_pw, orient=tk.VERTICAL,
                                  sashwidth=6, sashrelief=tk.FLAT, bg='#aaaaaa')
        main_pw.add(right_pw, minsize=550)

        plot_frame = ttk.Frame(right_pw)
        right_pw.add(plot_frame, minsize=380)
        self._build_plot_panel(plot_frame)

        term_frame = ttk.LabelFrame(right_pw, text=" Terminal ")
        right_pw.add(term_frame, minsize=140)
        self._build_terminal_panel(term_frame)

    # ── Config panel ──────────────────────────────────────────────────────────

    def _build_config_panel(self, parent):
        # Scrollable inner canvas
        canvas = tk.Canvas(parent, borderwidth=0, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')

        inner.bind('<Configure>', lambda e: canvas.configure(
            scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind_all('<MouseWheel>',
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

        p = dict(padx=10, pady=3)

        # ── Axis configuration ────────────────────────────────────────────────
        self._section(inner, "Axis Configuration")

        row = ttk.Frame(inner); row.pack(fill=tk.X, **p)
        ttk.Label(row, text="Line Axis:", width=16, anchor='w').pack(side=tk.LEFT)
        for ax in ALL_AXES:
            ttk.Radiobutton(row, text=ax, value=ax, variable=self.vars['l_ax'],
                            command=self._on_axis_change).pack(side=tk.LEFT, padx=3)

        self._entry_row(inner, "Start (mm):", 'start')
        self._entry_row(inner, "End (mm):",   'end')
        self._entry_row(inner, "Point count:", 'count')

        row2 = ttk.Frame(inner); row2.pack(fill=tk.X, **p)
        ttk.Label(row2, text="Probe Axis:", width=16, anchor='w').pack(side=tk.LEFT)
        for ax in ALL_AXES:
            ttk.Radiobutton(row2, text=ax, value=ax, variable=self.vars['p_ax'],
                            command=self._on_axis_change).pack(side=tk.LEFT, padx=3)

        row3 = ttk.Frame(inner); row3.pack(fill=tk.X, **p)
        ttk.Label(row3, text="Direction:", width=16, anchor='w').pack(side=tk.LEFT)
        ttk.Radiobutton(row3, text="Positive (+)", value='1',
                        variable=self.vars['dir_pos']).pack(side=tk.LEFT, padx=3)
        ttk.Radiobutton(row3, text="Negative (–)", value='0',
                        variable=self.vars['dir_pos']).pack(side=tk.LEFT, padx=3)

        # ── Fixed positions ───────────────────────────────────────────────────
        self._section(inner, "Fixed Positions (mm)")
        self._fixed_frame = ttk.Frame(inner)
        self._fixed_frame.pack(fill=tk.X)
        self._rebuild_fixed_fields()

        # ── Probing parameters ────────────────────────────────────────────────
        self._section(inner, "Probing Parameters")
        self._entry_row(inner, "Staging offset (mm):", 'offset')
        self._entry_row(inner, "Max probe dist (mm):", 'probe_dist')
        self._entry_row(inner, "Move feed (mm/min):",  'feed_move')
        self._entry_row(inner, "Probe feed (mm/min):", 'feed_probe')

        # ── Connection ────────────────────────────────────────────────────────
        self._section(inner, "Connection")
        self._entry_row(inner, "Host IP:", 'host')

        row4 = ttk.Frame(inner); row4.pack(fill=tk.X, **p)
        ttk.Label(row4, text="Mode:", width=16, anchor='w').pack(side=tk.LEFT)
        ttk.Radiobutton(row4, text="Simulate", value='1',
                        variable=self.vars['debug_mode']).pack(side=tk.LEFT, padx=3)
        ttk.Radiobutton(row4, text="Live CNC", value='0',
                        variable=self.vars['debug_mode']).pack(side=tk.LEFT, padx=3)

        # ── Buttons ───────────────────────────────────────────────────────────
        ttk.Separator(inner, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=10)
        btn = ttk.Frame(inner)
        btn.pack(fill=tk.X, padx=10, pady=(0, 10))

        self._btn_start = ttk.Button(btn, text="▶  Start", command=self._on_start)
        self._btn_start.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self._btn_cancel = ttk.Button(btn, text="■  Cancel", command=self._on_cancel,
                                      state=tk.DISABLED)
        self._btn_cancel.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _section(self, parent, title):
        """Draws a labelled section divider."""
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, padx=6, pady=(10, 2))
        ttk.Label(f, text=title, font=('TkDefaultFont', 9, 'bold')).pack(side=tk.LEFT)
        ttk.Separator(f, orient=tk.HORIZONTAL).pack(side=tk.LEFT, fill=tk.X,
                                                     expand=True, padx=(6, 0))

    def _entry_row(self, parent, label, key):
        """One label + entry row bound to self.vars[key]."""
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, padx=10, pady=3)
        ttk.Label(f, text=label, width=20, anchor='w').pack(side=tk.LEFT)
        ttk.Entry(f, textvariable=self.vars[key], width=12).pack(side=tk.LEFT)

    def _on_axis_change(self):
        self._rebuild_fixed_fields()
        self._schedule_plan_update()

    def _rebuild_fixed_fields(self):
        """Show a fixed-value entry for every axis that is NOT the line axis."""
        for w in self._fixed_frame.winfo_children():
            w.destroy()
        l_ax = self.vars['l_ax'].get()
        p_ax = self.vars['p_ax'].get()
        for ax in ALL_AXES:
            if ax == l_ax:
                continue
            tag = "(probe ref)" if ax == p_ax else ""
            f = ttk.Frame(self._fixed_frame)
            f.pack(fill=tk.X, padx=10, pady=3)
            ttk.Label(f, text=f"{ax} fixed {tag}:", width=20, anchor='w').pack(side=tk.LEFT)
            ttk.Entry(f, textvariable=self.vars[f'fixed_{ax}'], width=12).pack(side=tk.LEFT)

    # ── Plot panel ────────────────────────────────────────────────────────────

    def _build_plot_panel(self, parent):
        self._fig = Figure(figsize=(6, 5), tight_layout=True)
        self._ax_plan = self._fig.add_subplot(2, 1, 1)
        self._ax_dev  = self._fig.add_subplot(2, 1, 2)

        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Live result line (deviation plot)
        self._live_line, = self._ax_dev.plot([], [], 'go-', linewidth=2,
                                              markersize=7, label='Deviation')
        self._ax_dev.axhline(0, color='black', linewidth=0.8, alpha=0.4)

    # ── Terminal panel ────────────────────────────────────────────────────────

    def _build_terminal_panel(self, parent):
        self._terminal = scrolledtext.ScrolledText(
            parent, height=8, font=('Courier', 9),
            bg='#1e1e1e', fg='#d4d4d4', insertbackground='white',
            state=tk.DISABLED)
        self._terminal.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Colour tags
        self._terminal.tag_config('send',  foreground='#569cd6')
        self._terminal.tag_config('recv',  foreground='#9cdcfe')
        self._terminal.tag_config('error', foreground='#f44747')
        self._terminal.tag_config('warn',  foreground='#dcdcaa')
        self._terminal.tag_config('info',  foreground='#4ec9b0')

    def _term_write(self, text, tag='recv'):
        self._terminal.configure(state=tk.NORMAL)
        self._terminal.insert(tk.END, text + '\n', tag)
        self._terminal.see(tk.END)
        self._terminal.configure(state=tk.DISABLED)

    # ── Traces & debounced plan update ────────────────────────────────────────

    def _setup_traces(self):
        keys = ['start', 'end', 'count', 'p_ax', 'dir_pos',
                'offset', 'probe_dist', 'feed_move', 'feed_probe',
                'fixed_X', 'fixed_Y', 'fixed_Z']
        for k in keys:
            self.vars[k].trace_add('write', lambda *_: self._schedule_plan_update())

    def _schedule_plan_update(self):
        if self._debounce:
            self.root.after_cancel(self._debounce)
        self._debounce = self.root.after(300, self._update_plan_plot)

    # ── Plan plot (static preview) ────────────────────────────────────────────

    def _build_prober_from_ui(self):
        """Construct and configure a GUIMarlinProber from current UI values."""
        l_ax  = self.vars['l_ax'].get()
        p_ax  = self.vars['p_ax'].get()
        fixed = {ax: float(self.vars[f'fixed_{ax}'].get())
                 for ax in ALL_AXES if ax != l_ax}

        prober = GUIMarlinProber(
            debug_mode  = self.vars['debug_mode'].get() == '1',
            log_cb      = lambda msg, tag='recv': self._msg_queue.put(('log', msg, tag)),
            result_cb   = lambda idx, a, r: self._msg_queue.put(('result', idx, a, r)),
            stop_event  = self._stop_event,
        )
        prober.configure(
            l_ax       = l_ax,
            start      = float(self.vars['start'].get()),
            end        = float(self.vars['end'].get()),
            count      = int(self.vars['count'].get()),
            fixed_vals = fixed,
            p_ax       = p_ax,
            dir_pos    = self.vars['dir_pos'].get() == '1',
            offset     = float(self.vars['offset'].get()),
            probe_dist = float(self.vars['probe_dist'].get()),
            host       = self.vars['host'].get(),
            feed_move  = float(self.vars['feed_move'].get()),
            feed_probe = float(self.vars['feed_probe'].get()),
        )
        return prober

    def _update_plan_plot(self):
        try:
            prober = self._build_prober_from_ui()
        except Exception:
            return  # Invalid input — skip redraw until values are corrected

        cfg    = prober.config
        points = prober.points
        l_ax   = cfg['l_ax']
        p_ax   = cfg['p_ax']

        if cfg['start'] == cfg['end']:
            xs    = list(range(1, cfg['count'] + 1))
            xlabel = "Point index"
        else:
            xs    = [pt[l_ax] for pt in points]
            xlabel = f"{l_ax} position (mm)"

        self._plan_xs     = xs
        self._plan_config = cfg

        targets  = [pt[p_ax] for pt in points]
        staging  = [pt[p_ax] - cfg['offset'] * cfg['dir_mult'] for pt in points]
        probe_tg = [pt[p_ax] + cfg['probe_dist'] * cfg['dir_mult'] for pt in points]

        # ── Top subplot: absolute plan ────────────────────────────────────────
        self._ax_plan.cla()
        self._ax_plan.scatter(xs, targets,  c='#e05252', s=40, zorder=4,
                              label='Probe reference')
        self._ax_plan.scatter(xs, staging,  c='#5588cc', s=30, marker='x',
                              zorder=3, label='Staging')
        self._ax_plan.scatter(xs, probe_tg, c='#888888', s=20, marker='.',
                              zorder=2, label='Probe target')

        # Draw vertical arrows from staging → probe target at each point
        for x, s_val, t_val in zip(xs, staging, probe_tg):
            self._ax_plan.annotate("", xy=(x, t_val), xytext=(x, s_val),
                arrowprops=dict(arrowstyle='->', color='#aaaaaa', lw=0.8))

        # Replay any already-collected results (e.g. after config change mid-run)
        if self._res_x:
            self._ax_plan.scatter(self._res_x, self._res_abs,
                                  c='#44bb44', s=35, zorder=5, label='Measured')

        self._ax_plan.set_title("Probing plan — absolute space")
        self._ax_plan.set_ylabel(f"{p_ax} (mm)")
        self._ax_plan.set_xlabel(xlabel)
        self._ax_plan.grid(True, alpha=0.3)
        self._ax_plan.legend(fontsize=7, loc='best')

        # ── Bottom subplot: deviation ─────────────────────────────────────────
        self._ax_dev.cla()
        self._live_line, = self._ax_dev.plot([], [], 'go-', linewidth=2,
                                              markersize=7, label='Deviation')
        self._ax_dev.axhline(0, color='black', linewidth=0.8, alpha=0.4)

        if self._res_x and self._res_rel:
            self._live_line.set_data(self._res_x, self._res_rel)
            self._ax_dev.relim()
            self._ax_dev.autoscale_view()

        x_min, x_max = min(xs), max(xs)
        margin = 1 if x_min == x_max else (x_max - x_min) * 0.1
        self._ax_dev.set_xlim(x_min - margin, x_max + margin)
        self._ax_dev.set_title("Results — relative deviation")
        self._ax_dev.set_xlabel(xlabel)
        self._ax_dev.set_ylabel("Deviation (mm)")
        self._ax_dev.grid(True, alpha=0.3)
        self._ax_dev.legend(fontsize=7, loc='best')

        self._canvas.draw_idle()

    # ── Execute / cancel ──────────────────────────────────────────────────────

    def _on_start(self):
        # Reset state
        self._stop_event.clear()
        self._res_x   = []
        self._res_abs = []
        self._res_rel = []
        self._update_plan_plot()

        try:
            self._prober = self._build_prober_from_ui()
        except Exception as e:
            self._term_write(f"[ERROR]: Bad configuration — {e}", 'error')
            return

        self._save_settings()
        self._btn_start.configure(state=tk.DISABLED)
        self._btn_cancel.configure(state=tk.NORMAL)

        self._run_thread = threading.Thread(target=self._prober.run, daemon=True)
        self._run_thread.start()

    def _on_cancel(self):
        self._stop_event.set()
        self._btn_cancel.configure(state=tk.DISABLED)

    # ── Queue polling (thread → GUI) ──────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                if msg[0] == 'log':
                    _, text, tag = msg
                    self._term_write(text, tag)
                    if msg[1] in ("[DONE]: Sequence complete.", ) or "DONE" in text:
                        self._on_run_finished()
                elif msg[0] == 'result':
                    _, idx, abs_val, rel_val = msg
                    self._on_result(idx, abs_val, rel_val)
        except queue.Empty:
            pass

        # Check if thread ended (covers cancel / error paths)
        if self._run_thread and not self._run_thread.is_alive():
            self._on_run_finished()
            self._run_thread = None

        self.root.after(100, self._poll_queue)

    def _on_result(self, idx, abs_val, rel_val):
        """Called from the main thread when a probe result arrives."""
        x = self._plan_xs[idx] if hasattr(self, '_plan_xs') and idx < len(self._plan_xs) else idx

        self._res_x.append(x)
        self._res_abs.append(abs_val)
        self._res_rel.append(rel_val)

        # Update plan subplot with measured point
        self._ax_plan.scatter([x], [abs_val], c='#44bb44', s=35, zorder=5)

        # Update deviation subplot
        self._live_line.set_data(self._res_x, self._res_rel)
        self._ax_dev.relim()
        self._ax_dev.autoscale_view()

        self._canvas.draw_idle()

    def _on_run_finished(self):
        self._btn_start.configure(state=tk.NORMAL)
        self._btn_cancel.configure(state=tk.DISABLED)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app  = ProberGUI(root)
    root.mainloop()