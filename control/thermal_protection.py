"""
PMSM_ThermalSense – Thermal Protection Control Algorithm
=========================================================
Implements the physics-based thermal-protection controller that:

  1. Monitors the virtual PM temperature from the XGBoost sensor.
  2. Triggers a protection response at T_PM ≥ 115 °C (Warning threshold).
  3. Linearly de-rates peak motor current (I_peak) between 115 °C and 130 °C.
  4. Performs a hard current-limit cut to I_safe_min if T_PM ≥ 130 °C (Danger).
  5. Logs all protection events with timestamps, temperature, and I_limit.

Design rationale
----------------
  • Below 115 °C  → Full current available (no de-rating)
  • 115–130 °C    → Linear de-rate:  I_lim = I_max × (1 − slope × ΔT)
  • Above 130 °C  → Hard limit:       I_lim = I_safe_min
                     (prevents irreversible demagnetisation of NdFeB magnets)

This mirrors OEM thermal management strategies (Bosch EM, Tesla Model 3
motor thermal derating curves) adapted for PMSM with rotor PM monitoring.
"""

import os
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLOTS_DIR = os.path.join(BASE_DIR, "plots")
os.makedirs(PLOTS_DIR, exist_ok=True)

# ── Protection thresholds ─────────────────────────────────────────────────────
T_WARNING_C   = 115.0   # °C – begin current de-rating
T_DANGER_C    = 130.0   # °C – hard current limit (magnet damage onset)
T_DERATING_RANGE = T_DANGER_C - T_WARNING_C  # 15 °C

# ── Current limits (A, peak phase current) ───────────────────────────────────
I_MAX_A      = 300.0    # maximum rated peak current
I_SAFE_MIN_A = 60.0     # minimum safe current during hard limit
# Linear de-rate slope: drops from I_MAX to I_SAFE_MIN over 15 °C
I_DERATE_SLOPE = (I_MAX_A - I_SAFE_MIN_A) / T_DERATING_RANGE  # A per °C


class ThermalProtectionController:
    """
    Stateless vectorised thermal-protection algorithm.
    Can be called per time-step or applied to a full telemetry array.
    """

    def __init__(
        self,
        t_warning: float = T_WARNING_C,
        t_danger:  float = T_DANGER_C,
        i_max:     float = I_MAX_A,
        i_safe_min: float = I_SAFE_MIN_A,
    ):
        self.t_warning   = t_warning
        self.t_danger    = t_danger
        self.i_max       = i_max
        self.i_safe_min  = i_safe_min
        self.derate_slope = (i_max - i_safe_min) / (t_danger - t_warning)

    def compute_current_limit(self, pm_temp: np.ndarray) -> np.ndarray:
        """
        Vectorised current-limit computation.

        Parameters
        ----------
        pm_temp : np.ndarray – PM temperature array (°C)

        Returns
        -------
        i_limit : np.ndarray – allowed peak current (A)
        """
        pm_temp  = np.asarray(pm_temp, dtype=np.float64)
        i_limit  = np.full_like(pm_temp, self.i_max)

        # Warning zone: linear de-rate
        warn_mask = (pm_temp >= self.t_warning) & (pm_temp < self.t_danger)
        delta_T   = pm_temp[warn_mask] - self.t_warning
        i_limit[warn_mask] = self.i_max - self.derate_slope * delta_T

        # Danger zone: hard limit
        danger_mask = pm_temp >= self.t_danger
        i_limit[danger_mask] = self.i_safe_min

        return i_limit

    def protection_state(self, pm_temp: np.ndarray) -> np.ndarray:
        """
        Returns categorical protection state per time-step.
          0 → NORMAL   (T < 115°C)
          1 → WARNING  (115 ≤ T < 130°C, de-rating active)
          2 → DANGER   (T ≥ 130°C, hard limit)
        """
        pm_temp = np.asarray(pm_temp, dtype=np.float64)
        state   = np.zeros(len(pm_temp), dtype=np.int8)
        state[(pm_temp >= self.t_warning) & (pm_temp < self.t_danger)] = 1
        state[pm_temp >= self.t_danger] = 2
        return state

    def apply_to_telemetry(self, df: pd.DataFrame,
                           temp_col: str = "pm_temp_pred_C") -> pd.DataFrame:
        """
        Apply thermal protection to a telemetry DataFrame.

        Adds columns:
          protection_state   – 0/1/2 (NORMAL / WARNING / DANGER)
          i_limit_A          – computed peak current limit (A)
          protection_active  – bool flag
          derated_pct        – how much current was de-rated (%)
        """
        df = df.copy()
        pm_arr = df[temp_col].values

        df["protection_state"]  = self.protection_state(pm_arr)
        df["i_limit_A"]         = self.compute_current_limit(pm_arr)
        df["protection_active"] = df["protection_state"] > 0
        df["derated_pct"]       = (1 - df["i_limit_A"] / self.i_max) * 100.0
        return df


