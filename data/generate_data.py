"""
PMSM_ThermalSense – Synthetic Motor Test-Bench Data Generator
=============================================================
Generates a realistic 185-hour (≈1.33 M row) dataset of PMSM
(Permanent-Magnet Synchronous Motor) test-bench measurements at 7.2 Hz,
covering multiple drive cycles with varying speed, load, and ambient
temperature profiles.

Output files
------------
data/motor_telemetry.parquet  – full dataset (Parquet, ~40 MB)
data/motor_telemetry.csv      – first 50 000 rows (CSV preview)

Columns
-------
timestamp_s          : float  – elapsed seconds since start of session
session_id           : int    – drive-cycle session index (1–37)
cycle_type           : str    – {urban, highway, wltp, constant_speed, transient}
ambient_temp_C       : float  – ambient / coolant-inlet temperature (°C)
speed_rpm            : float  – rotor mechanical speed (RPM)
torque_Nm            : float  – shaft torque (Nm)
i_d_A                : float  – d-axis current (A)
i_q_A                : float  – q-axis current (A)
v_d_V                : float  – d-axis voltage (V)
v_q_V                : float  – q-axis voltage (V)
i_s_A                : float  – stator current magnitude (A)  = sqrt(id²+iq²)
v_s_V                : float  – stator voltage magnitude (V)
motor_freq_Hz        : float  – electrical frequency (Hz)
pm_temp_C            : float  – rotor permanent-magnet temperature (°C)  ← TARGET
stator_winding_temp_C: float  – stator winding temperature (°C)
stator_yoke_temp_C   : float  – stator yoke temperature (°C)
coolant_temp_C       : float  – coolant outlet temperature (°C)
"""

import os
import numpy as np
import pandas as pd

# ── Reproducibility ────────────────────────────────────────────────────────────
RNG = np.random.default_rng(42)

# ── Motor constants ────────────────────────────────────────────────────────────
R_S        = 0.028        # stator resistance  (Ω)
L_D        = 2.65e-4      # d-axis inductance  (H)
L_Q        = 3.90e-4      # q-axis inductance  (H)
POLE_PAIRS = 4            # pole pairs
Ke         = 0.075        # back-EMF constant  (V·s/rad)
# Thermal resistance network (simplified 2-RC model)
R_PM_WIND  = 1.8          # thermal R: PM ↔ winding (K/W)
R_WIND_AMB = 3.5          # thermal R: winding ↔ ambient (K/W)
C_PM       = 420.0        # thermal capacitance of PM mass (J/K)
C_WIND     = 850.0        # thermal capacitance of winding (J/K)

# ── Drive-cycle definitions ────────────────────────────────────────────────────
CYCLE_TYPES = ["urban", "highway", "wltp", "constant_speed", "transient"]
SAMPLE_HZ   = 7.2         # test-bench acquisition rate
DT          = 1 / SAMPLE_HZ
TOTAL_HOURS = 185.0
TOTAL_STEPS = int(TOTAL_HOURS * 3600 * SAMPLE_HZ)   # ≈ 4,795,200


def _speed_profile(cycle: str, n_steps: int) -> np.ndarray:
    """Return shaft speed (RPM) for a given cycle type."""
    t = np.linspace(0, 1, n_steps)
    if cycle == "urban":
        base = 1800 + 1200 * np.sin(2 * np.pi * 0.03 * np.arange(n_steps) * DT)
        noise = RNG.normal(0, 60, n_steps)
        return np.clip(base + noise, 200, 4000)
    elif cycle == "highway":
        base = 4500 + 500 * np.sin(2 * np.pi * 0.008 * np.arange(n_steps) * DT)
        noise = RNG.normal(0, 80, n_steps)
        return np.clip(base + noise, 2000, 6000)
    elif cycle == "wltp":
        # WLTP-like: low-medium-high-extra-high phases
        phases = np.array_split(np.arange(n_steps), 4)
        spd = np.zeros(n_steps)
        targets = [1500, 2800, 4200, 5500]
        for ph, tgt in zip(phases, targets):
            spd[ph] = tgt + RNG.normal(0, 150, len(ph))
        return np.clip(spd, 100, 6500)
    elif cycle == "constant_speed":
        tgt = RNG.choice([2000, 3000, 4000, 5000])
        return np.clip(np.full(n_steps, tgt) + RNG.normal(0, 30, n_steps), 500, 6000)
    else:  # transient
        spd = np.zeros(n_steps)
        pos = 0
        while pos < n_steps:
            seg = RNG.integers(50, 400)
            tgt = RNG.uniform(500, 6000)
            end = min(pos + seg, n_steps)
            spd[pos:end] = np.linspace(spd[pos - 1] if pos > 0 else tgt, tgt, end - pos)
            pos = end
        return np.clip(spd + RNG.normal(0, 50, n_steps), 100, 6500)


