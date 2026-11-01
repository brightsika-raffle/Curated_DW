#!/usr/bin/env python3
"""
Reconciliation and Verification Script for F2: Organic Social Weekly (Complete Weeks Only)
Outputs:
1. Control totals for weeks up to 2026-10-19 before update
2. Snapshot creation in rh_backup_Bright_Test with 30-day expiry
3. Rebuild rpt_organic_social_weekly and rpt_organic_social_status
4. Latest weeks in rpt_organic_social_weekly
5. rpt_organic_social_status row showing latest_week = 2026-10-19 and site_data_to = 2026-10-31
6. Control totals comparison before vs after
"""

import os, subprocess, sys, datetime
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
print("F2.1 CONTROL TOTALS: rpt_organic_social_weekly WEEKS UP TO 2026-10-19 (BEFORE)")
print("=" * 100)
q_ctrl = """
SELECT
  week_commencing,
  platform,
  site_sessions,
  site_purchases,
  site_revenue,
  site_new_customers,
  site_sessions_unclear,
  site_purchases_unclear,
  site_revenue_unclear
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`
WHERE week_commencing <= '2026-10-19'
ORDER BY week_commencing DESC, platform
"""
dry_cfg = bigquery.QueryJobConfig(dry_run=True)
dry = client.query(q_ctrl, job_config=dry_cfg)
print(f"Dry run control query bytes: {dry.total_bytes_processed:,} bytes")
before_rows = [dict(r) for r in client.query(q_ctrl).result()]
for r in before_rows[:12]:
    print(r)

print("\n" + "=" * 100)
print("F2.2 SNAPSHOTTING TABLES TO rh_backup_Bright_Test (30-DAY EXPIRY)")
print("=" * 100)
ts_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
snap_weekly = f"rh-ga4-491819.rh_backup_Bright_Test.rpt_organic_social_weekly_snap_{ts_suffix}"
snap_status = f"rh-ga4-491819.rh_backup_Bright_Test.rpt_organic_social_status_snap_{ts_suffix}"

q_snap1 = f"""
CREATE OR REPLACE TABLE `{snap_weekly}`
OPTIONS(expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 30 DAY))
AS
SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`
"""
q_snap2 = f"""
CREATE OR REPLACE TABLE `{snap_status}`
OPTIONS(expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 30 DAY))
AS
SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_status`
"""

dry1 = client.query(q_snap1, job_config=dry_cfg)
print(f"Dry run snap weekly bytes: {dry1.total_bytes_processed:,} bytes")
client.query(q_snap1).result()
print(f"Snapshot created: {snap_weekly}")

dry2 = client.query(q_snap2, job_config=dry_cfg)
print(f"Dry run snap status bytes: {dry2.total_bytes_processed:,} bytes")
client.query(q_snap2).result()
print(f"Snapshot created: {snap_status}")

