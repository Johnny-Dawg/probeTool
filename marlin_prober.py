import time
import re
import random
import matplotlib
matplotlib.use('TkAgg') 
import matplotlib.pyplot as plt
import numpy as np

try:
    import telnetlib
except ImportError:
    pass

class MarlinProber:
    def __init__(self, debug_mode=True):
        self.host = ""
        self.points = []
        self.recipe = []
        self.abs_results = []
        self.rel_results = []
        self.config = {}
        self.debug_mode = debug_mode
        self.fig = None
        self.ax_abs = None
        self.ax_rel = None
        self.live_line = None

    def configure(self, l_ax, start, end, count, fixed_vals, p_ax, dir_pos, offset, probe_dist, host="", feed_move=2000, feed_probe=100):
        """Programmatic configuration entry point."""
        self.host = host
        dir_mult = 1 if dir_pos else -1
        
        self.config = {
            'l_ax': l_ax.upper(), 
            'p_ax': p_ax.upper(), 
            'dir_mult': dir_mult,
            'offset': offset, 
            'fixed_vals': fixed_vals, 
            'probe_dist': probe_dist,
            'start': start, 
            'end': end, 
            'count': count,
            'feed_move': feed_move,
            'feed_probe': feed_probe
        }

        # Generate point list
        line_space = np.linspace(start, end, count)
        self.points = []
        for val in line_space:
            pt = {self.config['l_ax']: val}
            pt.update(fixed_vals)
            self.points.append(pt)
        
        self._generate_gcode()

    def _generate_gcode(self):
        self.recipe = []
        p_ax = self.config['p_ax']
        feed_move = self.config['feed_move']
        feed_probe = self.config['feed_probe']
        for pt in self.points:
            staging_val = pt[p_ax] - (self.config['offset'] * self.config['dir_mult'])
            self.recipe.append(f"G0 {p_ax}{staging_val:.3f} F{feed_move}")
            move_cmd = "G0 " + " ".join([f"{k}{v:.3f}" for k, v in pt.items() if k != p_ax]) + f" F{feed_move}"
            self.recipe.append(move_cmd)
            target_probe = pt[p_ax] + (self.config['probe_dist'] * self.config['dir_mult'])
            self.recipe.append(f"G38.2 {p_ax}{target_probe:.3f} F{feed_probe}")
            self.recipe.append("M114 D")
            self.recipe.append(f"G0 {p_ax}{staging_val:.3f} F{feed_move}")

    def setup_visualization(self):
        plt.ion()
        self.fig, (self.ax_abs, self.ax_rel) = plt.subplots(2, 1, figsize=(10, 8))
        self.fig.canvas.manager.set_window_title("Marlin Probing Visualizer")

        l_ax, p_ax = self.config['l_ax'], self.config['p_ax']
        
        if self.config['start'] == self.config['end']:
            self.plot_x_coords = list(range(1, self.config['count'] + 1))
            xlabel = "Point Index"
        else:
            self.plot_x_coords = [p[l_ax] for p in self.points]
            xlabel = f"{l_ax} Position (mm)"

        # Absolute Plan
        target_vals = [p[p_ax] for p in self.points]
        staging_vals = [p[p_ax] - (self.config['offset'] * self.config['dir_mult']) for p in self.points]
        self.ax_abs.scatter(self.plot_x_coords, target_vals, c='red', label='Targets', zorder=3)
        self.ax_abs.scatter(self.plot_x_coords, staging_vals, c='blue', marker='x', label='Staging')
        self.ax_abs.set_title("Plan (Absolute Space)")
        self.ax_abs.set_ylabel(f"{p_ax} (mm)")
        self.ax_abs.grid(True)
        self.ax_abs.legend()

        # Relative Deviation
        self.live_line, = self.ax_rel.plot([], [], 'go-', linewidth=2, markersize=8, label='Deviation')
        self.ax_rel.axhline(0, color='black', linewidth=1, alpha=0.3)
        x_min, x_max = min(self.plot_x_coords), max(self.plot_x_coords)
        margin = 1 if x_min == x_max else (x_max - x_min) * 0.1
        self.ax_rel.set_xlim(x_min - margin, x_max + margin)
        self.ax_rel.set_title("Results (Relative)")
        self.ax_rel.set_xlabel(xlabel)
        self.ax_rel.set_ylabel("Deviation (mm)")
        self.ax_rel.grid(True)

        plt.tight_layout()
        plt.draw()
        plt.pause(0.1)

    def _simulate_marlin_response(self, cmd):
        if "M114 D" in cmd:
            p_ax = self.config['p_ax']
            base_val = self.config['fixed_vals'].get(p_ax, 0)
            # Simulated noise + slight tilt
            sim_val = base_val + (len(self.abs_results) * 0.02) + random.gauss(0, 0.01)
            # Match the real Marlin M114 D output format:
            # "Logical: X: 0.000 Y: 3.430 Z: 0.000"
            axis_vals = {ax: (sim_val if ax == p_ax else 0.0) for ax in ['X', 'Y', 'Z']}
            logical = " ".join(f"{ax}: {v:.3f}" for ax, v in axis_vals.items())
            return (
                f"X:0.00 Y:0.00 Z:0.00 Count X:0 Y:0 Z:0\n"
                f"Logical: {logical}\n"
                f"ok\n"
            )
        return "ok\n"

    def run(self):
        tn = None
        if not self.debug_mode:
            tn = telnetlib.Telnet(self.host, 23, timeout=5)

        point_index = 0
        x_plot, y_plot = [], []

        for cmd in self.recipe:
            if self.debug_mode:
                response = self._simulate_marlin_response(cmd)
                print(f"[SIM SEND]: {cmd}\n[SIM RECV]: {response.strip()}")
            else:
                tn.write(f"{cmd}\n".encode('ascii'))
                response = ""
                # Read lines until we get an 'ok' acknowledgement or a kill signal.
                # Marlin signals a kill/emergency-stop with a line starting with '!!'
                # (e.g. "!! KILL called!"). The matching G-code command to trigger
                # this from the host side is M112 (Emergency Stop).
                while "ok" not in response.lower():
                    line = tn.read_until(b"\n", timeout=5).decode('ascii')
                    response += line
                    if line.startswith("!!"):
                        print(f"[KILL SIGNAL]: {line.strip()}")
                        print("[ABORT]: Marlin kill signal received — stopping command sequence.")
                        print("[INFO]: Send M112 to trigger an emergency stop from the host.")
                        if tn:
                            tn.close()
                        raise RuntimeError(
                            f"Marlin kill signal detected: '{line.strip()}'. "
                            "Sequence aborted. Use M112 to command an emergency stop."
                        )
                print(f"[SEND]: {cmd}\n[RECV]: {response.strip()}")

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
                    
                    x_plot.append(self.plot_x_coords[point_index])
                    y_plot.append(rel_val)
                    
                    self.live_line.set_data(x_plot, y_plot)
                    self.ax_rel.relim()
                    self.ax_rel.autoscale_view()
                    self.ax_abs.scatter(x_plot[-1], abs_val, color='green', s=30, alpha=0.6)
                    
                    plt.draw()
                    plt.pause(0.1)
                    point_index += 1

        if tn: tn.close()
        plt.ioff()
        print("Sequence complete.")
        plt.show()