def _torque_profile(cycle: str, speed: np.ndarray) -> np.ndarray:
    """Return shaft torque (Nm) correlated with speed & cycle."""
    if cycle in ("urban", "wltp", "transient"):
        base_torque = 15 + 0.004 * speed + RNG.normal(0, 5, len(speed))
    elif cycle == "highway":
        base_torque = 8 + 0.003 * speed + RNG.normal(0, 3, len(speed))
    else:
        base_torque = np.full(len(speed), 20.0) + RNG.normal(0, 2, len(speed))
    return np.clip(base_torque, 0, 180)


def _dq_currents(torque: np.ndarray, speed: np.ndarray) -> tuple:
    """Compute d- and q-axis currents from torque & speed (MTPA approximation)."""
    # q-axis current proportional to torque  (id = 0 for MTPA on non-salient SPMSM approx)
    i_q = torque / (1.5 * POLE_PAIRS * Ke)
    # small d-axis current for field weakening at high speed
    omega_e = speed * (2 * np.pi / 60) * POLE_PAIRS
    v_lim   = 300.0   # DC-link / sqrt(3)
    i_d     = np.where(omega_e > 0, np.clip(-Ke * omega_e / (v_lim + 1e-6), -80, 0), 0.0)
    return i_d.astype(np.float32), i_q.astype(np.float32)


def _voltages(i_d, i_q, speed):
    """Compute d/q axis voltages from current and speed (steady-state PMSM model)."""
    omega_e = speed * (2 * np.pi / 60) * POLE_PAIRS
    v_d = R_S * i_d - omega_e * L_Q * i_q
    v_q = R_S * i_q + omega_e * (L_D * i_d + Ke)
    return v_d.astype(np.float32), v_q.astype(np.float32)


def _simulate_thermal(i_s, speed, ambient, n_steps):
    """
    2-RC thermal network simulation.
    States: T_winding, T_pm
    Losses drive winding node; PM heats via conduction from winding.
    Returns T_pm, T_winding, T_yoke, T_coolant arrays.
    """
    omega_mech = speed * (np.pi / 30)
    # Copper loss  (W)
    P_copper = R_S * i_s ** 2
    # Iron loss  (simplified linear model: P_iron ∝ freq)
    freq_e   = POLE_PAIRS * omega_mech / (2 * np.pi)
    P_iron   = 0.12 * freq_e + 0.0003 * freq_e ** 2  # (W) stator-core loss

    T_winding = np.zeros(n_steps, dtype=np.float32)
    T_pm      = np.zeros(n_steps, dtype=np.float32)

    T_winding[0] = ambient[0] + 15.0
    T_pm[0]      = ambient[0] + 10.0

    for k in range(1, n_steps):
        Q_cu = float(P_copper[k])
        Q_fe = float(P_iron[k])
        Ta   = float(ambient[k])

        # Heat flows
        Q_wind_to_amb = (T_winding[k - 1] - Ta) / R_WIND_AMB
        Q_pm_to_wind  = (T_pm[k - 1] - T_winding[k - 1]) / R_PM_WIND

        dT_wind = DT / C_WIND * (Q_cu + Q_fe - Q_wind_to_amb + Q_pm_to_wind)
        dT_pm   = DT / C_PM  * (-Q_pm_to_wind + 0.3 * Q_cu)   # PM self-heat (eddy)

        T_winding[k] = T_winding[k - 1] + dT_wind
        T_pm[k]      = T_pm[k - 1] + dT_pm

    # Stator yoke ≈ 0.75 × winding rise + ambient
    T_yoke    = ambient + 0.75 * (T_winding - ambient)
    # Coolant outlet ≈ ambient + small rise
    T_coolant = ambient + 0.4 * (T_winding - ambient) + RNG.normal(0, 0.5, n_steps)

    # Add sensor noise
    T_pm      += RNG.normal(0, 0.3, n_steps)
    T_winding += RNG.normal(0, 0.5, n_steps)
    T_yoke    += RNG.normal(0, 0.4, n_steps)
    T_coolant += RNG.normal(0, 0.2, n_steps)

    return T_pm, T_winding, T_yoke.astype(np.float32), T_coolant.astype(np.float32)


