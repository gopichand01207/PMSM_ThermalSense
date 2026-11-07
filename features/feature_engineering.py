"""
PMSM_ThermalSense – Physics-Based Feature Engineering
======================================================
Derives thermal-physics features from raw motor telemetry:

  1. Copper loss   (I²R heating – dominant loss source)
  2. Iron / core loss   (frequency-dependent eddy-current + hysteresis)
  3. Total electrical loss (heat source)
  4. Apparent power & power factor proxy
  5. Field-weakening depth (|id| / |is|)
  6. Thermal lag features – rolling windows capture heat accumulation
  7. Rate-of-change features – dI/dt, dN/dt (thermal stress indicators)
  8. Interaction feature – speed × current² (combined thermal load)
  9. Coolant delta-T (heat rejection capacity)
 10. Duty-cycle proxy – high-load fraction in rolling window

All features are grounded in the PMSM thermal model from Wallscheid & Böcker
(2017) and the UCI Electric Motor Temperature dataset methodology.
"""

import os
import numpy as np
import pandas as pd

# ── Motor constants (must match generate_data.py) ─────────────────────────────
R_S        = 0.028        # stator resistance  (Ω)
POLE_PAIRS = 4

# ── Rolling-window sizes ──────────────────────────────────────────────────────
SAMPLE_HZ  = 7.2
WIN_SHORT  = int(10  * SAMPLE_HZ)   #  10 s  – fast transient
WIN_MEDIUM = int(60  * SAMPLE_HZ)   #  60 s  – thermal lag
WIN_LONG   = int(300 * SAMPLE_HZ)   # 300 s  – heat accumulation

