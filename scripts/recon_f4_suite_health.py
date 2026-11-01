#!/usr/bin/env python3
"""
Reconciliation and Verification Script for F4: Cross-Suite Health View (GA4 Lag Allowance)
Outputs:
1. Recreate vw_report_suite_health with:
   - Paid media: stale_feeds scoped to Meta & Google (>2d lag), no Affiliates reference
   - Free trials: dynamic count from vw_trial_status
   - GA4 lag: up to 2 days behind is ok (stale only when > 2 days)
2. Full output of vw_report_suite_health across all 6 reporting areas
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
print("F4.1 RECREATING vw_report_suite_health WITH GA4 LAG ALLOWANCE AND CLEAN FEED WARNINGS")
print("=" * 100)

view_ddl = """
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
OPTIONS (description = "Cross-suite health and data freshness monitoring view for all 6 Raffle House reporting areas.")
AS
WITH trial_counts AS (
  SELECT
    COUNTIF(status = 'payment due') AS payment_due_count,
    COUNTIF(status = 'active') AS active_count
  FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
),
base_reports AS (
  -- 1. Paid Media (Meta & Google only)
  SELECT
    'Paid media' AS report,
    'rpt_paid_media_entity_daily, rpt_paid_media_entity_status' AS tables_read,
    built_at,
    COALESCE((SELECT MAX(d) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`), wh_to) AS latest_data_date,
    ARRAY_LENGTH(stale_feeds) > 0 AS has_caveat,
    IF(
      ARRAY_LENGTH(stale_feeds) > 0,
      CONCAT('Feed warning: ', ARRAY_TO_STRING(stale_feeds, ', '), ' spend feed >2 days behind build date'),
      'Platform feeds up to date'
    ) AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`

  UNION ALL

  -- 2. Organic Social (complete weeks only; GA4 site data lag up to 2 days ok)
  SELECT
    'Organic social' AS report,
    'rpt_organic_social_weekly, rpt_organic_social_status' AS tables_read,
    built_at,
    site_data_to AS latest_data_date,
    NOT meta_connected AS has_caveat,
    IF(
      NOT meta_connected,
      'Meta Graph API not connected (meta_connected = false); top posts table empty pending permissions',
      'Meta feed connected and site metrics fresh'
    ) AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_status`

  UNION ALL

  -- 3. Free Trials (renewal-day grace window dynamic counts)
  SELECT
    'Free trials' AS report,
    'vw_trial_status, rpt_trial_conversion' AS tables_read,
    MAX(t.built_at) AS built_at,
    (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`) AS latest_data_date,
    FALSE AS has_caveat,
    CONCAT('Fresh (renewal-day grace window active: ',
      CAST(MAX(c.payment_due_count) AS STRING), ' payment due, ',
      CAST(MAX(c.active_count) AS STRING), ' active)') AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion` t
  CROSS JOIN trial_counts c

  UNION ALL

  -- 4. Customer LTV
  SELECT
    'Customer LTV' AS report,
    'customer_ltv_horizons' AS tables_read,
    MAX(built_at) AS built_at,
    MAX(as_of_date) AS latest_data_date,
    FALSE AS has_caveat,
    'Fresh (horizons model updated nightly)' AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons`

  UNION ALL

  -- 5. Tracking Health (GA4 daily export lag up to 2 days ok)
  SELECT
    'Tracking health' AS report,
    'rpt_tracking_coverage_daily' AS tables_read,
    MAX(built_at) AS built_at,
    MAX(d) AS latest_data_date,
    FALSE AS has_caveat,
    'Fresh (daily coverage and consent tracking)' AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`

  UNION ALL

  -- 6. Charity Sales
  SELECT
    'Charity sales' AS report,
    'rpt_charity_sales_daily' AS tables_read,
    MAX(loaded_at) AS built_at,
    MAX(date) AS latest_data_date,
    FALSE AS has_caveat,
    'Fresh (daily charity allocations and revenue)' AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily`
)
SELECT
  report,
  tables_read,
  built_at,
  latest_data_date,
  ROUND(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), built_at, MINUTE) / 60.0, 1) AS hours_since_build,
  CASE
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), built_at, HOUR) > 36
      OR (report = 'Organic social' AND DATE_DIFF(CURRENT_DATE('Europe/London'), latest_data_date, DAY) > 2)
      OR (report = 'Tracking health' AND DATE_DIFF(CURRENT_DATE('Europe/London'), latest_data_date, DAY) > 2)
      OR (report NOT IN ('Organic social', 'Tracking health') AND DATE_DIFF(CURRENT_DATE('Europe/London'), latest_data_date, DAY) > 2)
      THEN 'stale'
    WHEN has_caveat THEN 'warning'
    ELSE 'ok'
  END AS status,
  CASE
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), built_at, HOUR) > 36
      THEN CONCAT('Build stale: last run ', CAST(built_at AS STRING), ' (>36h ago)')
    WHEN DATE_DIFF(CURRENT_DATE('Europe/London'), latest_data_date, DAY) > 2
      THEN CONCAT('Data stale: latest data date ', CAST(latest_data_date AS STRING), ' (>2d behind build date)')
    WHEN has_caveat THEN caveat_reason
    ELSE caveat_reason
  END AS reason
FROM base_reports
ORDER BY
  CASE report
    WHEN 'Paid media' THEN 1
    WHEN 'Organic social' THEN 2
    WHEN 'Free trials' THEN 3
    WHEN 'Customer LTV' THEN 4
    WHEN 'Tracking health' THEN 5
    WHEN 'Charity sales' THEN 6
  END
"""

dry_cfg = bigquery.QueryJobConfig(dry_run=True)
dry = client.query(view_ddl, job_config=dry_cfg)
print(f"Dry run view DDL bytes: {dry.total_bytes_processed:,} bytes")
client.query(view_ddl).result()
print("vw_report_suite_health recreated successfully.")

print("\n" + "=" * 100)
print("F4.2 FULL OUTPUT OF vw_report_suite_health")
print("=" * 100)
q_full = """
SELECT
  report,
  tables_read,
  built_at,
  latest_data_date,
  hours_since_build,
  status,
  reason
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
"""
dry2 = client.query(q_full, job_config=dry_cfg)
print(f"Dry run select bytes: {dry2.total_bytes_processed:,} bytes\n")

rows = [dict(r) for r in client.query(q_full).result()]
print(f"{'Report':<16} | {'Status':<8} | {'Hours':<6} | {'Latest Data':<11} | {'Built At':<24} | {'Reason'}")
print("-" * 120)
for r in rows:
    built_str = str(r['built_at'])[:19] + "Z" if r['built_at'] else "None"
    print(f"{r['report']:<16} | {r['status']:<8} | {r['hours_since_build']:<6.1f} | {str(r['latest_data_date']):<11} | {built_str:<24} | {r['reason']}")
