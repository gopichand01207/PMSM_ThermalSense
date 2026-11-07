"""
PMSM_ThermalSense – End-to-End Pipeline Runner
===============================================
Runs all pipeline stages in sequence:

  Stage 1  data/generate_data.py      – Synthetic 185-h motor telemetry
  Stage 2  features/feature_engineering.py – Physics feature engineering
  Stage 3  models/train_xgboost.py    – XGBoost virtual sensor training
  Stage 4  control/thermal_protection.py – Protection algorithm demo & plots

Usage
-----
  python run_pipeline.py              # Full pipeline
  python run_pipeline.py --skip-data  # Skip data generation (data already exists)
  python run_pipeline.py --stage 3    # Run only Stage 3 (model training)
"""

import os
import sys
import time
import argparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

STAGES = {
    1: ("Data Generation",           "data.generate_data"),
    2: ("Feature Engineering",       "features.feature_engineering"),
    3: ("XGBoost Model Training",    "models.train_xgboost"),
    4: ("Thermal Protection Demo",   "control.thermal_protection"),
}


def banner(text: str, width: int = 60):
    print("\n" + "═" * width)
    print(f"  {text}")
    print("═" * width)


def run_data_generation():
    from data.generate_data import generate_sessions, save_data
    df = generate_sessions()
    save_data(df, os.path.join(BASE_DIR, "data"))
    return df


def run_feature_engineering():
    import pandas as pd
    from features.feature_engineering import engineer_features

    raw_path  = os.path.join(BASE_DIR, "data", "motor_telemetry.parquet")
    feat_path = os.path.join(BASE_DIR, "data", "motor_telemetry_features.parquet")

    print("Loading raw telemetry …")
    df = pd.read_parquet(raw_path)
    print(f"  → {len(df):,} rows")

    print("Engineering physics features …")
    df_feat = engineer_features(df)
    df_feat.to_parquet(feat_path, index=False)
    print(f"✓ Feature dataset saved → {feat_path}")
    print(f"  Shape: {df_feat.shape}")
    return df_feat


def run_model_training():
    from models.train_xgboost import run_training
    return run_training()


def run_protection_demo():
    from control.thermal_protection import run_protection_demo
    return run_protection_demo()


STAGE_FUNCS = {
    1: run_data_generation,
    2: run_feature_engineering,
    3: run_model_training,
    4: run_protection_demo,
}


def main():
    parser = argparse.ArgumentParser(
        description="PMSM_ThermalSense – Pipeline Runner"
    )
    parser.add_argument("--skip-data", action="store_true",
                        help="Skip Stage 1 (data generation) if data already exists")
    parser.add_argument("--stage", type=int, choices=[1, 2, 3, 4],
                        help="Run only a single stage (1–4)")
    args = parser.parse_args()

    banner("PMSM ThermalSense – Virtual Temperature Sensor Pipeline")
    t_total = time.time()

    if args.stage:
        # Single-stage mode
        stages_to_run = [args.stage]
    else:
        stages_to_run = list(STAGES.keys())
        if args.skip_data:
            stages_to_run = [s for s in stages_to_run if s != 1]

    for stage_id in stages_to_run:
        name, _ = STAGES[stage_id]
        banner(f"Stage {stage_id}: {name}")
        t0 = time.time()
        try:
            STAGE_FUNCS[stage_id]()
            elapsed = time.time() - t0
            print(f"\n✓ Stage {stage_id} complete in {elapsed:.1f}s")
        except Exception as exc:
            print(f"\n✗ Stage {stage_id} FAILED: {exc}")
            raise

    total_elapsed = time.time() - t_total
    banner(f"Pipeline Complete  ({total_elapsed:.1f}s)")
    print("""
Next step → launch the dashboard:

    streamlit run app/dashboard.py

""")


if __name__ == "__main__":
    main()
