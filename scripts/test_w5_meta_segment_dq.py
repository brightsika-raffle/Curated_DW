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
    return bigquery.Client(project=PROJECT_ID, location="europe-west2", credentials=Credentials(token))

def main():
    client = get_client()
    sql = """
    SELECT
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = (SELECT MAX(run_ts) FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`)
      AND test IN ('meta_segment_spend_parity', 'meta_segment_null_spend')
    ORDER BY test
    """
    rows = list(client.query(sql).result())
    print("=== META SEGMENT DQ TEST RESULTS IN LATEST RUN ===")
    for r in rows:
        print(f"[{r.severity}] {r.test}: {r.result} (src_limit={r.source_value}, observed={r.curated_value}) - {r.detail}")

if __name__ == "__main__":
    main()