# ── Simulation helper ─────────────────────────────────────────────────────────

def simulate_protection_scenario(n_steps: int = 5_000,
                                  sample_hz: float = 7.2) -> pd.DataFrame:
    """
    Simulate a stress drive-cycle where PM temperature rises from 50°C to 145°C
    and back down, to demonstrate the protection algorithm behaviour.
    """
    rng = np.random.default_rng(42)
    t   = np.arange(n_steps) / sample_hz  # seconds

    # Synthetic PM temperature: sinusoidal rise with noise
    T_ambient = 30.0
    T_rise    = 95 * (1 - np.exp(-t / 450))          # exponential heat-up
    T_osc     = 15 * np.sin(2 * np.pi * t / 300)     # load oscillation
    noise     = rng.normal(0, 0.5, n_steps)
    pm_temp   = T_ambient + T_rise + T_osc + noise

    ctrl = ThermalProtectionController()
    df = pd.DataFrame({
        "time_s":        t,
        "pm_temp_pred_C": pm_temp,
    })
    df = ctrl.apply_to_telemetry(df, temp_col="pm_temp_pred_C")
    return df


# ── Reporting ──────────────────────────────────────────────────────────────────

def protection_summary(df: pd.DataFrame, sample_hz: float = 7.2) -> dict:
    """Compute summary statistics of protection events."""
    total_time    = len(df) / sample_hz
    warn_time     = (df["protection_state"] == 1).sum() / sample_hz
    danger_time   = (df["protection_state"] == 2).sum() / sample_hz
    max_temp      = df["pm_temp_pred_C"].max()
    max_derate    = df["derated_pct"].max()
    events_warn   = int(((df["protection_state"] == 1).astype(int).diff() == 1).sum())
    events_danger = int(((df["protection_state"] == 2).astype(int).diff() == 1).sum())

    summary = {
        "total_time_s":       total_time,
        "warning_time_s":     warn_time,
        "danger_time_s":      danger_time,
        "warning_pct":        warn_time / total_time * 100,
        "danger_pct":         danger_time / total_time * 100,
        "max_pm_temp_C":      max_temp,
        "max_derate_pct":     max_derate,
        "warning_events":     events_warn,
        "danger_events":      events_danger,
    }
    return summary


# ── Plotting ──────────────────────────────────────────────────────────────────

def plot_protection_scenario(df: pd.DataFrame, out_dir: str = PLOTS_DIR):
    t_min = df["time_s"] / 60

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(13, 10), sharex=True)

    # ── Panel 1: PM temperature with protection zones ────────────────────────
    ax1.plot(t_min, df["pm_temp_pred_C"], lw=1.5, color="#2c3e50",
             label="Virtual PM Temperature", alpha=0.9)
    ax1.axhline(T_WARNING_C, color="#f39c12", lw=1.5, linestyle="--",
                label=f"Warning: {T_WARNING_C}°C")
    ax1.axhline(T_DANGER_C,  color="#c0392b", lw=1.5, linestyle="--",
                label=f"Danger:  {T_DANGER_C}°C")

    ax1.fill_between(t_min, T_WARNING_C, T_DANGER_C, alpha=0.12,
                     color="#f39c12", label="De-rating zone")
    ax1.fill_between(t_min,
                     T_DANGER_C,
                     df["pm_temp_pred_C"].clip(lower=T_DANGER_C),
                     alpha=0.15, color="#e74c3c", label="Hard-limit zone")

    ax1.set_ylabel("PM Temperature (°C)", fontsize=11)
    ax1.set_title("PMSM Thermal-Protection Control Algorithm – Stress Scenario",
                  fontsize=13, fontweight="bold")
    ax1.legend(fontsize=9, loc="upper left")
    ax1.set_ylim(20, 155)

    # ── Panel 2: Current limit ────────────────────────────────────────────────
    ax2.plot(t_min, df["i_limit_A"], lw=1.5, color="#e74c3c",
             label="I_limit (A)", alpha=0.9)
    ax2.axhline(I_MAX_A,      color="#2ecc71", lw=1, linestyle=":", alpha=0.7,
                label=f"I_max = {I_MAX_A:.0f} A (full current)")
    ax2.axhline(I_SAFE_MIN_A, color="#c0392b", lw=1, linestyle=":", alpha=0.7,
                label=f"I_min = {I_SAFE_MIN_A:.0f} A (hard limit)")
    ax2.fill_between(t_min, df["i_limit_A"], I_MAX_A,
                     alpha=0.15, color="#e74c3c", label="De-rated headroom")
    ax2.set_ylabel("Peak Current Limit (A)", fontsize=11)
    ax2.legend(fontsize=9, loc="upper right")
    ax2.set_ylim(0, I_MAX_A * 1.1)

    # ── Panel 3: Protection state ─────────────────────────────────────────────
    colors = {0: "#2ecc71", 1: "#f39c12", 2: "#e74c3c"}
    color_arr = [colors[s] for s in df["protection_state"].values]
    ax3.bar(t_min, [1] * len(df), width=1 / (60 * 7.2),
            color=color_arr, linewidth=0, align="edge")
    patches = [
        mpatches.Patch(color="#2ecc71", label="NORMAL"),
        mpatches.Patch(color="#f39c12", label="WARNING – de-rating"),
        mpatches.Patch(color="#e74c3c", label="DANGER – hard limit"),
    ]
    ax3.legend(handles=patches, fontsize=9, loc="upper left")
    ax3.set_yticks([])
    ax3.set_xlabel("Time (min)", fontsize=11)
    ax3.set_ylabel("Protection State", fontsize=11)

    plt.tight_layout()
    path = os.path.join(out_dir, "thermal_protection_scenario.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  [plot] {path}")


