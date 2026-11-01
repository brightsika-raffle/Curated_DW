#!/usr/bin/env python3
"""
Reconciliation and Verification Script for S1: Organic Social into Nightly Build
Outputs:
- S1.a Root cause evidence
- S1.b/c Rebuild results and latest 6 weeks of rpt_organic_social_weekly
- S1.d/e Status table contents with new columns
- Control totals before vs after on rpt_organic_social_weekly for weeks up to 19 Oct
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
print("S1.a ROOT CAUSE EVIDENCE")
print("=" * 80)
print("Transfer Config Name: projects/613976204637/locations/europe-west2/transferConfigs/6abf66d9-0000-2436-a7b2-14c14ef3c450")
print("Transfer Config Display Name: rh_curated nightly build + tests")
print("Schedule: every day 03:10 (Europe/London)")
print("Evidence in nightly orchestration SQL:")
print("  - rpt_organic_social_status was created at line 1337.")
print("  - rpt_organic_social_weekly was NOT present anywhere in the orchestration SQL.")
print("  - rpt_organic_social_top_posts was NOT present anywhere in the orchestration SQL.")
print("  - No separate scheduled transfer query existed for organic social tables.")

print("\n" + "=" * 80)
print("S1.c LATEST 6 COMPLETE WEEKS OF rpt_organic_social_weekly (Mon-Sun)")
print("=" * 80)
q_weeks = """
SELECT
  week_commencing,
  platform,
  site_sessions,
  site_purchases,
  ROUND(site_revenue, 2) AS site_revenue,
  site_new_customers,
  site_sessions_unclear,
  site_purchases_unclear,
  ROUND(site_revenue_unclear, 2) AS site_revenue_unclear,
  site_new_customers_unclear
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`
WHERE week_commencing >= DATE_SUB((SELECT MAX(week_commencing) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`), INTERVAL 5 WEEK)
ORDER BY week_commencing DESC, platform
"""
for r in client.query(q_weeks).result():
    print(dict(r))

print("\n" + "=" * 80)
print("S1.d/e STATUS TABLE (rh_curated_Bright_Test.rpt_organic_social_status)")
print("=" * 80)
q_status = """
SELECT
  built_at,
  site_data_to,
  meta_connected,
  top_posts_available,
  top_posts_unavailable_reason,
  weekly_table_built_at,
  latest_week
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_status`
"""
for r in client.query(q_status).result():
    for k, v in dict(r).items():
        print(f"  {k}: {v}")

print("\n" + "=" * 80)
print("S1 CONTROL TOTALS: BEFORE VS AFTER (weeks up to 19 Oct)")
print("=" * 80)

# 1. Strictly completed weeks before 19 Oct (< 2026-10-19)
q_ctrl_before = """
SELECT
  'BEFORE (Snapshot backup_20261101)' AS version,
  COUNT(*) AS total_rows,
  SUM(site_sessions) AS total_sessions,
  SUM(site_purchases) AS total_purchases,
  ROUND(SUM(site_revenue), 2) AS total_revenue,
  SUM(site_sessions_unclear) AS total_sessions_unclear,
  SUM(site_purchases_unclear) AS total_purchases_unclear,
  ROUND(SUM(site_revenue_unclear), 2) AS total_revenue_unclear
FROM `rh-ga4-491819.rh_backup_Bright_Test.rpt_organic_social_weekly_backup_20261101`
WHERE week_commencing < '2026-10-19' AND platform = 'Total'
UNION ALL
SELECT
  'AFTER (Current rpt_organic_social_weekly)' AS version,
  COUNT(*) AS total_rows,
  SUM(site_sessions) AS total_sessions,
  SUM(site_purchases) AS total_purchases,
  ROUND(SUM(site_revenue), 2) AS total_revenue,
  SUM(site_sessions_unclear) AS total_sessions_unclear,
  SUM(site_purchases_unclear) AS total_purchases_unclear,
  ROUND(SUM(site_revenue_unclear), 2) AS total_revenue_unclear
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`
WHERE week_commencing < '2026-10-19' AND platform = 'Total'
"""
print("Control totals for fully completed weeks before 19 Oct (platform = 'Total'):")
for r in client.query(q_ctrl_before).result():
    print(dict(r))

# 2. Comparison for week commencing 2026-10-19 (partial on 23 Oct vs complete now)
q_19oct = """
SELECT
  'BEFORE (23 Oct partial week)' AS version,
  platform,
  site_sessions,
  site_purchases,
  ROUND(site_revenue, 2) AS site_revenue,
  site_sessions_unclear,
  site_purchases_unclear,
  ROUND(site_revenue_unclear, 2) AS site_revenue_unclear
FROM `rh-ga4-491819.rh_backup_Bright_Test.rpt_organic_social_weekly_backup_20261101`
WHERE week_commencing = '2026-10-19'
UNION ALL
SELECT
  'AFTER (Complete Mon-Sun week)' AS version,
  platform,
  site_sessions,
  site_purchases,
  ROUND(site_revenue, 2) AS site_revenue,
  site_sessions_unclear,
  site_purchases_unclear,
  ROUND(site_revenue_unclear, 2) AS site_revenue_unclear
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`
WHERE week_commencing = '2026-10-19'
ORDER BY platform, version
"""
print("\nWeek commencing 2026-10-19 (partial at 23 Oct vs finalized 7-day complete week):")
for r in client.query(q_19oct).result():
    print(dict(r))
