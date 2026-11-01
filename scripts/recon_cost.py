#!/usr/bin/env python3
"""
Compute total BigQuery query execution cost for Prompt S-FIX run
via region-europe-west2.INFORMATION_SCHEMA.JOBS_BY_PROJECT
"""

import os, subprocess, sys
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_bq_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, credentials=Credentials(token), location=LOCATION)

client = get_bq_client()

sql = f"""
SELECT
  project_id,
  user_email,
  job_id,
  creation_time,
  start_time,
  end_time,
  statement_type,
  total_bytes_processed,
  total_bytes_billed,
  ROUND(total_bytes_billed / POW(1024, 3), 4) AS gib_billed,
  ROUND(total_bytes_billed / POW(1024, 4) * 6.25 * 0.77, 4) AS est_cost_gbp,
  query
FROM `region-europe-west2`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP('2026-11-01 08:50:00+00:00')
  AND user_email = '{SA}'
  AND job_type = 'QUERY'
ORDER BY creation_time ASC
"""

print("=" * 100)
print("QUERY AUDIT & COST BREAKDOWN: PROMPT S-FIX")
print("=" * 100)

jobs = list(client.query(sql).result())
total_processed = 0
total_billed = 0

for j in jobs:
    b_proc = j.total_bytes_processed or 0
    b_bill = j.total_bytes_billed or 0
    total_processed += b_proc
    total_billed += b_bill
    q_snippet = " ".join((j.query or "").split())[:80]
    stmt = str(j.statement_type or "QUERY")
    print(f"{j.creation_time.strftime('%Y-%m-%d %H:%M:%S UTC')} | Job: {j.job_id} | Type: {stmt:<10} | Proc: {b_proc/1024/1024:>8.2f} MB | Billed: {b_bill/1024/1024:>8.2f} MB | SQL: {q_snippet}...")

gib_billed_total = total_billed / (1024 ** 3)
tib_billed_total = total_billed / (1024 ** 4)
# On-demand query pricing: $6.25 / TiB. Conversion: 1 USD ~ 0.77 GBP -> ~£4.8125 / TiB
cost_gbp_total = tib_billed_total * 6.25 * 0.77

print("=" * 100)
print(f"Total Queries Executed: {len(jobs)}")
print(f"Total Bytes Processed: {total_processed} ({total_processed / (1024**3):.4f} GiB)")
print(f"Total Bytes Billed:    {total_billed} ({gib_billed_total:.4f} GiB / {tib_billed_total:.6f} TiB)")
print(f"Estimated Cost (GBP):  GBP {cost_gbp_total:.4f}")
print("=" * 100)
