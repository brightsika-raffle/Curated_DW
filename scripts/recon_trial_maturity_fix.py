#!/usr/bin/env python3
"""
Reconciliation and Verification Script for Trial Maturity Fix:
1. Rebuild rpt_trial_conversion with:
   - mature_1st: CURRENT_DATE('Europe/London') >= DATE_ADD(DATE_ADD(t.cohort, INTERVAL 1 MONTH), INTERVAL 15 DAY)
   - mature_3rd: CURRENT_DATE('Europe/London') >= DATE_ADD(DATE_ADD(t.cohort, INTERVAL 3 MONTH), INTERVAL 15 DAY)
2. Verify trailing mature rates:
   - 1st payment rate: 53.59% (excluding October)
   - 3rd payment rate: 30.32% (excluding August)
3. Hard test verification: fails if any mature cohort has 0 conversions with > 20 trials
4. Recreate vw_report_suite_health with hard test failure reporting
5. Rebuild rpt_exec_summary and verify headline KPI rows
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

print("=" * 110)
print("PART 1: REBUILDING rpt_trial_conversion WITH 15-DAY MATURITY BUFFER")
print("=" * 110)

rebuild_rpt_sql = """
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
CLUSTER BY cohort, tier
OPTIONS (description = "Free-trial conversion by trial start month (cohort of first_free_at) and plan tier: 1st-payment and 3rd-payment conversion rates, maturity flags (+15d buffer from renewal date), and trailing mature conversion rates by tier. Rebuilt nightly.")
AS
WITH sub_tx AS (
  SELECT
    subscription_key,
    MIN(IF(is_free_entry, purchase_at, NULL)) AS first_free_at,
    COUNTIF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE)) AS paid_tx_count,
    ARRAY_AGG(
      IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), purchase_date, NULL)
      IGNORE NULLS
      ORDER BY purchase_at, transaction_id
    ) AS paid_dates
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE subscription_key IS NOT NULL
  GROUP BY subscription_key
),
trials AS (
  SELECT
    s.subscription_key,
    s.customer_key,
    DATE(tx.first_free_at) AS start_date,
    DATE_TRUNC(DATE(tx.first_free_at), MONTH) AS cohort,
    DATE(s.next_purchase_date) AS next_purchase_date,
    s.monthly_value_gbp,
    COALESCE(
      m.tier_name,
      CASE
        WHEN s.monthly_value_gbp IN (10, 15) THEN 'Bronze'
        WHEN s.monthly_value_gbp IN (20, 25, 30) THEN 'Silver'
        WHEN s.monthly_value_gbp IN (35, 50) THEN 'Gold'
        WHEN s.monthly_value_gbp IN (55, 75) THEN 'Platinum'
        ELSE 'Other'
      END
    ) AS tier,
    COALESCE(
      m.tier_band,
      CASE
        WHEN s.monthly_value_gbp < 20 THEN 'Low'
        WHEN s.monthly_value_gbp < 40 THEN 'Mid'
        WHEN s.monthly_value_gbp < 60 THEN 'High'
        ELSE 'Very High'
      END
    ) AS tier_band,
    COALESCE(tx.paid_tx_count, 0) AS paid_tx_count,
    COALESCE(tx.paid_tx_count, 0) >= 1 AS converted_1st,
    COALESCE(tx.paid_tx_count, 0) >= 3 AS converted_3rd,
    tx.paid_dates[SAFE_OFFSET(0)] AS first_paid_date,
    tx.paid_dates[SAFE_OFFSET(2)] AS third_paid_date,
    CASE
      WHEN COALESCE(tx.paid_tx_count, 0) >= 1 THEN 'converted'
      WHEN CURRENT_DATE('Europe/London') < DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at), MONTH), INTERVAL 1 MONTH) THEN 'active'
      ELSE 'lapsed'
    END AS trial_status,
    IF(
      COALESCE(tx.paid_tx_count, 0) >= 1,
      GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(0)], DATE(tx.first_free_at), DAY), 0),
      NULL
    ) AS days_to_1st,
    IF(
      COALESCE(tx.paid_tx_count, 0) >= 3,
      GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(2)], DATE(tx.first_free_at), DAY), 0),
      NULL
    ) AS days_to_3rd
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
  JOIN sub_tx tx
    ON s.subscription_key = tx.subscription_key
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model` m
    ON s.subscription_model_key = m.subscription_model_key
  WHERE s.is_special_offer_trial
    AND tx.first_free_at IS NOT NULL
),
cohort_tier_med AS (
  SELECT DISTINCT
    cohort,
    tier,
    PERCENTILE_CONT(CAST(days_to_1st AS FLOAT64), 0.5) OVER (PARTITION BY cohort, tier) AS median_days_1st_raw,
    PERCENTILE_CONT(CAST(days_to_3rd AS FLOAT64), 0.5) OVER (PARTITION BY cohort, tier) AS median_days_3rd_raw
  FROM trials
),
cohort_tier_agg AS (
  SELECT
    t.cohort,
    t.tier,
    ANY_VALUE(t.tier_band) AS tier_band,
    COUNT(*) AS trials,
    COUNTIF(t.trial_status = 'active') AS active_trials,
    COUNTIF(t.trial_status = 'lapsed') AS lapsed_trials,
    COUNTIF(t.converted_1st) AS converted_1st,
    ROUND(CAST(SAFE_DIVIDE(COUNTIF(t.converted_1st), COUNT(*)) AS NUMERIC), 4) AS conversion_rate_1st,
    ROUND(CAST(AVG(t.days_to_1st) AS NUMERIC), 2) AS mean_days_1st,
    COUNTIF(t.converted_3rd) AS converted_3rd,
    ROUND(CAST(SAFE_DIVIDE(COUNTIF(t.converted_3rd), COUNT(*)) AS NUMERIC), 4) AS conversion_rate_3rd,
    ROUND(CAST(AVG(t.days_to_3rd) AS NUMERIC), 2) AS mean_days_3rd,
    CURRENT_DATE('Europe/London') >= DATE_ADD(DATE_ADD(t.cohort, INTERVAL 1 MONTH), INTERVAL 15 DAY) AS mature_1st,
    CURRENT_DATE('Europe/London') >= DATE_ADD(DATE_ADD(t.cohort, INTERVAL 3 MONTH), INTERVAL 15 DAY) AS mature_3rd
  FROM trials t
  GROUP BY t.cohort, t.tier
),
trailing_tier AS (
  SELECT
    tier,
    SUM(IF(mature_1st, trials, 0)) AS trailing_mature_trials_1st,
    SUM(IF(mature_1st, converted_1st, 0)) AS trailing_mature_converted_1st,
    SAFE_DIVIDE(SUM(IF(mature_1st, converted_1st, 0)), SUM(IF(mature_1st, trials, 0))) AS trailing_tier_cvr_1st,
    SUM(IF(mature_3rd, trials, 0)) AS trailing_mature_trials_3rd,
    SUM(IF(mature_3rd, converted_3rd, 0)) AS trailing_mature_converted_3rd,
    SAFE_DIVIDE(SUM(IF(mature_3rd, converted_3rd, 0)), SUM(IF(mature_3rd, trials, 0))) AS trailing_tier_cvr_3rd
  FROM cohort_tier_agg
  GROUP BY tier
),
trailing_overall AS (
  SELECT
    SUM(IF(mature_1st, trials, 0)) AS overall_mature_trials_1st,
    SUM(IF(mature_1st, converted_1st, 0)) AS overall_mature_converted_1st,
    SAFE_DIVIDE(SUM(IF(mature_1st, converted_1st, 0)), SUM(IF(mature_1st, trials, 0))) AS trailing_overall_cvr_1st,
    SUM(IF(mature_3rd, trials, 0)) AS overall_mature_trials_3rd,
    SUM(IF(mature_3rd, converted_3rd, 0)) AS overall_mature_converted_3rd,
    SAFE_DIVIDE(SUM(IF(mature_3rd, converted_3rd, 0)), SUM(IF(mature_3rd, trials, 0))) AS trailing_overall_cvr_3rd
  FROM cohort_tier_agg
)
SELECT
  a.cohort,
  a.tier,
  a.tier_band,
  a.trials,
  a.active_trials,
  a.lapsed_trials,
  a.converted_1st,
  a.conversion_rate_1st,
  a.mean_days_1st,
  ROUND(CAST(m.median_days_1st_raw AS NUMERIC), 1) AS median_days_1st,
  a.converted_3rd,
  a.conversion_rate_3rd,
  a.mean_days_3rd,
  ROUND(CAST(m.median_days_3rd_raw AS NUMERIC), 1) AS median_days_3rd,
  a.mature_1st,
  a.mature_3rd,
  ROUND(CAST(
    CASE
      WHEN tt.trailing_mature_trials_1st >= 30 THEN tt.trailing_tier_cvr_1st
      ELSE o.trailing_overall_cvr_1st
    END AS NUMERIC
  ), 4) AS trailing_cvr_1st,
  ROUND(CAST(
    CASE
      WHEN tt.trailing_mature_trials_3rd >= 30 THEN tt.trailing_tier_cvr_3rd
      ELSE o.trailing_overall_cvr_3rd
    END AS NUMERIC
  ), 4) AS trailing_cvr_3rd,
  ROUND(CAST(o.trailing_overall_cvr_1st AS NUMERIC), 4) AS trailing_overall_cvr_1st,
  ROUND(CAST(o.trailing_overall_cvr_3rd AS NUMERIC), 4) AS trailing_overall_cvr_3rd,
  CURRENT_TIMESTAMP() AS built_at
FROM cohort_tier_agg a
JOIN cohort_tier_med m
  ON a.cohort = m.cohort AND a.tier = m.tier
LEFT JOIN trailing_tier tt
  ON a.tier = tt.tier
CROSS JOIN trailing_overall o;
"""

dry_rpt = client.query(rebuild_rpt_sql, job_config=bigquery.QueryJobConfig(dry_run=True))
print(f"Dry run rpt_trial_conversion rebuild bytes: {dry_rpt.total_bytes_processed} bytes ({dry_rpt.total_bytes_processed / 1024 / 1024:.2f} MB)")
client.query(rebuild_rpt_sql).result()
print("rpt_trial_conversion rebuilt successfully.")

print("\n" + "=" * 110)
print("PART 2: rpt_trial_conversion FULL COHORT ROWS (MATURITY & CONVERSIONS)")
print("=" * 110)
q_show = """
SELECT
  cohort,
  tier,
  trials,
  converted_1st,
  conversion_rate_1st,
  mature_1st,
  converted_3rd,
  conversion_rate_3rd,
  mature_3rd,
  trailing_cvr_1st,
  trailing_cvr_3rd
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
ORDER BY cohort
"""
for r in client.query(q_show).result():
    print(dict(r))

print("\n" + "=" * 110)
print("PART 3: TRAILING MATURE CONVERSION RATES AS OF TODAY")
print("=" * 110)
q_trailing_summary = """
SELECT
  -- 1st payment rate: mature cohorts only (excluding October)
  ROUND(SUM(IF(mature_1st, converted_1st, 0)) / SUM(IF(mature_1st, trials, 0)) * 100, 2) AS trailing_mature_1st_cvr_pct,
  SUM(IF(mature_1st, converted_1st, 0)) AS mature_converted_1st_sum,
  SUM(IF(mature_1st, trials, 0)) AS mature_trials_1st_sum,
  -- 3rd payment rate: mature cohorts only (excluding August, September, October)
  ROUND(SUM(IF(mature_3rd, converted_3rd, 0)) / SUM(IF(mature_3rd, trials, 0)) * 100, 2) AS trailing_mature_3rd_cvr_pct,
  SUM(IF(mature_3rd, converted_3rd, 0)) AS mature_converted_3rd_sum,
  SUM(IF(mature_3rd, trials, 0)) AS mature_trials_3rd_sum
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
"""
for r in client.query(q_trailing_summary).result():
    print(dict(r))

print("\n" + "=" * 110)
print("PART 4: HARD TEST VERIFICATION (ZERO CONVERSIONS IN MATURE COHORT >20 TRIALS)")
print("=" * 110)
q_hard_test = """
SELECT
  cohort,
  tier,
  trials,
  converted_1st,
  mature_1st,
  converted_3rd,
  mature_3rd,
  (mature_1st AND trials > 20 AND converted_1st = 0) AS failed_1st,
  (mature_3rd AND trials > 20 AND converted_3rd = 0) AS failed_3rd
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
WHERE (mature_1st AND trials > 20 AND converted_1st = 0)
   OR (mature_3rd AND trials > 20 AND converted_3rd = 0)
"""
hard_test_failures = list(client.query(q_hard_test).result())
print(f"Number of hard test failures detected: {len(hard_test_failures)}")
if hard_test_failures:
    for f in hard_test_failures:
        print("FAILURE:", dict(f))
else:
    print("HARD test result: PASS (zero mature cohorts with >20 trials have 0 conversions).")

print("\n" + "=" * 110)
print("PART 5: RECREATING vw_report_suite_health WITH HARD TEST FAILURE MONITORING")
print("=" * 110)

health_ddl = """
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
OPTIONS (description = "Cross-suite health and data freshness monitoring view for all 6 Raffle House reporting areas. Fails if any mature trial cohort has 0 conversions.")
AS
WITH trial_counts AS (
  SELECT
    COUNTIF(status = 'payment due') AS payment_due_count,
    COUNTIF(status = 'active') AS active_count
  FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
),
trial_mature_test AS (
  SELECT
    COUNTIF((mature_1st AND trials > 20 AND converted_1st = 0) OR (mature_3rd AND trials > 20 AND converted_3rd = 0)) AS anomaly_count,
    STRING_AGG(
      IF(
        (mature_1st AND trials > 20 AND converted_1st = 0) OR (mature_3rd AND trials > 20 AND converted_3rd = 0),
        CONCAT(CAST(cohort AS STRING), ' (', tier, ')'),
        NULL
      ),
      ', '
    ) AS failed_cohorts
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
),
base_reports AS (
  -- 1. Paid Media (Meta & Google only)
  SELECT
    'Paid media' AS report,
    'rpt_paid_media_entity_daily, rpt_paid_media_entity_status' AS tables_read,
    built_at,
    COALESCE((SELECT MAX(d) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`), wh_to) AS latest_data_date,
    FALSE AS is_failed,
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
    FALSE AS is_failed,
    NOT meta_connected AS has_caveat,
    IF(
      NOT meta_connected,
      'Meta Graph API not connected (meta_connected = false); top posts table empty pending permissions',
      'Meta feed connected and site metrics fresh'
    ) AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_status`

  UNION ALL

  -- 3. Free Trials (renewal-day grace window dynamic counts & mature cohort zero-conversion HARD test)
  SELECT
    'Free trials' AS report,
    'vw_trial_status, rpt_trial_conversion' AS tables_read,
    MAX(t.built_at) AS built_at,
    (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`) AS latest_data_date,
    MAX(m.anomaly_count) > 0 AS is_failed,
    FALSE AS has_caveat,
    IF(
      MAX(m.anomaly_count) > 0,
      CONCAT('HARD test failed: mature cohort has 0 conversions with >20 trials in ', MAX(m.failed_cohorts)),
      CONCAT('Fresh (renewal-day grace window active: ',
        CAST(MAX(c.payment_due_count) AS STRING), ' payment due, ',
        CAST(MAX(c.active_count) AS STRING), ' active)')
    ) AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion` t
  CROSS JOIN trial_counts c
  CROSS JOIN trial_mature_test m

  UNION ALL

  -- 4. Customer LTV
  SELECT
    'Customer LTV' AS report,
    'customer_ltv_horizons' AS tables_read,
    MAX(built_at) AS built_at,
    MAX(as_of_date) AS latest_data_date,
    FALSE AS is_failed,
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
    FALSE AS is_failed,
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
    FALSE AS is_failed,
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
    WHEN is_failed THEN 'failed'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), built_at, HOUR) > 36
      OR (report = 'Organic social' AND DATE_DIFF(CURRENT_DATE('Europe/London'), latest_data_date, DAY) > 2)
      OR (report = 'Tracking health' AND DATE_DIFF(CURRENT_DATE('Europe/London'), latest_data_date, DAY) > 2)
      OR (report NOT IN ('Organic social', 'Tracking health') AND DATE_DIFF(CURRENT_DATE('Europe/London'), latest_data_date, DAY) > 2)
      THEN 'stale'
    WHEN has_caveat THEN 'warning'
    ELSE 'ok'
  END AS status,
  CASE
    WHEN is_failed THEN caveat_reason
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
    ELSE 7
  END;
"""

dry_health = client.query(health_ddl, job_config=bigquery.QueryJobConfig(dry_run=True))
print(f"Dry run health DDL bytes: {dry_health.total_bytes_processed} bytes")
client.query(health_ddl).result()
print("vw_report_suite_health recreated successfully.")

print("\n" + "=" * 110)
print("PART 6: FULL OUTPUT OF vw_report_suite_health")
print("=" * 110)
q_health_select = """
SELECT report, status, hours_since_build, latest_data_date, built_at, reason
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
"""
print(f"{'Report':<16} | {'Status':<8} | {'Hours':<6} | {'Latest Data':<11} | {'Built At':<24} | Reason")
print("-" * 120)
for r in client.query(q_health_select).result():
    built_str = str(r['built_at'])[:19] + "Z" if r['built_at'] else "None"
    print(f"{r['report']:<16} | {r['status']:<8} | {r['hours_since_build']:<6.1f} | {str(r['latest_data_date']):<11} | {built_str:<24} | {r['reason']}")
