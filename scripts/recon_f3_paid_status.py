#!/usr/bin/env python3
"""
Reconciliation and Verification Script for F3: Paid Feed Status (Meta and Google only)
Outputs:
1. Snapshot creation in rh_backup_Bright_Test with 30-day expiry
2. Rebuild rpt_paid_media_entity_status with stale_feeds scoped strictly to Meta & Google (> 2 days)
3. rpt_paid_media_entity_status full output row
4. vw_report_suite_health Paid media health row
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
print("F3.1 SNAPSHOTTING rpt_paid_media_entity_status TO rh_backup_Bright_Test (30-DAY EXPIRY)")
print("=" * 100)
ts_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
snap_status = f"rh-ga4-491819.rh_backup_Bright_Test.rpt_paid_media_entity_status_snap_{ts_suffix}"

q_snap = f"""
CREATE OR REPLACE TABLE `{snap_status}`
OPTIONS(expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 30 DAY))
AS
SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`
"""
dry_cfg = bigquery.QueryJobConfig(dry_run=True)
dry = client.query(q_snap, job_config=dry_cfg)
print(f"Dry run snapshot bytes: {dry.total_bytes_processed:,} bytes")
client.query(q_snap).result()
print(f"Snapshot created: {snap_status}")

print("\n" + "=" * 100)
print("F3.2 REBUILDING rpt_paid_media_entity_status (META & GOOGLE ONLY, > 2 DAYS LAG)")
print("=" * 100)
q_rebuild = """
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`
OPTIONS (description = "Status table for paid media reporting showing data currency and parameters. Rebuilt nightly.")
AS
WITH
raw_touches AS (
  SELECT
    t.transaction_id,
    DATE(t.purchase_at, 'Europe/London') AS pay_date_lon,
    t.is_first_purchase,
    COALESCE(h.ltv_3m, h.realised_value, IF(IFNULL(t.is_free_entry, FALSE), NUMERIC '0', t.revenue_gbp)) AS v3_raw,
    COALESCE(touch.first_touch_timestamp, touch.last_touch_timestamp) AS f_ts,
    touch.last_touch_timestamp AS l_ts
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
  JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
    ON t.transaction_id = touch.transaction_id
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
    ON t.subscription_key = s.subscription_key
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons` h
    ON t.customer_key = h.customer_key
  WHERE DATE(t.purchase_at, 'Europe/London') >= DATE '2026-06-01'
    AND COALESCE(t.payment_initiated_by, '') != 'recurring'
    AND t.customer_key != 'UNKNOWN_DELETED'
    AND (
      NOT IFNULL(t.is_free_entry, FALSE)
      OR (IFNULL(t.is_free_entry, FALSE) AND t.order_type = 'SUBSCRIPTION' AND s.created_by = 'special-offer')
    )
),
unioned_touches AS (
  SELECT pay_date_lon, is_first_purchase, v3_raw, f_ts AS touch_ts FROM raw_touches
  UNION ALL
  SELECT pay_date_lon, is_first_purchase, v3_raw, l_ts AS touch_ts FROM raw_touches
),
windowed_touches AS (
  SELECT is_first_purchase, v3_raw
  FROM unioned_touches
  WHERE touch_ts IS NOT NULL
    AND DATE_DIFF(pay_date_lon, DATE(touch_ts, 'Europe/London'), DAY) BETWEEN 0 AND 7
    AND DATE(touch_ts, 'Europe/London') >= DATE '2026-06-01'
),
p99_calc AS (
  SELECT
    ROUND(CAST(PERCENTILE_CONT(IF(is_first_purchase, CAST(v3_raw AS FLOAT64), NULL), 0.99) OVER () AS NUMERIC), 2) AS p99_v3
  FROM windowed_touches
  LIMIT 1
),
dates_calc AS (
  SELECT
    (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`) AS wh_to,
    (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`) AS meta_to,
    (SELECT MAX(segments_date) FROM `rh-ga4-491819.google_ads_transfer.ads_CampaignBasicStats_6102368044`) AS google_to,
    CAST(NULL AS DATE) AS bing_to,
    (SELECT MAX(`Period`) FROM `rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload` WHERE `Channel Group` = 'Affiliates') AS affiliates_to
)
SELECT
  CURRENT_TIMESTAMP() AS built_at,
  d.wh_to,
  d.meta_to,
  d.google_to,
  d.bing_to,
  d.affiliates_to,
  ARRAY(
    SELECT feed FROM UNNEST([
      STRUCT('Meta' AS feed, d.meta_to AS feed_date),
      STRUCT('Google' AS feed, d.google_to AS feed_date)
    ])
    WHERE feed_date IS NOT NULL AND DATE_DIFF(CURRENT_DATE('Europe/London'), feed_date, DAY) > 2
  ) AS stale_feeds,
  p99.p99_v3,
  DATE '2026-09-01' AS report_start,
  DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 6 DAY) AS mat_from,
  [DATE '2026-10-19', DATE '2026-10-24', DATE '2026-10-26'] AS change_dates
FROM p99_calc p99
CROSS JOIN dates_calc d;
"""
dry2 = client.query(q_rebuild, job_config=dry_cfg)
print(f"Dry run rebuild bytes: {dry2.total_bytes_processed:,} bytes")
client.query(q_rebuild).result()
print("rpt_paid_media_entity_status rebuilt successfully.")

print("\n" + "=" * 100)
print("F3.3 rpt_paid_media_entity_status FULL ROW OUTPUT")
print("=" * 100)
q_status_row = "SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`"
for r in client.query(q_status_row).result():
    print(dict(r))

print("\n" + "=" * 100)
print("F3.4 vw_report_suite_health PAID MEDIA ROW")
print("=" * 100)
q_health = """
SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
WHERE report = 'Paid media'
"""
for r in client.query(q_health).result():
    print(dict(r))
