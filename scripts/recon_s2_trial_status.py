#!/usr/bin/env python3
"""
Reconciliation and Verification Script for S2: Trial Status Renewal-Day Grace
Outputs:
- S2.a Current (previous) status logic from vw_trial_status
- S2.b Status distribution after renewal-day grace window
- S2.c Inspection and classification of the future-dated trials
- S2.d Confirmation that active trials > 0 and 31 Oct trials are not lapsed
- Control totals before vs after on rpt_trial_conversion
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
print("S2.a PREVIOUS STATUS LOGIC FROM vw_trial_status")
print("=" * 80)
print("""  CASE
    WHEN COALESCE(tx.paid_payments, 0) >= 1 THEN 'converted'
    WHEN CURRENT_DATE('Europe/London') < DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at), MONTH), INTERVAL 1 MONTH) THEN 'active'
    ELSE 'lapsed'
  END AS status""")

print("\n" + "=" * 80)
print("S2.b STATUS DISTRIBUTION AFTER RENEWAL-DAY GRACE WINDOW")
print("=" * 80)
q_dist = """
SELECT
  status,
  COUNT(*) AS trial_count,
  COUNTIF(cohort = '2026-10-01') AS oct_cohort_count,
  COUNTIF(DATE(first_free_at) = '2026-10-31') AS oct31_count
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
GROUP BY 1
ORDER BY 2 DESC
"""
for r in client.query(q_dist).result():
    print(dict(r))

print("\n" + "=" * 80)
print("S2.c INSPECTION OF FUTURE-DATED TRIALS (next_purchase_date > 2026-11-01, 0 paid)")
print("=" * 80)
q_future = """
SELECT
  v.subscription_key,
  v.customer_key,
  s.status AS woo_status,
  v.tier,
  DATE(v.first_free_at) AS start_date,
  v.cohort,
  v.next_purchase_date,
  v.days_in,
  v.days_left,
  v.status AS new_view_status,
  (
    SELECT COUNT(*) 
    FROM `rh-ga4-491819.rh_warehouse.fact_transactions` t 
    WHERE t.subscription_id = v.subscription_key
  ) AS total_tx_attempts,
  (
    SELECT STRING_AGG(DISTINCT payment_status, ', ')
    FROM `rh-ga4-491819.rh_warehouse.fact_transactions` t 
    WHERE t.subscription_id = v.subscription_key
  ) AS tx_payment_statuses
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status` v
JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
  ON v.subscription_key = s.subscription_key
WHERE v.paid_payments = 0
  AND v.next_purchase_date > '2026-11-01'
ORDER BY v.next_purchase_date, v.first_free_at
"""
for r in client.query(q_future).result():
    print(dict(r))

print("\n" + "=" * 80)
print("S2.d CONFIRMATION OF 31 OCT TRIALS STATUS")
print("=" * 80)
q_oct31 = """
SELECT
  subscription_key,
  customer_key,
  DATE(first_free_at) AS start_date,
  next_purchase_date,
  paid_payments,
  status
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
WHERE DATE(first_free_at) = '2026-10-31'
ORDER BY subscription_key
"""
for r in client.query(q_oct31).result():
    print(dict(r))

print("\n" + "=" * 80)
print("S2 CONTROL TOTALS: rpt_trial_conversion (Mature Cohorts)")
print("=" * 80)
q_ctrl = """
SELECT
  cohort,
  tier,
  trials,
  converted_1st,
  converted_3rd,
  conversion_rate_1st,
  conversion_rate_3rd,
  trailing_conversion_rate_1st,
  trailing_conversion_rate_3rd,
  mature
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
ORDER BY cohort, tier
"""
for r in client.query(q_ctrl).result():
    print(dict(r))
