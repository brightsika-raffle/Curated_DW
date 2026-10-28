import os
import subprocess
import sys
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

KNOWN_EXPLOIT_ACCOUNTS = (
    "66d231300ff155003e747fcc",
    "69dfaa9740447116ffe02052",
    "67e8bfdb8b1def003ebee6e0",
    "6911fdcc76ae9a44158d1022",
)

def get_bq_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, credentials=Credentials(token), location=LOCATION)

def run_q(client, label, sql):
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry = client.query(sql, job_config=dry_cfg)
    if dry.total_bytes_processed > 1024**3:
        raise RuntimeError(f"Query [{label}] exceeds 1 GB limit: {dry.total_bytes_processed}")
    print(f"[Dry run ({label}): {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024**2):.2f} MB)]")
    cfg = bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})
    return list(client.query(sql, job_config=cfg).result())

def main():
    client = get_bq_client()

    print("================================================================================")
    print("STEP 3.1: WATERFALL OF SPECIAL-OFFER SUBSCRIPTIONS (MAY TO OCTOBER 2026)")
    print("================================================================================")

    sql_waterfall = f"""
    WITH sub_tx AS (
      SELECT
        subscription_id,
        COUNT(*) AS total_tx,
        COUNTIF(payment_status = 'ACCEPTED') AS accepted_tx,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost > 0) AS accepted_paid_tx,
        COUNTIF(payment_status = 'FREE') AS status_free_tx,
        COUNTIF(payment_status = 'REFUNDED') AS status_refunded_tx,
        STRING_AGG(DISTINCT payment_status, ',' ORDER BY payment_status) AS all_statuses
      FROM `{PROJECT}.rh_warehouse.fact_transactions`
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    cust_trial_counts AS (
      SELECT
        s.customer_id,
        COUNTIF(
          s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
          AND COALESCE(t.accepted_free_tx, 0) > 0
        ) AS customer_free_trial_subs,
        COUNT(*) AS customer_all_special_offer_subs
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      LEFT JOIN sub_tx t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
      GROUP BY 1
    ),
    classified AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        FORMAT_DATE('%Y-%m', DATE(s.created_at)) AS cohort_month,
        s.status AS woo_status,
        COALESCE(t.total_tx, 0) AS total_tx,
        COALESCE(t.accepted_free_tx, 0) AS accepted_free_tx,
        COALESCE(t.accepted_paid_tx, 0) AS accepted_paid_tx,
        COALESCE(t.status_free_tx, 0) AS status_free_tx,
        c.customer_free_trial_subs,
        CASE
          WHEN s.status IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
            THEN '1_abandoned'
          WHEN COALESCE(t.total_tx, 0) = 0
            THEN '2_empty_cancelled'
          WHEN COALESCE(t.accepted_free_tx, 0) = 0 AND t.all_statuses = 'FREE'
            THEN '3a_free_only'
          WHEN COALESCE(t.accepted_free_tx, 0) = 0
            THEN '3b_other_no_0_accepted'
          WHEN c.customer_free_trial_subs > 3
            OR s.customer_id IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022')
            THEN '4_exploit'
          ELSE '5_genuine'
        END AS waterfall_bucket
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      LEFT JOIN sub_tx t ON s.subscription_id = t.subscription_id
      LEFT JOIN cust_trial_counts c ON s.customer_id = c.customer_id
      WHERE s.created_by = 'special-offer'
    )
    SELECT
      cohort_month,
      COUNT(*) AS all_special_offer,
      COUNTIF(waterfall_bucket = '1_abandoned') AS abandoned,
      COUNTIF(waterfall_bucket = '2_empty_cancelled') AS empty_cancelled,
      COUNTIF(waterfall_bucket = '3a_free_only') AS free_only,
      COUNTIF(waterfall_bucket = '3b_other_no_0_accepted') AS paid_or_refunded_no_0_accepted,
      COUNTIF(waterfall_bucket IN ('3a_free_only', '3b_other_no_0_accepted')) AS total_no_0_accepted_non_empty,
      COUNTIF(waterfall_bucket = '4_exploit') AS exploit,
      COUNTIF(waterfall_bucket = '5_genuine') AS genuine_trials,
      COUNT(*) - (
        COUNTIF(waterfall_bucket = '1_abandoned') +
        COUNTIF(waterfall_bucket = '2_empty_cancelled') +
        COUNTIF(waterfall_bucket IN ('3a_free_only', '3b_other_no_0_accepted')) +
        COUNTIF(waterfall_bucket = '4_exploit') +
        COUNTIF(waterfall_bucket = '5_genuine')
      ) AS waterfall_check_diff
    FROM classified
    GROUP BY 1
    ORDER BY 1
    """
    wf_rows = run_q(client, "waterfall by month", sql_waterfall)
    print(f"{'Month':<10} {'All SO':>8} {'Aband':>8} {'Empty':>8} {'FREEOnly':>9} {'No£0Acc':>9} {'Exploit':>9} {'Genuine':>9} {'Diff':>6}")
    print("-" * 84)
    tot = [0] * 8
    by_m = {}
    for r in wf_rows:
        by_m[r["cohort_month"]] = r
        vals = [
            r["all_special_offer"],
            r["abandoned"],
            r["empty_cancelled"],
            r["free_only"],
            r["paid_or_refunded_no_0_accepted"],
            r["exploit"],
            r["genuine_trials"],
            r["waterfall_check_diff"],
        ]
        for i in range(8):
            tot[i] += vals[i]
        print(
            f"{r['cohort_month']:<10} {vals[0]:>8} {vals[1]:>8} {vals[2]:>8} "
            f"{vals[3]:>9} {vals[4]:>9} {vals[5]:>9} {vals[6]:>9} {vals[7]:>6}"
        )
    print("-" * 84)
    print(
        f"{'TOTAL':<10} {tot[0]:>8} {tot[1]:>8} {tot[2]:>8} "
        f"{tot[3]:>9} {tot[4]:>9} {tot[5]:>9} {tot[6]:>9} {tot[7]:>6}"
    )

    print("\n--- May to August Genuine Trial Reconciliation vs Target (417 / 81 / 189 / 163) ---")
    targets = {"2026-05": 417, "2026-06": 81, "2026-07": 189, "2026-08": 163}
    all_pass = True
    for m, tgt in targets.items():
        ours = by_m[m]["genuine_trials"]
        diff = ours - tgt
        pct = (diff / tgt) * 100.0
        within_1pct = abs(pct) <= 1.0
        if not within_1pct:
            all_pass = False
        status_str = "PASS (<= 1%)" if within_1pct else "FAIL (> 1%)"
        print(f"  {m}: ours={ours:4d} vs target={tgt:4d} | diff={diff:+4d} ({pct:+6.2f}%) -> {status_str}")
    print(f"Overall Step 3.1 May-Aug 1% tolerance gate: {'PASS' if all_pass else 'FAIL'}")

    print("\n--- Diagnostic: Why May-July Genuine Counts Differ from 417 / 81 / 189 / 163 ---")
    sql_diag = f"""
    WITH sub_tx AS (
      SELECT
        subscription_id,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        MIN(IF(payment_status = 'ACCEPTED' AND total_cost = 0, COALESCE(purchase_date, created_at), NULL)) AS first_free_at
      FROM `{PROJECT}.rh_warehouse.fact_transactions`
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    valid_free_subs AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        s.created_at,
        t.first_free_at,
        COUNT(*) OVER (PARTITION BY s.customer_id) AS cust_trial_count,
        ROW_NUMBER() OVER (PARTITION BY s.customer_id ORDER BY s.created_at, s.subscription_id) AS rn_by_created,
        ROW_NUMBER() OVER (PARTITION BY s.customer_id ORDER BY t.first_free_at, s.subscription_id) AS rn_by_free_tx
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      JOIN sub_tx t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
        AND s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
        AND t.accepted_free_tx > 0
    )
    SELECT
      cust_trial_count AS trials_per_customer,
      COUNT(DISTINCT customer_id) AS distinct_customers,
      COUNT(*) AS total_trial_subs,
      COUNTIF(FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-05') AS may_subs,
      COUNTIF(FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-06') AS jun_subs,
      COUNTIF(FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-07') AS jul_subs,
      COUNTIF(FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-08') AS aug_subs,
      COUNTIF(FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-09') AS sep_subs,
      COUNTIF(FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-10') AS oct_subs
    FROM valid_free_subs
    GROUP BY 1
    ORDER BY 1
    """
    diag_rows = run_q(client, "customer trial multiplicity breakdown", sql_diag)
    print(f"{'N_trials/cust':>14} {'Customers':>10} {'Subs':>8} {'May':>6} {'Jun':>6} {'Jul':>6} {'Aug':>6} {'Sep':>6} {'Oct':>6}")
    print("-" * 76)
    for r in diag_rows:
        print(
            f"{r['trials_per_customer']:>14} {r['distinct_customers']:>10} {r['total_trial_subs']:>8} "
            f"{r['may_subs']:>6} {r['jun_subs']:>6} {r['jul_subs']:>6} {r['aug_subs']:>6} {r['sep_subs']:>6} {r['oct_subs']:>6}"
        )

    sql_alt = f"""
    WITH sub_tx AS (
      SELECT
        subscription_id,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        MIN(IF(payment_status = 'ACCEPTED' AND total_cost = 0, COALESCE(purchase_date, created_at), NULL)) AS first_free_at
      FROM `{PROJECT}.rh_warehouse.fact_transactions`
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    valid_free_subs AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        s.created_at,
        t.first_free_at,
        COUNT(*) OVER (PARTITION BY s.customer_id) AS cust_trial_count,
        ROW_NUMBER() OVER (PARTITION BY s.customer_id ORDER BY s.created_at, s.subscription_id) AS rn_by_created,
        ROW_NUMBER() OVER (PARTITION BY s.customer_id ORDER BY t.first_free_at, s.subscription_id) AS rn_by_free_tx
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      JOIN sub_tx t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
        AND s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
        AND t.accepted_free_tx > 0
    )
    SELECT
      'A. Subs where cust_trial_count <= 3 (by sub created_at)' AS definition,
      COUNTIF(cust_trial_count <= 3 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-05') AS may,
      COUNTIF(cust_trial_count <= 3 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-06') AS jun,
      COUNTIF(cust_trial_count <= 3 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-07') AS jul,
      COUNTIF(cust_trial_count <= 3 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-08') AS aug
    FROM valid_free_subs
    UNION ALL
    SELECT
      'B. 1st trial/cust where cust_trial_count <= 3 (by sub created_at)',
      COUNTIF(cust_trial_count <= 3 AND rn_by_created = 1 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-05'),
      COUNTIF(cust_trial_count <= 3 AND rn_by_created = 1 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-06'),
      COUNTIF(cust_trial_count <= 3 AND rn_by_created = 1 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-07'),
      COUNTIF(cust_trial_count <= 3 AND rn_by_created = 1 AND FORMAT_DATE('%Y-%m', DATE(created_at)) = '2026-08')
    FROM valid_free_subs
    UNION ALL
    SELECT
      'C. 1st trial/cust where cust_trial_count <= 4 (by £0 tx date)',
      COUNTIF(cust_trial_count <= 4 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-05'),
      COUNTIF(cust_trial_count <= 4 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-06'),
      COUNTIF(cust_trial_count <= 4 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-07'),
      COUNTIF(cust_trial_count <= 4 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-08')
    FROM valid_free_subs
    UNION ALL
    SELECT
      'D. 1st trial/cust excluding top 2 exploit accts (by £0 tx date)',
      COUNTIF(cust_trial_count <= 100 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-05'),
      COUNTIF(cust_trial_count <= 100 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-06'),
      COUNTIF(cust_trial_count <= 100 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-07'),
      COUNTIF(cust_trial_count <= 100 AND rn_by_free_tx = 1 AND FORMAT_DATE('%Y-%m', DATE(first_free_at)) = '2026-08')
    FROM valid_free_subs
    ORDER BY 1
    """
    print("\n--- Alternative Cohort Counting Definitions (May-Aug) ---")
    for r in run_q(client, "alternative definitions", sql_alt):
        print(f"  {r['definition']:<66}: May={r['may']:4d}, Jun={r['jun']:4d}, Jul={r['jul']:4d}, Aug={r['aug']:4d}")

    print("\n================================================================================")
    print("STEP 3.2: PER COHORT & TIER CONVERSION (1ST AND 3RD PAID PAYMENT) & MATURITY")
    print("================================================================================")

    sql_cohorts = f"""
    WITH sub_tx AS (
      SELECT
        subscription_id,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost > 0) AS paid_tx_count,
        ARRAY_AGG(
          IF(payment_status = 'ACCEPTED' AND total_cost > 0, DATE(COALESCE(purchase_date, created_at)), NULL)
          IGNORE NULLS
          ORDER BY COALESCE(purchase_date, created_at), transaction_id
        ) AS paid_dates
      FROM `{PROJECT}.rh_warehouse.fact_transactions`
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    cust_trial_counts AS (
      SELECT
        s.customer_id,
        COUNTIF(
          s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
          AND COALESCE(t.accepted_free_tx, 0) > 0
        ) AS customer_free_trial_subs
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      LEFT JOIN sub_tx t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
      GROUP BY 1
    ),
    genuine_trials AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        DATE_TRUNC(DATE(s.created_at), MONTH) AS cohort,
        DATE(s.created_at) AS start_date,
        DATE(s.next_purchase_date) AS next_purchase_date,
        CAST(s.total_cost AS NUMERIC) / 100 AS monthly_value_gbp,
        CASE
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (10, 15) THEN 'Bronze'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (20, 25, 30) THEN 'Silver'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (35, 50) THEN 'Gold'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (55, 75) THEN 'Platinum'
          ELSE 'Other'
        END AS tier,
        CASE
          WHEN CAST(s.total_cost AS NUMERIC) / 100 < 20 THEN 'Low'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 < 40 THEN 'Mid'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 < 60 THEN 'High'
          ELSE 'Very High'
        END AS tier_band,
        COALESCE(t.paid_tx_count, 0) AS paid_tx_count,
        t.paid_dates[SAFE_OFFSET(0)] AS first_paid_date,
        t.paid_dates[SAFE_OFFSET(2)] AS third_paid_date,
        COALESCE(t.paid_tx_count, 0) >= 1 AS converted_1st,
        COALESCE(t.paid_tx_count, 0) >= 3 AS converted_3rd,
        CASE
          WHEN COALESCE(t.paid_tx_count, 0) >= 1 THEN 'converted'
          WHEN DATE(s.next_purchase_date) > CURRENT_DATE('Europe/London') THEN 'active'
          ELSE 'lapsed'
        END AS trial_status,
        DATE_DIFF(DATE(s.next_purchase_date), CURRENT_DATE('Europe/London'), DAY) AS days_left,
        IF(COALESCE(t.paid_tx_count, 0) >= 1, GREATEST(DATE_DIFF(t.paid_dates[SAFE_OFFSET(0)], DATE(s.created_at), DAY), 0), NULL) AS days_to_1st,
        IF(COALESCE(t.paid_tx_count, 0) >= 3, GREATEST(DATE_DIFF(t.paid_dates[SAFE_OFFSET(2)], DATE(s.created_at), DAY), 0), NULL) AS days_to_3rd
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      JOIN sub_tx t ON s.subscription_id = t.subscription_id
      JOIN cust_trial_counts c ON s.customer_id = c.customer_id
      WHERE s.created_by = 'special-offer'
        AND s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
        AND s.customer_id IS NOT NULL
        AND s.created_at IS NOT NULL
        AND t.accepted_free_tx > 0
        AND c.customer_free_trial_subs <= 3
        AND s.customer_id NOT IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022')
    ),
    med_cohort AS (
      SELECT DISTINCT
        cohort,
        PERCENTILE_CONT(CAST(days_to_1st AS FLOAT64), 0.5) OVER (PARTITION BY cohort) AS med_days_1st,
        PERCENTILE_CONT(CAST(days_to_3rd AS FLOAT64), 0.5) OVER (PARTITION BY cohort) AS med_days_3rd
      FROM genuine_trials
    )
    SELECT
      g.cohort,
      COUNT(*) AS trials,
      COUNTIF(g.trial_status = 'active') AS active_raw_npd,
      COUNTIF(g.trial_status = 'active' AND g.cohort = '2026-10-01') AS active_first_cycle,
      COUNTIF(g.trial_status = 'lapsed') AS lapsed_trials,
      COUNTIF(g.converted_1st) AS converted_1st,
      ROUND(SAFE_DIVIDE(COUNTIF(g.converted_1st), COUNT(*)), 4) AS cvr_1st,
      ROUND(AVG(g.days_to_1st), 2) AS mean_days_1st,
      ROUND(ANY_VALUE(m.med_days_1st), 2) AS median_days_1st,
      COUNTIF(g.converted_3rd) AS converted_3rd,
      ROUND(SAFE_DIVIDE(COUNTIF(g.converted_3rd), COUNT(*)), 4) AS cvr_3rd,
      ROUND(AVG(g.days_to_3rd), 2) AS mean_days_3rd,
      ROUND(ANY_VALUE(m.med_days_3rd), 2) AS median_days_3rd,
      DATE_ADD(g.cohort, INTERVAL 1 MONTH) <= CURRENT_DATE('Europe/London') AS mature_1st,
      DATE_ADD(g.cohort, INTERVAL 3 MONTH) <= CURRENT_DATE('Europe/London') AS mature_3rd
    FROM genuine_trials g
    LEFT JOIN med_cohort m USING (cohort)
    GROUP BY 1
    ORDER BY 1
    """
    c_rows = run_q(client, "cohort conversion summary", sql_cohorts)
    print(f"{'Cohort':<12} {'Trials':>6} {'ActRaw':>6} {'Act1C':>5} {'Lps':>5} {'Conv1':>6} {'CVR1':>8} {'Mean1':>6} {'Med1':>6} {'Conv3':>6} {'CVR3':>8} {'Mean3':>6} {'Med3':>6} {'Mat1':>6} {'Mat3':>6}")
    print("-" * 120)
    for r in c_rows:
        print(
            f"{str(r['cohort']):<12} {r['trials']:>6} {r['active_raw_npd']:>6} {r['active_first_cycle']:>5} {r['lapsed_trials']:>5} "
            f"{r['converted_1st']:>6} {float(r['cvr_1st']):>8.4f} {str(r['mean_days_1st']):>6} {str(r['median_days_1st']):>6} "
            f"{r['converted_3rd']:>6} {float(r['cvr_3rd']):>8.4f} {str(r['mean_days_3rd']):>6} {str(r['median_days_3rd']):>6} "
            f"{str(r['mature_1st']):>6} {str(r['mature_3rd']):>6}"
        )

    sql_cohort_tier = f"""
    WITH sub_tx AS (
      SELECT
        subscription_id,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost > 0) AS paid_tx_count,
        ARRAY_AGG(
          IF(payment_status = 'ACCEPTED' AND total_cost > 0, DATE(COALESCE(purchase_date, created_at)), NULL)
          IGNORE NULLS
          ORDER BY COALESCE(purchase_date, created_at), transaction_id
        ) AS paid_dates
      FROM `{PROJECT}.rh_warehouse.fact_transactions`
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    cust_trial_counts AS (
      SELECT
        s.customer_id,
        COUNTIF(
          s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
          AND COALESCE(t.accepted_free_tx, 0) > 0
        ) AS customer_free_trial_subs
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      LEFT JOIN sub_tx t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
      GROUP BY 1
    ),
    genuine_trials AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        DATE_TRUNC(DATE(s.created_at), MONTH) AS cohort,
        DATE(s.created_at) AS start_date,
        DATE(s.next_purchase_date) AS next_purchase_date,
        CASE
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (10, 15) THEN 'Bronze'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (20, 25, 30) THEN 'Silver'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (35, 50) THEN 'Gold'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (55, 75) THEN 'Platinum'
          ELSE 'Other'
        END AS tier,
        COALESCE(t.paid_tx_count, 0) >= 1 AS converted_1st,
        COALESCE(t.paid_tx_count, 0) >= 3 AS converted_3rd,
        CASE
          WHEN COALESCE(t.paid_tx_count, 0) >= 1 THEN 'converted'
          WHEN DATE(s.next_purchase_date) > CURRENT_DATE('Europe/London') THEN 'active'
          ELSE 'lapsed'
        END AS trial_status,
        IF(COALESCE(t.paid_tx_count, 0) >= 1, GREATEST(DATE_DIFF(t.paid_dates[SAFE_OFFSET(0)], DATE(s.created_at), DAY), 0), NULL) AS days_to_1st,
        IF(COALESCE(t.paid_tx_count, 0) >= 3, GREATEST(DATE_DIFF(t.paid_dates[SAFE_OFFSET(2)], DATE(s.created_at), DAY), 0), NULL) AS days_to_3rd
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      JOIN sub_tx t ON s.subscription_id = t.subscription_id
      JOIN cust_trial_counts c ON s.customer_id = c.customer_id
      WHERE s.created_by = 'special-offer'
        AND s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
        AND s.customer_id IS NOT NULL
        AND s.created_at IS NOT NULL
        AND t.accepted_free_tx > 0
        AND c.customer_free_trial_subs <= 3
        AND s.customer_id NOT IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022')
    ),
    med_ct AS (
      SELECT DISTINCT
        cohort,
        tier,
        PERCENTILE_CONT(CAST(days_to_1st AS FLOAT64), 0.5) OVER (PARTITION BY cohort, tier) AS med_days_1st,
        PERCENTILE_CONT(CAST(days_to_3rd AS FLOAT64), 0.5) OVER (PARTITION BY cohort, tier) AS med_days_3rd
      FROM genuine_trials
    )
    SELECT
      g.cohort,
      g.tier,
      COUNT(*) AS trials,
      COUNTIF(g.trial_status = 'active') AS active_raw_npd,
      COUNTIF(g.trial_status = 'active' AND g.cohort = '2026-10-01') AS active_first_cycle,
      COUNTIF(g.trial_status = 'lapsed') AS lapsed_trials,
      COUNTIF(g.converted_1st) AS converted_1st,
      ROUND(SAFE_DIVIDE(COUNTIF(g.converted_1st), COUNT(*)), 4) AS cvr_1st,
      ROUND(AVG(g.days_to_1st), 2) AS mean_days_1st,
      ROUND(ANY_VALUE(m.med_days_1st), 2) AS median_days_1st,
      COUNTIF(g.converted_3rd) AS converted_3rd,
      ROUND(SAFE_DIVIDE(COUNTIF(g.converted_3rd), COUNT(*)), 4) AS cvr_3rd,
      ROUND(AVG(g.days_to_3rd), 2) AS mean_days_3rd,
      ROUND(ANY_VALUE(m.med_days_3rd), 2) AS median_days_3rd,
      DATE_ADD(g.cohort, INTERVAL 1 MONTH) <= CURRENT_DATE('Europe/London') AS mature_1st,
      DATE_ADD(g.cohort, INTERVAL 3 MONTH) <= CURRENT_DATE('Europe/London') AS mature_3rd
    FROM genuine_trials g
    LEFT JOIN med_ct m USING (cohort, tier)
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    ct_rows = run_q(client, "cohort x tier conversion", sql_cohort_tier)
    print("\n--- Per Cohort x Tier Breakdown ---")
    for r in ct_rows:
        print(
            f"  cohort={r['cohort']} | tier={r['tier']:<8} | trials={r['trials']:4d} | act_raw={r['active_raw_npd']:3d} | act_1c={r['active_first_cycle']:3d} | lps={r['lapsed_trials']:3d} | "
            f"conv1={r['converted_1st']:3d} ({float(r['cvr_1st'])*100:5.2f}%, mean={r['mean_days_1st']}d, med={r['median_days_1st']}d) | "
            f"conv3={r['converted_3rd']:3d} ({float(r['cvr_3rd'])*100:5.2f}%, mean={r['mean_days_3rd']}d, med={r['median_days_3rd']}d) | "
            f"mat1={r['mature_1st']}, mat3={r['mature_3rd']}"
        )

    print("\n================================================================================")
    print("STEP 3.3: ACTIVE TRIAL COUNT TODAY & TOTAL EFV (USING 1ST-PAYMENT TRAILING RATE)")
    print("================================================================================")

    sql_efv = f"""
    WITH tx AS (
      SELECT customer_key AS customer_id, DATE(purchase_date) AS d, revenue_gbp AS amt,
        subscription_key IS NULL AS one_off, subscription_key
      FROM `{PROJECT}.rh_curated_Bright_Test.vw_fact_transactions`
      WHERE NOT IFNULL(is_free_entry, FALSE) AND revenue_gbp > 0 AND customer_key != 'UNKNOWN_DELETED'
    ),
    sub_pay AS (
      SELECT customer_id, subscription_key, d, amt,
        CASE
          WHEN amt IN (10, 15) THEN 'Low'
          WHEN amt IN (20, 25, 30) THEN 'Mid'
          WHEN amt IN (35, 50) THEN 'High'
          WHEN amt IN (55, 75) THEN 'Very High'
          ELSE CASE
            WHEN ABS(amt - 10) <= ABS(amt - 15)
                 AND ABS(amt - 10) < LEAST(ABS(amt-20), ABS(amt-30), ABS(amt-35), ABS(amt-50), ABS(amt-55), ABS(amt-75)) THEN 'Low'
            WHEN LEAST(ABS(amt-20), ABS(amt-25), ABS(amt-30)) <= LEAST(ABS(amt-35), ABS(amt-50), ABS(amt-55), ABS(amt-75)) THEN 'Mid'
            WHEN LEAST(ABS(amt-35), ABS(amt-50)) <= LEAST(ABS(amt-55), ABS(amt-75)) THEN 'High'
            ELSE 'Very High' END
        END AS tier
      FROM tx WHERE NOT one_off
    ),
    fs_cur AS (
      SELECT subscription_key, customer_key AS customer_id, status
      FROM `{PROJECT}.rh_curated_Bright_Test.vw_fact_subscriptions`
    ),
    cust_modal AS (
      SELECT customer_id, tier AS modal_tier FROM (
        SELECT customer_id, tier, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY COUNT(*) DESC) AS rn
        FROM sub_pay GROUP BY customer_id, tier) WHERE rn = 1
    ),
    tenure_detail AS (
      SELECT t.customer_id, IF(m.modal_tier = 'Very High', 'High', m.modal_tier) AS curve_tier,
        DATE_DIFF(MAX(t.d), MIN(t.d), MONTH) + 1 AS tenure_months,
        NOT LOGICAL_OR(IFNULL(st.active, FALSE)) AS is_churned
      FROM sub_pay t
      JOIN cust_modal m USING (customer_id)
      LEFT JOIN (SELECT customer_id, COUNTIF(status = 'ACTIVE') > 0 AS active FROM fs_cur GROUP BY 1) st USING (customer_id)
      GROUP BY t.customer_id, curve_tier
    ),
    raw_hazard AS (
      SELECT curve_tier, m AS month, COUNT(*) AS at_risk,
        SAFE_DIVIDE(COUNTIF(m = tenure_months AND is_churned), COUNT(*)) AS hazard_rate,
        COUNT(*) >= 50 AS is_reliable
      FROM tenure_detail, UNNEST(GENERATE_ARRAY(0, tenure_months)) AS m
      GROUP BY 1, 2
    ),
    frozen_rate AS (
      SELECT curve_tier, AVG(hazard_rate) AS frozen_hazard FROM (
        SELECT curve_tier, hazard_rate, ROW_NUMBER() OVER (PARTITION BY curve_tier ORDER BY month DESC) AS rn
        FROM raw_hazard WHERE is_reliable) WHERE rn <= 3 GROUP BY 1
    ),
    last_reliable AS (SELECT curve_tier, MAX(month) AS last_reliable FROM raw_hazard WHERE is_reliable GROUP BY 1),
    survival_curve AS (
      SELECT curve_tier AS tier, month,
        EXP(SUM(LN(1 - hazard_rate)) OVER (PARTITION BY curve_tier ORDER BY month ROWS UNBOUNDED PRECEDING)) AS survival_probability
      FROM (
        SELECT f.curve_tier, f.month, IF(f.month <= l.last_reliable, h.hazard_rate, fr.frozen_hazard) AS hazard_rate
        FROM (SELECT t.curve_tier, month FROM (SELECT DISTINCT curve_tier FROM raw_hazard) t, UNNEST(GENERATE_ARRAY(0, 120)) AS month) f
        JOIN last_reliable l ON l.curve_tier = f.curve_tier
        JOIN frozen_rate fr ON fr.curve_tier = f.curve_tier
        LEFT JOIN raw_hazard h ON h.curve_tier = f.curve_tier AND h.month = f.month
      )
    ),
    -- Genuine trials & mature 1st-payment rate
    sub_tx AS (
      SELECT
        subscription_id,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost > 0) AS paid_tx_count
      FROM `{PROJECT}.rh_warehouse.fact_transactions`
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    cust_trial_counts AS (
      SELECT
        s.customer_id,
        COUNTIF(
          s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
          AND COALESCE(t.accepted_free_tx, 0) > 0
        ) AS customer_free_trial_subs
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      LEFT JOIN sub_tx t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
      GROUP BY 1
    ),
    genuine_trials AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        DATE_TRUNC(DATE(s.created_at), MONTH) AS cohort,
        DATE(s.created_at) AS start_date,
        DATE(s.next_purchase_date) AS next_purchase_date,
        CAST(s.total_cost AS NUMERIC) / 100 AS monthly_value_gbp,
        CASE
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (10, 15) THEN 'Bronze'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (20, 25, 30) THEN 'Silver'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (35, 50) THEN 'Gold'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 IN (55, 75) THEN 'Platinum'
          ELSE 'Other'
        END AS tier,
        CASE
          WHEN CAST(s.total_cost AS NUMERIC) / 100 < 20 THEN 'Low'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 < 40 THEN 'Mid'
          WHEN CAST(s.total_cost AS NUMERIC) / 100 < 60 THEN 'High'
          ELSE 'Very High'
        END AS tier_band,
        COALESCE(t.paid_tx_count, 0) >= 1 AS converted_1st,
        COALESCE(t.paid_tx_count, 0) >= 3 AS converted_3rd,
        CASE
          WHEN COALESCE(t.paid_tx_count, 0) >= 1 THEN 'converted'
          WHEN DATE(s.next_purchase_date) > CURRENT_DATE('Europe/London') THEN 'active'
          ELSE 'lapsed'
        END AS trial_status,
        DATE_DIFF(DATE(s.next_purchase_date), CURRENT_DATE('Europe/London'), DAY) AS days_left
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      JOIN sub_tx t ON s.subscription_id = t.subscription_id
      JOIN cust_trial_counts c ON s.customer_id = c.customer_id
      WHERE s.created_by = 'special-offer'
        AND s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
        AND s.customer_id IS NOT NULL
        AND s.created_at IS NOT NULL
        AND t.accepted_free_tx > 0
        AND c.customer_free_trial_subs <= 3
        AND s.customer_id NOT IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022')
    ),
    cohort_maturity AS (
      SELECT
        cohort,
        tier,
        DATE_ADD(cohort, INTERVAL 1 MONTH) <= CURRENT_DATE('Europe/London') AS mature_1st,
        DATE_ADD(cohort, INTERVAL 3 MONTH) <= CURRENT_DATE('Europe/London') AS mature_3rd
      FROM genuine_trials
      GROUP BY 1, 2
    ),
    mature_tier_rates AS (
      SELECT
        g.tier,
        g.tier_band,
        COUNT(*) AS mature_trials_1st,
        COUNTIF(g.converted_1st) AS mature_conv_1st,
        SAFE_DIVIDE(COUNTIF(g.converted_1st), COUNT(*)) AS tier_cvr_1st,
        COUNTIF(cm.mature_3rd) AS mature_trials_3rd,
        COUNTIF(cm.mature_3rd AND g.converted_3rd) AS mature_conv_3rd,
        SAFE_DIVIDE(COUNTIF(cm.mature_3rd AND g.converted_3rd), COUNTIF(cm.mature_3rd)) AS tier_cvr_3rd
      FROM genuine_trials g
      JOIN cohort_maturity cm USING (cohort, tier)
      WHERE cm.mature_1st
      GROUP BY 1, 2
    ),
    mature_overall_rate AS (
      SELECT
        SUM(mature_trials_1st) AS overall_mature_trials_1st,
        SUM(mature_conv_1st) AS overall_mature_conv_1st,
        SAFE_DIVIDE(SUM(mature_conv_1st), SUM(mature_trials_1st)) AS overall_cvr_1st,
        SUM(mature_trials_3rd) AS overall_mature_trials_3rd,
        SUM(mature_conv_3rd) AS overall_mature_conv_3rd,
        SAFE_DIVIDE(SUM(mature_conv_3rd), SUM(mature_trials_3rd)) AS overall_cvr_3rd
      FROM mature_tier_rates
    ),
    curve_m0_efv AS (
      SELECT
        sc0.tier AS curve_tier,
        SUM(IF(m BETWEEN 1 AND 3,  SAFE_DIVIDE(sc.survival_probability, sc0.survival_probability), 0)) AS mult_3m,
        SUM(IF(m BETWEEN 1 AND 6,  SAFE_DIVIDE(sc.survival_probability, sc0.survival_probability), 0)) AS mult_6m,
        SUM(IF(m BETWEEN 1 AND 12, SAFE_DIVIDE(sc.survival_probability, sc0.survival_probability), 0)) AS mult_12m,
        SUM(IF(m BETWEEN 1 AND 36, SAFE_DIVIDE(sc.survival_probability, sc0.survival_probability), 0)) AS mult_36m
      FROM survival_curve sc0
      JOIN UNNEST(GENERATE_ARRAY(1, 36)) AS m ON TRUE
      JOIN survival_curve sc ON sc.tier = sc0.tier AND sc.month = m
      WHERE sc0.month = 0
      GROUP BY 1
    ),
    active_trials AS (
      SELECT
        g.*,
        IF(mtr.mature_trials_1st >= 30, mtr.tier_cvr_1st, mor.overall_cvr_1st) AS eff_cvr_1st,
        mor.overall_cvr_1st,
        mtr.mature_trials_1st AS tier_mature_trials_1st,
        mtr.mature_conv_1st AS tier_mature_conv_1st,
        mtr.tier_cvr_1st,
        mtr.mature_trials_3rd AS tier_mature_trials_3rd,
        mtr.mature_conv_3rd AS tier_mature_conv_3rd,
        mtr.tier_cvr_3rd,
        cm0.mult_3m * CAST(g.monthly_value_gbp AS FLOAT64) AS full_efv_3m,
        cm0.mult_6m * CAST(g.monthly_value_gbp AS FLOAT64) AS full_efv_6m,
        cm0.mult_12m * CAST(g.monthly_value_gbp AS FLOAT64) AS full_efv_12m,
        cm0.mult_36m * CAST(g.monthly_value_gbp AS FLOAT64) AS full_efv_36m
      FROM genuine_trials g
      LEFT JOIN mature_tier_rates mtr ON g.tier = mtr.tier
      CROSS JOIN mature_overall_rate mor
      LEFT JOIN curve_m0_efv cm0 ON cm0.curve_tier = IF(g.tier_band = 'Very High', 'High', g.tier_band)
      WHERE g.trial_status = 'active'
    )
    SELECT
      'A. All next_purchase_date > today & 0 paid tx (May-Oct)' AS scope,
      tier,
      tier_band,
      COUNT(*) AS active_trials,
      COUNT(DISTINCT customer_id) AS active_customers,
      MIN(days_left) AS min_days_left,
      APPROX_QUANTILES(days_left, 2)[OFFSET(1)] AS median_days_left,
      ROUND(AVG(days_left), 2) AS mean_days_left,
      MAX(days_left) AS max_days_left,
      MAX(tier_mature_trials_1st) AS mature_trials_1st,
      MAX(tier_mature_conv_1st) AS mature_conv_1st,
      ROUND(MAX(effective_cvr_1st_val), 4) AS effective_cvr_1st,
      MAX(tier_mature_trials_3rd) AS mature_trials_3rd,
      MAX(tier_mature_conv_3rd) AS mature_conv_3rd,
      ROUND(MAX(tier_cvr_3rd), 4) AS trailing_cvr_3rd,
      ROUND(SUM(full_efv_3m * effective_cvr_1st_val), 2) AS total_trial_efv_3m,
      ROUND(SUM(full_efv_6m * effective_cvr_1st_val), 2) AS total_trial_efv_6m,
      ROUND(SUM(full_efv_12m * effective_cvr_1st_val), 2) AS total_trial_efv_12m,
      ROUND(SUM(full_efv_36m * effective_cvr_1st_val), 2) AS total_trial_efv_36m
    FROM (SELECT *, eff_cvr_1st AS effective_cvr_1st_val FROM active_trials)
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT
      'B. Current cohort (2026-10) active trials only' AS scope,
      tier,
      tier_band,
      COUNT(*) AS active_trials,
      COUNT(DISTINCT customer_id) AS active_customers,
      MIN(days_left) AS min_days_left,
      APPROX_QUANTILES(days_left, 2)[OFFSET(1)] AS median_days_left,
      ROUND(AVG(days_left), 2) AS mean_days_left,
      MAX(days_left) AS max_days_left,
      MAX(tier_mature_trials_1st) AS mature_trials_1st,
      MAX(tier_mature_conv_1st) AS mature_conv_1st,
      ROUND(MAX(effective_cvr_1st_val), 4) AS effective_cvr_1st,
      MAX(tier_mature_trials_3rd) AS mature_trials_3rd,
      MAX(tier_mature_conv_3rd) AS mature_conv_3rd,
      ROUND(MAX(tier_cvr_3rd), 4) AS trailing_cvr_3rd,
      ROUND(SUM(full_efv_3m * effective_cvr_1st_val), 2) AS total_trial_efv_3m,
      ROUND(SUM(full_efv_6m * effective_cvr_1st_val), 2) AS total_trial_efv_6m,
      ROUND(SUM(full_efv_12m * effective_cvr_1st_val), 2) AS total_trial_efv_12m,
      ROUND(SUM(full_efv_36m * effective_cvr_1st_val), 2) AS total_trial_efv_36m
    FROM (SELECT *, eff_cvr_1st AS effective_cvr_1st_val FROM active_trials WHERE cohort = '2026-10-01')
    GROUP BY 1, 2, 3
    ORDER BY 1
    """
    for r in run_q(client, "active trials and EFV", sql_efv):
        print(f"\nScope: {r['scope']}")
        print(f"  tier / tier_band             : {r['tier']} ({r['tier_band']})")
        print(f"  active_trials                : {r['active_trials']} ({r['active_customers']} distinct customers)")
        print(f"  days_left (min/med/mean/max) : {r['min_days_left']} / {r['median_days_left']} / {r['mean_days_left']} / {r['max_days_left']}")
        print(f"  mature 1st-pay trailing rate : {r['mature_conv_1st']} / {r['mature_trials_1st']} = {float(r['effective_cvr_1st'])*100:.2f}% ({r['effective_cvr_1st']})")
        print(f"  mature 3rd-pay trailing rate : {r['mature_conv_3rd']} / {r['mature_trials_3rd']} = {float(r['trailing_cvr_3rd'])*100:.2f}% ({r['trailing_cvr_3rd']})")
        print(f"  total_trial_efv_3m           : £{float(r['total_trial_efv_3m']):,.2f}")
        print(f"  total_trial_efv_6m           : £{float(r['total_trial_efv_6m']):,.2f}")
        print(f"  total_trial_efv_12m          : £{float(r['total_trial_efv_12m']):,.2f}")
        print(f"  total_trial_efv_36m          : £{float(r['total_trial_efv_36m']):,.2f}")

if __name__ == "__main__":
    main()

