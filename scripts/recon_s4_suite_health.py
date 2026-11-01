#!/usr/bin/env python3
"""
Reconciliation and Verification Script for S4: vw_report_suite_health
Outputs:
- Full output of rh_curated_Bright_Test.vw_report_suite_health across all 6 reporting areas
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

print("=" * 120)
print("S4. vw_report_suite_health FULL VIEW OUTPUT")
print("=" * 120)

q = """
SELECT
  report,
  tables_read,
  built_at,
  latest_data_date,
  hours_since_build,
  status,
  reason
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
ORDER BY
  CASE report
    WHEN 'Paid media' THEN 1
    WHEN 'Organic social' THEN 2
    WHEN 'Free trials' THEN 3
    WHEN 'Customer LTV' THEN 4
    WHEN 'Tracking health' THEN 5
    WHEN 'Charity sales' THEN 6
    ELSE 7
  END
"""

rows = list(client.query(q).result())
for r in rows:
    print(dict(r))

print("\n" + "=" * 120)
print("FORMATTED TABLE")
print("=" * 120)
headers = ["report", "tables_read", "built_at", "latest_data_date", "hours_since_build", "status", "reason"]
print(f"{'Report':<16} | {'Status':<8} | {'Hours':<6} | {'Data Date':<11} | {'Built At':<24} | {'Tables Read':<42} | Reason")
print("-" * 140)
for r in rows:
    built_str = str(r['built_at'])[:19] + "Z" if r['built_at'] else "None"
    print(f"{r['report']:<16} | {r['status']:<8} | {r['hours_since_build']:<6.1f} | {str(r['latest_data_date']):<11} | {built_str:<24} | {r['tables_read']:<42} | {r['reason']}")
