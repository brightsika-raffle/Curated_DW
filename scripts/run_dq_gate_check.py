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

    # Query recent runs
    recent_runs_sql = """
    SELECT run_ts, COUNT(*) AS cnt, COUNTIF(result = 'FAIL') AS fails, COUNTIF(result = 'WARN') AS warns
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    GROUP BY 1
    ORDER BY 1 DESC
    LIMIT 5
    """
    runs = list(client.query(recent_runs_sql).result())
    print("=== RECENT RUNS IN dq_gate_results ===")
    for r in runs:
        print(f"run_ts: {r.run_ts} | total tests: {r.cnt} | FAIL: {r.fails} | WARN: {r.warns}")

    target_ts = runs[0].run_ts
    print(f"\nTargeting latest run_ts: {target_ts}")

    # Summary by severity and result
    res_sql = f"""
    SELECT
      severity,
      result,
      COUNT(*) AS cnt
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = '{target_ts}'
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    summary = list(client.query(res_sql).result())
    print("\n=== DQ GATE RESULTS SUMMARY (LATEST RUN) ===")
    for s in summary:
        print(f"Severity: {s.severity:<5} | Result: {s.result:<5} | Count: {s.cnt}")

    # All tests for this run
    detail_sql = f"""
    SELECT
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = '{target_ts}'
    ORDER BY severity, result DESC, test
    """
    tests = list(client.query(detail_sql).result())
    print("\n=== ALL TEST RESULTS IN LATEST RUN ===")
    for t in tests:
        print(f"[{t.severity:<4}] {t.test:<40} : {t.result:<4} (src: {t.source_value}, cur: {t.curated_value}) - {t.detail[:80]}")

if __name__ == "__main__":
    main()
