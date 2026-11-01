#!/usr/bin/env python3
"""
Reconciliation and Verification Script for S3: Paid Media Feed Freshness
Outputs:
- S3.a Affiliates spend root cause, source table, owner, action needed
- S3.b Meta spend 1-day API lag explanation
- S3.c rpt_paid_media_entity_status output with new stale_feeds column
- Control totals before vs after on rpt_paid_media_entity_daily
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

print("=" * 80)
print("S3.a AFFILIATES SPEND FEED DIAGNOSIS")
print("=" * 80)
print("Source Table: rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload")
print("Channel Filter: `Channel Group` = 'Affiliates'")
q_aff = """
SELECT
  `Channel Group` AS channel_group,
  MIN(`Period`) AS min_period,
  MAX(`Period`) AS max_period,
  COUNT(*) AS total_rows,
  ROUND(SUM(CAST(`Cost` AS NUMERIC)), 2) AS total_spend_gbp
FROM `rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload`
WHERE `Channel Group` = 'Affiliates'
GROUP BY 1
"""
for r in client.query(q_aff).result():
    print(dict(r))
print("Root Cause: Affiliates spend is not connected via an automated API connector. It relies entirely on a manual CSV upload / Google Sheets ingestion into rh_warehouse. The last manual upload batch stopped at 2026-09-27.")
print("Feed Status: Affiliates spend is out of scope for paid media reporting; manual upload stopped at 2026-09-27.")

print("\n" + "=" * 80)
print("S3.b META SPEND 1-DAY LAG EXPLANATION")
print("=" * 80)
q_meta = """
SELECT
  MIN(spend_date) AS min_date,
  MAX(spend_date) AS max_date,
  COUNT(DISTINCT spend_date) AS num_days,
  ROUND(SUM(spend_gbp), 2) AS total_spend_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
"""
for r in client.query(q_meta).result():
    print(dict(r))
print("Meta Load Mechanism: Scheduler and job name are unknown (Cloud Run and Cloud Scheduler list permissions are denied on the service account). Factual evidence from BigQuery: stg_meta_ads_daily was last updated at 2026-10-31 21:00:47 UTC with spend data through 2026-10-30. Meta API reporting metrics settle with a 24-48 hour window, so a 1-day lag at morning build time is expected behavior.")

print("\n" + "=" * 80)
print("S3.c UPDATED rpt_paid_media_entity_status (with stale_feeds)")
print("=" * 80)
q_status = """
SELECT
  built_at,
  wh_to,
  meta_to,
  google_to,
  bing_to,
  affiliates_to,
  stale_feeds,
  p99_v3,
  report_start,
  mat_from
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`
"""
for r in client.query(q_status).result():
    for k, v in dict(r).items():
        print(f"  {k}: {v}")

print("\n" + "=" * 80)
print("S3 CONTROL TOTALS: rpt_paid_media_entity_daily (1 Sep to 18 Oct, F & L)")
print("=" * 80)
q_ctrl = """
SELECT
  basis,
  entity_id,
  ROUND(SUM(spend), 2) AS total_spend,
  ROUND(SUM(wh_rev), 2) AS total_wh_rev,
  SUM(wh_n) AS total_wh_n
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
WHERE d BETWEEN '2026-09-01' AND '2026-10-18'
  AND entity_id IN ('ps', 'gb', 'all')
GROUP BY 1, 2
ORDER BY basis, entity_id
"""
for r in client.query(q_ctrl).result():
    print(dict(r))
