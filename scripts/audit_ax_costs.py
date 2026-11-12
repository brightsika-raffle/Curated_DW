import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
PROJECT_ID = "rh-ga4-491819"

token = subprocess.check_output(f"gcloud auth print-access-token --impersonate-service-account={SA}", shell=True, text=True).strip()
client = bigquery.Client(project=PROJECT_ID, location="europe-west2", credentials=Credentials(token))

sql = """
SELECT
  creation_time,
  job_id,
  total_bytes_billed,
  ROUND(total_bytes_billed / (1024*1024*1024), 3) AS gb_billed,
  ROUND((total_bytes_billed / (1024*1024*1024*1024)) * 6.25, 4) AS cost_gbp,
  query
FROM `region-europe-west2`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= '2026-11-12 05:48:00 UTC'
  AND job_type = 'QUERY'
  AND statement_type != 'SCRIPT'
ORDER BY creation_time ASC
"""
rows = list(client.query(sql).result())
total_gb = sum(r["gb_billed"] or 0 for r in rows)
total_cost = sum(r["cost_gbp"] or 0 for r in rows)

print(f"AX Session Total Jobs: {len(rows)}")
print(f"AX Session Total GB billed: {total_gb:.3f} GB")
print(f"AX Session Total cost: £{total_cost:.4f}")
print("-" * 110)
for r in rows:
    q_prev = (r["query"] or "").replace("\n", " ").strip()[:75]
    print(f"{r['creation_time'].strftime('%H:%M:%S')} | {r['job_id'][:36]} | {r['gb_billed']:>6.3f} GB | £{r['cost_gbp']:>7.4f} | {q_prev}")
