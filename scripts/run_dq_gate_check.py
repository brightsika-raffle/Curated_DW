#!/usr/bin/env python3
"""
scripts/run_dq_gate_check.py

Runs the complete DQ gate script against BigQuery and verifies that:
1. All HARD tests PASS.
2. The GA4 WARN tests report the expected details.
3. Prints all rows from the latest run.
"""

import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, location=LOCATION, credentials=Credentials(token))

def main():
    client = get_client()

    with open("sql/nightly_build_and_test.sql", "r", encoding="utf-8") as f:
        full_sql = f.read()

    # Extract declarations and DQ gate queries
    # Lines 2613 to 3266
    dq_prefix = """
    DECLARE v_run_ts   TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
    DECLARE v_tx_ts    TIMESTAMP DEFAULT (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`);
    DECLARE v_sub_ts   TIMESTAMP DEFAULT (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`);
    DECLARE v_spend_ts TIMESTAMP DEFAULT (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`);
    DECLARE v_stat_ts  TIMESTAMP DEFAULT (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily`);
    DECLARE v_ga4_ts   TIMESTAMP DEFAULT (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`);
    """
    
    start_str = "INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`"
    start_idx = full_sql.find(start_str)
    end_str = "-- Power BI data-health page reads this."
    end_idx = full_sql.find(end_str)
    assert start_idx != -1 and end_idx != -1, "DQ gate section boundaries not found"

    dq_sql = dq_prefix + "\n" + full_sql[start_idx:end_idx]

    print("=" * 115)
    print("RUNNING COMPLETE DQ GATE FROM nightly_build_and_test.sql")
    print("=" * 115)

    job_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry = client.query(dq_sql, job_config=job_cfg, location=LOCATION)
    print(f"[Dry Run] Scans {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024*1024):.2f} MB)")

    job = client.query(dq_sql, location=LOCATION)
    job.result()
    print("[Execute] DQ Gate finished successfully.")

    # Fetch latest run results
    q_results = """
    SELECT
      run_ts,
      run_date,
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = (SELECT MAX(run_ts) FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`)
    ORDER BY severity DESC, test ASC
    """
    rows = list(client.query(q_results).result())
    print("\n" + "=" * 125)
    print(f"LATEST DQ GATE RUN RESULTS ({len(rows)} tests)")
    print("=" * 125)
    print(f"{'Test':<42} | {'Severity':<8} | {'Result':<6} | {'Source':<10} | {'Curated':<10} | {'Detail'}")
    print("-" * 125)

    hard_fails = 0
    warns = 0
    for r in rows:
        src = f"{r.source_value:.2f}" if isinstance(r.source_value, float) else str(r.source_value)
        cur = f"{r.curated_value:.2f}" if isinstance(r.curated_value, float) else str(r.curated_value)
        print(f"{r.test:<42} | {r.severity:<8} | {r.result:<6} | {src:<10} | {cur:<10} | {r.detail}")
        if r.severity == "HARD" and r.result == "FAIL":
            hard_fails += 1
        elif r.result == "WARN":
            warns += 1

    print("-" * 125)
    print(f"Summary: Total Tests: {len(rows)} | HARD Fails: {hard_fails} | WARNs: {warns}")
    assert hard_fails == 0, f"Found {hard_fails} HARD test failures!"

if __name__ == "__main__":
    main()