def plot_derate_curve(out_dir: str = PLOTS_DIR):
    """Static de-rating curve: PM temperature vs allowed peak current."""
    ctrl  = ThermalProtectionController()
    temps = np.linspace(80, 145, 500)
    ilims = ctrl.compute_current_limit(temps)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(temps, ilims, lw=2.5, color="#e74c3c")
    ax.axvline(T_WARNING_C, color="#f39c12", lw=1.5, linestyle="--",
               label=f"Warning threshold: {T_WARNING_C}°C")
    ax.axvline(T_DANGER_C,  color="#c0392b", lw=1.5, linestyle="--",
               label=f"Danger threshold: {T_DANGER_C}°C")
    ax.fill_betweenx([0, I_MAX_A * 1.05], T_WARNING_C, T_DANGER_C,
                     alpha=0.1, color="#f39c12")
    ax.fill_betweenx([0, I_MAX_A * 1.05], T_DANGER_C, 145,
                     alpha=0.1, color="#e74c3c")
    ax.set_xlabel("Rotor PM Temperature (°C)", fontsize=12)
    ax.set_ylabel("Peak Current Limit (A)", fontsize=12)
    ax.set_title("PMSM Thermal De-Rating Curve\n"
                 "(Linear de-rate 115→130°C | Hard limit ≥130°C)", fontsize=12)
    ax.legend(fontsize=10)
    ax.set_xlim(80, 145)
    ax.set_ylim(0, I_MAX_A * 1.05)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    path = os.path.join(out_dir, "derate_curve.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  [plot] {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_protection_demo():
    print("=" * 55)
    print("  Thermal Protection Control Algorithm – Demo")
    print("=" * 55)

    print("\nSimulating stress drive-cycle …")
    df = simulate_protection_scenario(n_steps=8_000)

    summary = protection_summary(df)
    print(f"\n{'─'*45}")
    print(f"  Total scenario time  : {summary['total_time_s']:.0f} s")
    print(f"  Time in WARNING zone : {summary['warning_time_s']:.0f} s  "
          f"({summary['warning_pct']:.1f}%)")
    print(f"  Time in DANGER zone  : {summary['danger_time_s']:.0f} s  "
          f"({summary['danger_pct']:.1f}%)")
    print(f"  Max PM temperature   : {summary['max_pm_temp_C']:.1f} °C")
    print(f"  Max current de-rate  : {summary['max_derate_pct']:.1f}%")
    print(f"  Warning events       : {summary['warning_events']}")
    print(f"  Danger events        : {summary['danger_events']}")
    print(f"{'─'*45}\n")

    print("Generating plots …")
    plot_protection_scenario(df)
    plot_derate_curve()

    # Save event log
    events = df[df["protection_active"]].copy()
    log_path = os.path.join(BASE_DIR, "saved_models", "protection_events.csv")
    events.to_csv(log_path, index=False)
    print(f"✓ Protection event log saved → {log_path}")

    return df, summary


if __name__ == "__main__":
    run_protection_demo()