print("\n" + "=" * 100)
print("F2.3 REBUILDING rpt_organic_social_weekly AND rpt_organic_social_status")
print("=" * 100)
q_rebuild = """
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`
PARTITION BY week_commencing
CLUSTER BY platform
OPTIONS (description = "Weekly organic social performance aggregation by platform and Total for complete weeks (Mon-Sun).")
AS
WITH max_site_date AS (
  SELECT MAX(date) AS site_data_to
  FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_organic_social_sessions_daily`
),
platform_rollup AS (
  SELECT
    DATE_TRUNC(s.date, WEEK(MONDAY)) AS week_commencing,
    s.platform,
    CAST(NULL AS INT64) AS followers_start,
    CAST(NULL AS INT64) AS followers_end,
    CAST(NULL AS INT64) AS follower_change,
    CAST(NULL AS INT64) AS reach,
    CAST(NULL AS INT64) AS total_engagements,
    CAST(NULL AS NUMERIC) AS engagement_rate,
    CAST(NULL AS INT64) AS posts_published,
    CAST(NULL AS STRING) AS posts_by_type,
    CAST(NULL AS NUMERIC) AS mean_reach_per_post,
    CAST(NULL AS NUMERIC) AS median_reach_per_post,
    CAST(NULL AS NUMERIC) AS mean_engagement_rate_per_post,
    CAST(NULL AS NUMERIC) AS median_engagement_rate_per_post,
    CAST(NULL AS BOOL) AS has_outlier_post,
    SUM(CASE WHEN s.traffic_category = 'clearly_organic' THEN s.sessions ELSE 0 END) AS site_sessions,
    SUM(CASE WHEN s.traffic_category = 'clearly_organic' THEN s.purchases ELSE 0 END) AS site_purchases,
    ROUND(SUM(CASE WHEN s.traffic_category = 'clearly_organic' THEN s.revenue ELSE 0 END), 2) AS site_revenue,
    SUM(CASE WHEN s.traffic_category = 'clearly_organic' THEN s.new_customers ELSE 0 END) AS site_new_customers,
    SUM(CASE WHEN s.traffic_category = 'unclear' THEN s.sessions ELSE 0 END) AS site_sessions_unclear,
    SUM(CASE WHEN s.traffic_category = 'unclear' THEN s.purchases ELSE 0 END) AS site_purchases_unclear,
    ROUND(SUM(CASE WHEN s.traffic_category = 'unclear' THEN s.revenue ELSE 0 END), 2) AS site_revenue_unclear,
    SUM(CASE WHEN s.traffic_category = 'unclear' THEN s.new_customers ELSE 0 END) AS site_new_customers_unclear
  FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_organic_social_sessions_daily` s
  CROSS JOIN max_site_date m
  WHERE DATE_ADD(DATE_TRUNC(s.date, WEEK(MONDAY)), INTERVAL 6 DAY) <= m.site_data_to
  GROUP BY 1, 2
),
total_rollup AS (
  SELECT
    week_commencing,
    'Total' AS platform,
    CAST(NULL AS INT64) AS followers_start,
    CAST(NULL AS INT64) AS followers_end,
    CAST(NULL AS INT64) AS follower_change,
    CAST(NULL AS INT64) AS reach,
    CAST(NULL AS INT64) AS total_engagements,
    CAST(NULL AS NUMERIC) AS engagement_rate,
    CAST(NULL AS INT64) AS posts_published,
    CAST(NULL AS STRING) AS posts_by_type,
    CAST(NULL AS NUMERIC) AS mean_reach_per_post,
    CAST(NULL AS NUMERIC) AS median_reach_per_post,
    CAST(NULL AS NUMERIC) AS mean_engagement_rate_per_post,
    CAST(NULL AS NUMERIC) AS median_engagement_rate_per_post,
    CAST(NULL AS BOOL) AS has_outlier_post,
    SUM(site_sessions) AS site_sessions,
    SUM(site_purchases) AS site_purchases,
    ROUND(SUM(site_revenue), 2) AS site_revenue,
    SUM(site_new_customers) AS site_new_customers,
    SUM(site_sessions_unclear) AS site_sessions_unclear,
    SUM(site_purchases_unclear) AS site_purchases_unclear,
    ROUND(SUM(site_revenue_unclear), 2) AS site_revenue_unclear,
    SUM(site_new_customers_unclear) AS site_new_customers_unclear
  FROM platform_rollup
  GROUP BY 1
)
SELECT * FROM platform_rollup
UNION ALL
SELECT * FROM total_rollup;
"""
dry3 = client.query(q_rebuild, job_config=dry_cfg)
print(f"Dry run rebuild weekly bytes: {dry3.total_bytes_processed:,} bytes")
client.query(q_rebuild).result()
print("rpt_organic_social_weekly rebuilt.")

q_status_rebuild = """
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_status`
OPTIONS (description = "One-row status table for organic social reporting page showing sync and break dates.")
AS
SELECT
  CURRENT_TIMESTAMP() AS built_at,
  DATE '2026-10-19' AS tracking_changed_on,
  DATE '2026-10-19' AS cookie_banner_changed_on,
  DATE '2026-10-24' AS consent_setting_changed_on,
  DATE '2026-10-26' AS pricing_display_changed_on,
  [DATE '2026-10-19', DATE '2026-10-24', DATE '2026-10-26'] AS change_dates,
  FALSE AS meta_connected,
  CAST(NULL AS DATE) AS ig_data_to,
  CAST(NULL AS DATE) AS fb_data_to,
  (SELECT MAX(date) FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_organic_social_sessions_daily`) AS site_data_to,
  FALSE AS top_posts_available,
  'Meta Graph API feed is not connected (meta_connected = false); top posts requires Meta Page and Instagram permissions.' AS top_posts_unavailable_reason,
  CURRENT_TIMESTAMP() AS weekly_table_built_at,
  (SELECT MAX(week_commencing) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`) AS latest_week;
"""
dry4 = client.query(q_status_rebuild, job_config=dry_cfg)
print(f"Dry run rebuild status bytes: {dry4.total_bytes_processed:,} bytes")
client.query(q_status_rebuild).result()
print("rpt_organic_social_status rebuilt.")

print("\n" + "=" * 100)
print("F2.4 LATEST WEEKS PRESENT IN rpt_organic_social_weekly")
print("=" * 100)
q_weeks = """
SELECT DISTINCT week_commencing
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`
ORDER BY week_commencing DESC
LIMIT 6
"""
for r in client.query(q_weeks).result():
    print(dict(r))

print("\n" + "=" * 100)
print("F2.5 rpt_organic_social_status ROW")
print("=" * 100)
q_st = "SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_status`"
for r in client.query(q_st).result():
    print(dict(r))

print("\n" + "=" * 100)
print("F2.6 CONTROL TOTALS: rpt_organic_social_weekly WEEKS UP TO 2026-10-19 (AFTER)")
print("=" * 100)
after_rows = [dict(r) for r in client.query(q_ctrl).result()]
for r in after_rows[:12]:
    print(r)

matches = (before_rows == after_rows)
print(f"\nControl totals up to 2026-10-19 match exactly: {matches}")
print(f"Number of rows compared: {len(before_rows)} before vs {len(after_rows)} after")