def generate_sessions() -> pd.DataFrame:
    """
    Generate 37 test sessions totalling ≈185 hours.
    Returns assembled DataFrame.
    """
    target_steps = TOTAL_STEPS
    generated    = 0
    session_id   = 1
    chunks       = []

    print(f"Generating {TOTAL_HOURS:.0f}-hour motor telemetry …")

    while generated < target_steps:
        cycle  = RNG.choice(CYCLE_TYPES)
        # Session length: 2–8 hours
        hours  = RNG.uniform(2, 8)
        n      = int(hours * 3600 * SAMPLE_HZ)
        n      = min(n, target_steps - generated)

        ambient_base = RNG.uniform(15, 45)
        ambient      = ambient_base + RNG.normal(0, 1, n).cumsum() * 0.0005
        ambient      = np.clip(ambient, 10, 50).astype(np.float32)

        speed   = _speed_profile(cycle, n).astype(np.float32)
        torque  = _torque_profile(cycle, speed).astype(np.float32)
        i_d, i_q = _dq_currents(torque, speed)
        v_d, v_q = _voltages(i_d, i_q, speed)

        i_s = np.sqrt(i_d ** 2 + i_q ** 2).astype(np.float32)
        v_s = np.sqrt(v_d ** 2 + v_q ** 2).astype(np.float32)

        # Electrical frequency
        omega_e   = speed * (2 * np.pi / 60) * POLE_PAIRS
        freq_e    = (omega_e / (2 * np.pi)).astype(np.float32)

        T_pm, T_wind, T_yoke, T_cool = _simulate_thermal(i_s, speed, ambient, n)

        t_elapsed = (generated + np.arange(n)) / SAMPLE_HZ

        df = pd.DataFrame({
            "timestamp_s":           t_elapsed.astype(np.float32),
            "session_id":            np.full(n, session_id, dtype=np.int16),
            "cycle_type":            [cycle] * n,
            "ambient_temp_C":        ambient,
            "speed_rpm":             speed,
            "torque_Nm":             torque,
            "i_d_A":                 i_d,
            "i_q_A":                 i_q,
            "v_d_V":                 v_d,
            "v_q_V":                 v_q,
            "i_s_A":                 i_s,
            "v_s_V":                 v_s,
            "motor_freq_Hz":         freq_e,
            "pm_temp_C":             T_pm.astype(np.float32),
            "stator_winding_temp_C": T_wind.astype(np.float32),
            "stator_yoke_temp_C":    T_yoke,
            "coolant_temp_C":        T_cool,
        })

        chunks.append(df)
        generated  += n
        session_id += 1
        pct = generated / target_steps * 100
        print(f"  Session {session_id-1:>2d} | {cycle:<15s} | {hours:.1f}h | "
              f"PM max={T_pm.max():.1f}°C | {pct:.1f}% done")

    full = pd.concat(chunks, ignore_index=True)
    print(f"\n✓ Dataset: {len(full):,} rows × {len(full.columns)} cols  "
          f"({len(full)/SAMPLE_HZ/3600:.1f} hours)")
    return full


def save_data(df: pd.DataFrame, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    parquet_path = os.path.join(out_dir, "motor_telemetry.parquet")
    csv_path     = os.path.join(out_dir, "motor_telemetry_preview.csv")
    df.to_parquet(parquet_path, index=False)
    df.head(50_000).to_csv(csv_path, index=False)
    print(f"\n✓ Saved → {parquet_path}")
    print(f"✓ Preview → {csv_path} (first 50 k rows)")


if __name__ == "__main__":
    OUT_DIR = os.path.join(os.path.dirname(__file__))
    df = generate_sessions()
    save_data(df, OUT_DIR)

