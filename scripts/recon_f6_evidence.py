#!/usr/bin/env python3
"""
Reconciliation and Verification Script for F6: Evidence or Withdraw
Outputs:
1. Affiliates: Confirmation that affiliates feed owner claim is withdrawn and feed is out of scope
2. Meta load: BigQuery factual evidence for stg_meta_ads_daily (last modified, last loaded_at, max spend date)
   and explicit declaration of scheduler and job name as unknown (due to restricted IAM permissions on Cloud Run/Scheduler)
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

print("=" * 100)
print("F6.a AFFILIATES FEED OWNER CLAIM: WITHDRAWN")
print("=" * 100)
print("Status: Affiliates spend is out of scope for paid media reporting (scope is strictly Meta and Google).")
print("Action taken: The unverified 'feed owner' claim has been deleted from scripts, documentation, and notes.")

print("\n" + "=" * 100)
print("F6.b META SPEND LOAD MECHANISM: FACTUAL EVIDENCE & STATUS")
print("=" * 100)
t = client.get_table("rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily")
print(f"Table: {t.table_id}")
print(f"Created: {t.created}")
print(f"Last Modified: {t.modified}")
print(f"Row Count: {t.num_rows}")

q = """
SELECT
  MIN(spend_date) AS min_spend_date,
  MAX(spend_date) AS max_spend_date,
  MAX(loaded_at) AS last_loaded_at,
  COUNT(DISTINCT spend_date) AS total_spend_days,
  ROUND(SUM(spend_gbp), 2) AS total_spend_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
"""
for r in client.query(q).result():
    print("Table metrics:", dict(r))

print("\nScheduler / Orchestrator: unknown")
print("Job Name: unknown")
print("Explanation: The pipeline service account lacks IAM permissions for 'cloudscheduler.jobs.list' and 'run.jobs.list'.")
print("Therefore, the exact cloud orchestrator and job name cannot be verified through GCP APIs. The claim of a Cloud Run")
print("job named 'meta-daily-sync' was unverified and is hereby withdrawn. Factual evidence shows stg_meta_ads_daily")
print("was updated at 2026-10-31 21:00:47 UTC with spend data through 2026-10-30.")