HIGH_LOAD_THRESHOLD = 100.0          # i_s threshold for "high load" flag (A)


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add physics-based thermal features to the raw telemetry DataFrame.
    Assumes data is already sorted by timestamp within each session.
    """
    df = df.copy()
    df = df.sort_values(["session_id", "timestamp_s"]).reset_index(drop=True)

    # ── 1. Copper loss  P_cu = R_s × I_s²  (W) ───────────────────────────────
    df["P_copper_W"] = R_S * df["i_s_A"] ** 2

    # ── 2. Iron/core loss  (Bertotti model, simplified) ───────────────────────
    #   P_iron ≈ k_h × f + k_e × f²   (k_h=0.12, k_e=3e-4)
    df["P_iron_W"] = 0.12 * df["motor_freq_Hz"] + 3e-4 * df["motor_freq_Hz"] ** 2

    # ── 3. Total electrical loss ───────────────────────────────────────────────
    df["P_loss_total_W"] = df["P_copper_W"] + df["P_iron_W"]

    # ── 4. Apparent power & power factor proxy ────────────────────────────────
    df["apparent_power_VA"] = df["v_s_V"] * df["i_s_A"]
    df["pf_proxy"]          = np.where(
        df["apparent_power_VA"] > 1,
        df["P_loss_total_W"] / df["apparent_power_VA"],
        0.0,
    )

    # ── 5. Field-weakening depth ──────────────────────────────────────────────
    df["fw_depth"] = np.where(
        df["i_s_A"] > 1e-3,
        df["i_d_A"].abs() / (df["i_s_A"] + 1e-6),
        0.0,
    )

    # ── 6-7. Rolling thermal-lag + rate-of-change features (per session) ────────
    # Process each session independently with a plain loop + concat to avoid
    # pandas 2.x groupby().apply() promoting session_id into the index.
    session_chunks = []
    for sid, grp in df.groupby("session_id", sort=False):
        g = grp.copy()
        # Rolling thermal-lag windows
        for col in ["P_loss_total_W", "P_copper_W", "i_s_A", "speed_rpm"]:
            for win, tag in [(WIN_SHORT, "10s"), (WIN_MEDIUM, "60s"), (WIN_LONG, "300s")]:
                g[f"{col}_mean_{tag}"] = (
                    g[col].rolling(win, min_periods=1).mean().astype(np.float32)
                )
        # Cumulative energy in session (Joules)
        g["cum_loss_J"] = (g["P_loss_total_W"] / SAMPLE_HZ).cumsum().astype(np.float32)
        # Rate-of-change (within session so boundary doesn't bleed across sessions)
        g["di_s_dt"]    = g["i_s_A"].diff().fillna(0).astype(np.float32) * SAMPLE_HZ
        g["dspeed_dt"]  = g["speed_rpm"].diff().fillna(0).astype(np.float32) * SAMPLE_HZ
        g["dP_loss_dt"] = g["P_loss_total_W"].diff().fillna(0).astype(np.float32) * SAMPLE_HZ
        # High-load duty cycle
        g["high_load_flag"]     = (g["i_s_A"] > HIGH_LOAD_THRESHOLD).astype(np.float32)
        g["high_load_duty_60s"] = (
            g["high_load_flag"].rolling(WIN_MEDIUM, min_periods=1).mean().astype(np.float32)
        )
        session_chunks.append(g)

    df = pd.concat(session_chunks, ignore_index=True)   # integer index, all cols preserved

    # ── 8. Interaction features ───────────────────────────────────────────────
    df["speed_x_I2"]     = df["speed_rpm"] * df["i_s_A"] ** 2
    df["torque_x_speed"] = df["torque_Nm"] * df["speed_rpm"] / 9550

    # ── 9. Coolant delta-T ────────────────────────────────────────────────────
    df["coolant_dT"] = df["coolant_temp_C"] - df["ambient_temp_C"]

    # ── 10. Stator winding temperature (observable in real system) ────────────
    #    Used as a conditioning feature – this IS measurable via thermistor.
    #    PM temp is NOT directly measurable → that is our prediction target.
    # (column already present as stator_winding_temp_C)

    return df


def get_feature_columns() -> list:
    """Return ordered list of model input features."""
    base = [
        "ambient_temp_C",
        "speed_rpm",
        "torque_Nm",
        "i_d_A",
        "i_q_A",
        "i_s_A",
        "v_d_V",
        "v_q_V",
        "v_s_V",
        "motor_freq_Hz",
        "stator_winding_temp_C",
        "stator_yoke_temp_C",
        "coolant_temp_C",
        # Physics features
        "P_copper_W",
        "P_iron_W",
        "P_loss_total_W",
        "apparent_power_VA",
        "pf_proxy",
        "fw_depth",
        # Rolling thermal-lag features
        "P_loss_total_W_mean_10s",
        "P_loss_total_W_mean_60s",
        "P_loss_total_W_mean_300s",
        "P_copper_W_mean_10s",
        "P_copper_W_mean_60s",
        "P_copper_W_mean_300s",
        "i_s_A_mean_10s",
        "i_s_A_mean_60s",
        "i_s_A_mean_300s",
        "speed_rpm_mean_10s",
        "speed_rpm_mean_60s",
        "speed_rpm_mean_300s",
        "cum_loss_J",
        # Rate-of-change
        "di_s_dt",
        "dspeed_dt",
        "dP_loss_dt",
        # Interaction
        "speed_x_I2",
        "torque_x_speed",
        # Coolant
        "coolant_dT",
        # Duty cycle
        "high_load_duty_60s",
    ]
    return base


TARGET_COL = "pm_temp_C"


if __name__ == "__main__":
    import sys
    DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
    parquet  = os.path.join(DATA_DIR, "motor_telemetry.parquet")

    if not os.path.exists(parquet):
        print("ERROR: Run data/generate_data.py first to create motor_telemetry.parquet")
        sys.exit(1)

    print("Loading telemetry …")
    df = pd.read_parquet(parquet)
    print(f"  → {len(df):,} rows")

    print("Engineering features …")
    df_feat = engineer_features(df)

    out = os.path.join(DATA_DIR, "motor_telemetry_features.parquet")
    df_feat.to_parquet(out, index=False)
    print(f"✓ Feature dataset saved → {out}")
    print(f"  Shape: {df_feat.shape}  |  Features: {len(get_feature_columns())}")

