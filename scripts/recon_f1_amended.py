#!/usr/bin/env python3
"""
Reconciliation and Execution Script for F1 Amendment:
- payment due = no paid payment AND today within first_renewal_date to first_renewal_date + 15 days AND subscription status in ('ACTIVE','MONTHLY_PAYMENT_FAILED')
- status in ('MONTHLY_PAYMENT_ACTION_REQUIRED', 'PAUSED', 'CANCELLED', 'MONTHLY_CANCELLED', 'MONTHLY_CANCELLED_FINISHED', 'FINISHED') is lapsed
- do not use failure count
- do not use next_purchase_date for window; days_left uses first_renewal_date
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
print("F1-AMEND.1 CONTROL TOTALS: rpt_trial_conversion BEFORE UPDATE")
print("=" * 100)
q_ctrl = """
SELECT cohort, tier, trials, converted_1st, conversion_rate_1st, converted_3rd, conversion_rate_3rd, mature_1st, mature_3rd
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
ORDER BY cohort
"""
ctrl_before = list(client.query(q_ctrl).result())
for r in ctrl_before:
    print(dict(r))

print("\n" + "=" * 100)
print("F1-AMEND.2 vw_trial_status COUNTS BEFORE UPDATE")
print("=" * 100)
q_status_before = """
SELECT
  status,
  COUNT(*) AS total_trials,
  COUNTIF(days_in > 35) AS trials_days_gt_35,
  COUNTIF(days_in > 38) AS trials_days_gt_38,
  COUNTIF(cohort = '2026-10-01') AS october_cohort_trials,
  COUNTIF(cohort < '2026-10-01') AS prior_cohort_trials,
  COUNTIF(cohort > '2026-10-01') AS november_cohort_trials
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
GROUP BY 1
ORDER BY 1
"""
for r in client.query(q_status_before).result():
    print(dict(r))

print("\n" + "=" * 100)
print("F1-AMEND.3 RECREATING vw_trial_status WITH AMENDED LOGIC")
print("=" * 100)

ddl_view = """
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
OPTIONS (description = "One row per genuine special-offer free trial (1st trial per customer with £0 ACCEPTED transaction, < 10 trials): date-based status (active / converted / lapsed / payment due), days_left, and 1st/3rd paid conversion details.")
AS
WITH sub_tx AS (
  SELECT
    subscription_key,
    MIN(IF(is_free_entry, purchase_at, NULL)) AS first_free_at,
    COUNTIF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE)) AS paid_payments,
    ROUND(SUM(IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), revenue_gbp, 0)), 2) AS paid_revenue_gbp,
    ARRAY_AGG(
      IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), purchase_date, NULL)
      IGNORE NULLS
      ORDER BY purchase_at, transaction_id
    ) AS paid_dates
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE subscription_key IS NOT NULL
  GROUP BY subscription_key
)
SELECT
  s.subscription_key,
  s.customer_key,
  tx.first_free_at,
  DATE(tx.first_free_at, 'Europe/London') AS start_date,
  DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH) AS cohort,
  DATE(s.next_purchase_date) AS next_purchase_date,
  DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH) AS first_renewal_date,
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
  s.monthly_value_gbp,
  DATE_DIFF(CURRENT_DATE('Europe/London'), DATE(tx.first_free_at, 'Europe/London'), DAY) AS days_in,
  DATE_DIFF(DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH), CURRENT_DATE('Europe/London'), DAY) AS days_left,
  CASE
    WHEN COALESCE(tx.paid_payments, 0) >= 1 THEN 'converted'
    WHEN s.status IN ('MONTHLY_PAYMENT_ACTION_REQUIRED', 'PAUSED', 'CANCELLED', 'MONTHLY_CANCELLED', 'MONTHLY_CANCELLED_FINISHED', 'FINISHED') THEN 'lapsed'
    WHEN CURRENT_DATE('Europe/London') < DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH) THEN 'active'
    WHEN CURRENT_DATE('Europe/London') BETWEEN DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH)
                                           AND DATE_ADD(DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH), INTERVAL 15 DAY)
         AND s.status IN ('ACTIVE', 'MONTHLY_PAYMENT_FAILED') THEN 'payment due'
    ELSE 'lapsed'
  END AS status,
  tx.paid_dates[SAFE_OFFSET(0)] AS converted_date,
  tx.paid_dates[SAFE_OFFSET(2)] AS third_paid_date,
  IF(
    COALESCE(tx.paid_payments, 0) >= 1,
    GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(0)], DATE(tx.first_free_at), DAY), 0),
    NULL
  ) AS days_to_convert,
  IF(
    COALESCE(tx.paid_payments, 0) >= 3,
    GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(2)], DATE(tx.first_free_at), DAY), 0),
    NULL
  ) AS days_to_3rd_payment,
  COALESCE(tx.paid_payments, 0) >= 1 AS converted_1st,
  COALESCE(tx.paid_payments, 0) >= 3 AS converted_3rd,
  COALESCE(tx.paid_payments, 0) AS paid_payments,
  COALESCE(tx.paid_revenue_gbp, NUMERIC '0.00') AS paid_revenue_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
JOIN sub_tx tx
  ON s.subscription_key = tx.subscription_key
LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model` m
  ON s.subscription_model_key = m.subscription_model_key
WHERE s.is_special_offer_trial
  AND tx.first_free_at IS NOT NULL;
"""

dry_ddl = client.query(ddl_view, job_config=bigquery.QueryJobConfig(dry_run=True))
print(f"Dry run DDL bytes: {dry_ddl.total_bytes_processed} bytes")
client.query(ddl_view).result()
print("vw_trial_status recreated successfully with amended F1 logic.")

print("\n" + "=" * 100)
print("F1-AMEND.4 vw_trial_status COUNTS AFTER UPDATE")
print("=" * 100)
status_after = list(client.query(q_status_before).result())
for r in status_after:
    print(dict(r))

print("\n" + "=" * 100)
print("F1-AMEND.5 CONFIRMATION: ANY TRIALS IN ACTIVE OR PAYMENT DUE WITH days_in > 38?")
print("=" * 100)
q_check = """
SELECT COUNT(*) AS invalid_trials_count
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
WHERE status IN ('active', 'payment due')
  AND days_in > 38
"""
for r in client.query(q_check).result():
    print(dict(r))

print("\n" + "=" * 100)
print("F1-AMEND.6 CONTROL TOTALS: rpt_trial_conversion AFTER UPDATE")
print("=" * 100)
ctrl_after = list(client.query(q_ctrl).result())
for r in ctrl_after:
    print(dict(r))

match = (
    len(ctrl_before) == len(ctrl_after)
    and all(dict(b) == dict(a) for b, a in zip(ctrl_before, ctrl_after))
)
print(f"\nControl totals match exactly: {match}")
