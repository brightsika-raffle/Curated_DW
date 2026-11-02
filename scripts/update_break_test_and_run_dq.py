"""
scripts/update_break_test_and_run_dq.py

1. Updates ga4_reliable_from_break to be bounded to:
   d BETWEEN DATE '2026-10-19' AND DATE '2026-10-31'
   with description: 'Zero days in break window (2026-10-19 to 2026-10-31) have ga4_sessions_reliable = true'
2. Re-runs the full DQ gate check inserting into dq_gate_results.
3. Prints the DQ gate summary by severity and result, and individual test results.
"""

import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

PROJECT_ID = "rh-ga4-491819"
LOCATION = "europe-west2"
SERVICE_ACCOUNT = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SERVICE_ACCOUNT}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    creds = Credentials(token)
    return bigquery.Client(project=PROJECT_ID, credentials=creds, location=LOCATION)

def update_sql_file():
    sql_file = "sql/nightly_build_and_test.sql"
    with open(sql_file, "r", encoding="utf-8") as f:
        content = f.read()

    old_cte = """ga4_break_quality AS (
  SELECT COUNTIF(d >= DATE '2026-10-19' AND ga4_sessions_reliable) AS true_after_break_count
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
),"""
    new_cte = """ga4_break_quality AS (
  SELECT COUNTIF(d BETWEEN DATE '2026-10-19' AND DATE '2026-10-31' AND ga4_sessions_reliable) AS true_in_break_window
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
),"""

    old_test = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_from_break', 'HARD',
               IF(true_after_break_count = 0, 'PASS', 'FAIL'), 0, true_after_break_count,
               'Zero days from 2026-10-19 onwards have ga4_sessions_reliable = true' FROM ga4_break_quality"""
    new_test = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_from_break', 'HARD',
               IF(true_in_break_window = 0, 'PASS', 'FAIL'), 0, true_in_break_window,
               'Zero days in break window (2026-10-19 to 2026-10-31) have ga4_sessions_reliable = true' FROM ga4_break_quality"""

    if old_cte in content:
        content = content.replace(old_cte, new_cte, 1)
        print("Updated CTE in sql/nightly_build_and_test.sql")
    else:
        print("Warning: old_cte not found in sql/nightly_build_and_test.sql")

    if old_test in content:
        content = content.replace(old_test, new_test, 1)
        print("Updated test statement in sql/nightly_build_and_test.sql")
    else:
        print("Warning: old_test not found in sql/nightly_build_and_test.sql")

    with open(sql_file, "w", encoding="utf-8") as f:
        f.write(content)
    print("Saved updated sql/nightly_build_and_test.sql")

def run_dq_gate(client):
    print("\n--- Running Full DQ Gate Check ---")
    with open("sql/nightly_build_and_test.sql", "r", encoding="utf-8") as f:
        full_sql = f.read()

    # Extract Section 17 (DQ gate)
    # Starts at '-- SECTION 17 — dq_gate_results' or 'DECLARE v_run_ts'
    sec17_marker = "-- =====================================================================\n-- SECTION 17 — dq_gate_results"
    idx = full_sql.find(sec17_marker)
    if idx == -1:
        # try finding DECLARE v_run_ts
        idx = full_sql.find("DECLARE v_run_ts TIMESTAMP")

    # We want everything from idx up to the end of the script (or ASSERT)
    sec17_sql = full_sql[idx:]
    # Remove ASSERT at the end so it finishes and lets us inspect
    assert_idx = sec17_sql.find("ASSERT NOT EXISTS")
    if assert_idx != -1:
        sec17_sql = sec17_sql[:assert_idx]

    # Dry run
    job_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry = client.query(sec17_sql, job_config=job_cfg, location=LOCATION)
    bytes_scanned = dry.total_bytes_processed or 0
    print(f"DQ Gate Dry-run: {bytes_scanned:,} bytes ({bytes_scanned / (1024*1024):.2f} MB)")

    # Execute
    exec_cfg = bigquery.QueryJobConfig(use_query_cache=False)
    job = client.query(sec17_sql, job_config=exec_cfg, location=LOCATION)
    job.result()
    print("Full DQ Gate executed successfully.")

def print_summary(client):
    sql_summary = """
    WITH latest_run AS (
      SELECT MAX(run_ts) AS max_ts
      FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    )
    SELECT
      severity,
      result,
      COUNT(*) AS count
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    JOIN latest_run ON run_ts = max_ts
    GROUP BY severity, result
    ORDER BY severity, result;
    """
    print("\n=======================================================")
    print("DQ GATE SUMMARY BY SEVERITY AND RESULT")
    print("=======================================================")
    rows = list(client.query(sql_summary).result())
    print("severity\tresult\tcount")
    for r in rows:
        print(f"{r['severity']}\t{r['result']}\t{r['count']}")

    sql_details = """
    WITH latest_run AS (
      SELECT MAX(run_ts) AS max_ts
      FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    )
    SELECT
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    JOIN latest_run ON run_ts = max_ts
    ORDER BY severity, result DESC, test ASC;
    """
    print("\n=======================================================")
    print("ALL TESTS IN LATEST RUN")
    print("=======================================================")
    details = list(client.query(sql_details).result())
    for d in details:
        print(f"[{d['severity']}] {d['test']}: {d['result']} (source={d['source_value']}, curated={d['curated_value']}) - {d['detail']}")

def main():
    update_sql_file()
    client = get_client()
    run_dq_gate(client)
    print_summary(client)

if __name__ == "__main__":
    main()

