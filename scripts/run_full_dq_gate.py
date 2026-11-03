import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

sys.stdout.reconfigure(encoding='utf-8')

PROJECT_ID = "rh-ga4-491819"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_client():
    token = subprocess.check_output(
        f"gcloud auth print-access-token --impersonate-service-account={SA}",
        shell=True, text=True
    ).strip()
    creds = Credentials(token)
    return bigquery.Client(project=PROJECT_ID, location="europe-west2", credentials=creds)

def main():
    client = get_client()

    # Clean up the single stray test from 09:04
    cleanup_sql = """
    DELETE FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = TIMESTAMP('2026-11-03 09:04:36.984813+00:00')
    """
    client.query(cleanup_sql).result()
    print("Cleaned up stray test row.")

    # Read the full DQ gate block from nightly_build_and_test.sql
    with open("sql/nightly_build_and_test.sql", "r", encoding="utf-8") as f:
        sql = f.read()

    # Lines 2632 to 3350
    start_marker = "SET v_tx_ts"
    end_marker = "CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dq_gate_results`"

    start_idx = sql.find(start_marker)
    end_idx = sql.find(end_marker, start_idx)

    dq_body = sql[start_idx:end_idx].strip()

    declarations = """
    DECLARE v_run_ts   TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
    DECLARE v_tx_ts    TIMESTAMP;
    DECLARE v_sub_ts   TIMESTAMP;
    DECLARE v_spend_ts TIMESTAMP;
    DECLARE v_stat_ts  TIMESTAMP;
    DECLARE v_ga4_ts   TIMESTAMP;
    """

    full_dq_script = declarations + "\n" + dq_body

    # Dry run
    job_config = bigquery.QueryJobConfig(dry_run=True)
    d_job = client.query(full_dq_script, job_config=job_config)
    print(f"Full DQ gate dry run: {d_job.total_bytes_processed:,} bytes ({d_job.total_bytes_processed / (1024**3):.4f} GB)")

    # Execute full DQ gate
    print("Executing full DQ gate...")
    job = client.query(full_dq_script)
    job.result()
    print(f"Full DQ gate finished. Job ID: {job.job_id}")

    # Query latest run results
    latest_ts_sql = "SELECT MAX(run_ts) AS max_ts FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`"
    latest_ts = list(client.query(latest_ts_sql).result())[0].max_ts
    print(f"\nLatest run_ts: {latest_ts}")

    # Summary by severity and result
    res_sql = f"""
    SELECT
      severity,
      result,
      COUNT(*) AS cnt
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = '{latest_ts}'
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    summary = list(client.query(res_sql).result())
    print("\n=== DQ GATE RESULTS SUMMARY ===")
    for s in summary:
        print(f"Severity: {s.severity:<5} | Result: {s.result:<5} | Count: {s.cnt}")

    # Print all tests
    detail_sql = f"""
    SELECT
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = '{latest_ts}'
    ORDER BY severity, result DESC, test
    """
    tests = list(client.query(detail_sql).result())
    print("\n=== ALL TEST RESULTS IN LATEST RUN ===")
    for t in tests:
        print(f"[{t.severity:<4}] {t.test:<42} : {t.result:<4} (src: {str(t.source_value):<10}, cur: {str(t.curated_value):<10}) - {t.detail[:80]}")

if __name__ == "__main__":
    main()

