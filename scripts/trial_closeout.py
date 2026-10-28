import json
import os
import subprocess
import sys
from pathlib import Path
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
ROOT = Path(__file__).resolve().parent.parent
STEP4_RUNS_JSON = ROOT / "backups" / "scheduled_queries" / "2026-10-28" / "step4_job_runs.json"

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
    print("1. WATERFALL BY MONTH (DECISIONS 1 & 2: 1ST TRIAL/CUST BY first_free_at, < 10 TRIALS)")
    print("================================================================================")
    sql_wf = f"""
    WITH sub_tx AS (
      SELECT
        subscription_id,
        COUNT(*) AS total_tx,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        MIN(IF(payment_status = 'ACCEPTED' AND total_cost = 0, COALESCE(purchase_date, created_at), NULL)) AS first_free_at,
        STRING_AGG(DISTINCT payment_status, ',' ORDER BY payment_status) AS all_statuses
      FROM `{PROJECT}.rh_warehouse.fact_transactions`
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    ranked AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        s.status AS woo_status,
        s.created_at,
        t.first_free_at,
        COALESCE(t.total_tx, 0) AS total_tx,
        COALESCE(t.accepted_free_tx, 0) AS accepted_free_tx,
        t.all_statuses,
        COUNTIF(
          s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
          AND s.customer_id IS NOT NULL
          AND s.created_at IS NOT NULL
          AND COALESCE(t.accepted_free_tx, 0) > 0
        ) OVER (PARTITION BY s.customer_id) AS cust_free_trial_subs,
        ROW_NUMBER() OVER (
          PARTITION BY s.customer_id,
            (s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND s.customer_id IS NOT NULL AND s.created_at IS NOT NULL AND COALESCE(t.accepted_free_tx, 0) > 0)
          ORDER BY t.first_free_at, s.created_at, s.subscription_id
        ) AS rn_valid
      FROM `{PROJECT}.rh_warehouse.fact_subscriptions` s
      LEFT JOIN sub_tx t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
    ),
    classified AS (
      SELECT
        FORMAT_DATE('%Y-%m', DATE(COALESCE(first_free_at, created_at))) AS month_key,
        CASE
          WHEN woo_status IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') OR customer_id IS NULL OR created_at IS NULL
            THEN '1_abandoned'
          WHEN total_tx = 0
            THEN '2_empty_cancelled'
          WHEN accepted_free_tx = 0 AND all_statuses = 'FREE'
            THEN '3a_free_only'
          WHEN accepted_free_tx = 0
            THEN '3b_other_no_0_accepted'
          WHEN cust_free_trial_subs >= 10
            OR customer_id IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022', '64b5d23f79c5fe003eefd800')
            THEN '4_exploit'
          WHEN rn_valid > 1
            THEN '5_repeat_trial'
          ELSE '6_genuine'
        END AS bucket
      FROM ranked
    )
    SELECT
      month_key,
      COUNT(*) AS all_special_offer,
      COUNTIF(bucket = '1_abandoned') AS abandoned,
      COUNTIF(bucket = '2_empty_cancelled') AS empty_cancelled,
      COUNTIF(bucket = '3a_free_only') AS free_only,
      COUNTIF(bucket = '3b_other_no_0_accepted') AS no_0_accepted,
      COUNTIF(bucket = '4_exploit') AS exploit,
      COUNTIF(bucket = '5_repeat_trial') AS repeat_trials,
      COUNTIF(bucket = '6_genuine') AS genuine_trials,
      COUNT(*) - (
        COUNTIF(bucket = '1_abandoned') +
        COUNTIF(bucket = '2_empty_cancelled') +
        COUNTIF(bucket = '3a_free_only') +
        COUNTIF(bucket = '3b_other_no_0_accepted') +
        COUNTIF(bucket = '4_exploit') +
        COUNTIF(bucket = '5_repeat_trial') +
        COUNTIF(bucket = '6_genuine')
      ) AS waterfall_diff
    FROM classified
    GROUP BY 1
    ORDER BY 1
    """
    wf_rows = run_q(client, "waterfall by month", sql_wf)
    print(f"{'Month':<10} {'All SO':>7} {'Aband':>7} {'Empty':>7} {'FREEOnly':>9} {'No£0Acc':>8} {'Exploit':>8} {'Repeat':>7} {'Genuine':>8} {'Diff':>5}")
    print("-" * 86)
    tot = [0] * 9
    by_m = {}
    for r in wf_rows:
        by_m[r["month_key"]] = r
        vals = [
            r["all_special_offer"],
            r["abandoned"],
            r["empty_cancelled"],
            r["free_only"],
            r["no_0_accepted"],
            r["exploit"],
            r["repeat_trials"],
            r["genuine_trials"],
            r["waterfall_diff"],
        ]
        for i in range(9):
            tot[i] += vals[i]
        print(
            f"{r['month_key']:<10} {vals[0]:>7} {vals[1]:>7} {vals[2]:>7} "
            f"{vals[3]:>9} {vals[4]:>8} {vals[5]:>8} {vals[6]:>7} {vals[7]:>8} {vals[8]:>5}"
        )
    print("-" * 86)
    print(
        f"{'TOTAL':<10} {tot[0]:>7} {tot[1]:>7} {tot[2]:>7} "
        f"{tot[3]:>9} {tot[4]:>8} {tot[5]:>8} {tot[6]:>7} {tot[7]:>8} {tot[8]:>5}"
    )

    print("\n--- Revised Reconciliation Gate (May-Aug within 2% of 417 / 81 / 189 / 163) ---")
    targets = {"2026-05": 417, "2026-06": 81, "2026-07": 189, "2026-08": 163}
    gate_pass = True
    for m, tgt in targets.items():
        ours = by_m[m]["genuine_trials"]
        diff = ours - tgt
        pct = (diff / tgt) * 100.0
        ok = abs(pct) <= 2.0
        if not ok:
            gate_pass = False
        print(f"  {m}: ours={ours:4d} vs target={tgt:4d} | diff={diff:+4d} ({pct:+6.2f}%) -> {'PASS (<= 2%)' if ok else 'FAIL (> 2%)'}")
    print(f"Overall May-Aug 2% reconciliation gate: {'PASS' if gate_pass else 'FAIL'}")

    print("\n================================================================================")
    print("2. COHORT TABLE FROM rpt_trial_conversion (1ST & 3RD PAYMENT RATES & MATURITY)")
    print("================================================================================")
    sql_coh = f"""
    SELECT
      cohort,
      tier,
      tier_band,
      trials,
      active_trials,
      lapsed_trials,
      converted_1st,
      conversion_rate_1st,
      mean_days_1st,
      median_days_1st,
      mature_1st,
      converted_3rd,
      conversion_rate_3rd,
      mean_days_3rd,
      median_days_3rd,
      mature_3rd,
      trailing_mature_trials_1st,
      trailing_mature_converted_1st,
      trailing_tier_cvr_1st,
      trailing_overall_cvr_1st,
      trailing_conversion_rate_1st,
      trailing_mature_trials_3rd,
      trailing_mature_converted_3rd,
      trailing_tier_cvr_3rd,
      trailing_overall_cvr_3rd,
      trailing_conversion_rate_3rd
    FROM `{PROJECT}.rh_curated_Bright_Test.rpt_trial_conversion`
    ORDER BY cohort, tier
    """
    coh_rows = run_q(client, "rpt_trial_conversion", sql_coh)
    print(f"{'Cohort':<11} {'Tier':<7} {'Trials':>6} {'Act':>4} {'Lps':>4} {'Conv1':>6} {'CVR1':>8} {'Mean1':>6} {'Med1':>5} {'Mat1':>5} {'Conv3':>6} {'CVR3':>8} {'Mean3':>6} {'Med3':>5} {'Mat3':>5}")
    print("-" * 116)
    for r in coh_rows:
        print(
            f"{str(r['cohort']):<11} {r['tier']:<7} {r['trials']:>6} {r['active_trials']:>4} {r['lapsed_trials']:>4} "
            f"{r['converted_1st']:>6} {float(r['conversion_rate_1st']):>8.4f} {str(r['mean_days_1st']):>6} {str(r['median_days_1st']):>5} {str(r['mature_1st']):>5} "
            f"{r['converted_3rd']:>6} {float(r['conversion_rate_3rd']):>8.4f} {str(r['mean_days_3rd']):>6} {str(r['median_days_3rd']):>5} {str(r['mature_3rd']):>5}"
        )

    print("\n================================================================================")
    print("3. TRAILING RATES IN USE (PER TIER AND OVERALL)")
    print("================================================================================")
    if coh_rows:
        r0 = coh_rows[0]
        print(f"  Tier                         : {r0['tier']} ({r0['tier_band']})")
        print(f"  1st-payment mature trials    : {r0['trailing_mature_converted_1st']} / {r0['trailing_mature_trials_1st']}")
        print(f"  1st-payment tier rate        : {float(r0['trailing_tier_cvr_1st']):.4f} ({float(r0['trailing_tier_cvr_1st'])*100:.2f}%)")
        print(f"  1st-payment overall rate     : {float(r0['trailing_overall_cvr_1st']):.4f} ({float(r0['trailing_overall_cvr_1st'])*100:.2f}%)")
        print(f"  1st-payment effective (LTV)  : {float(r0['trailing_conversion_rate_1st']):.4f} ({float(r0['trailing_conversion_rate_1st'])*100:.2f}%)")
        print(f"  3rd-payment mature trials    : {r0['trailing_mature_converted_3rd']} / {r0['trailing_mature_trials_3rd']}")
        print(f"  3rd-payment tier rate        : {float(r0['trailing_tier_cvr_3rd']):.4f} ({float(r0['trailing_tier_cvr_3rd'])*100:.2f}%)")
        print(f"  3rd-payment overall rate     : {float(r0['trailing_overall_cvr_3rd']):.4f} ({float(r0['trailing_overall_cvr_3rd'])*100:.2f}%)")
        print(f"  3rd-payment effective (KPI)  : {float(r0['trailing_conversion_rate_3rd']):.4f} ({float(r0['trailing_conversion_rate_3rd'])*100:.2f}%)")

    print("\n================================================================================")
    print("4. TODAY'S ACTIVE TRIALS (vw_trial_status) & DAYS-LEFT DISTRIBUTION")
    print("================================================================================")
    sql_act = f"""
    WITH act AS (
      SELECT
        v.*,
        PERCENTILE_CONT(CAST(v.days_left AS FLOAT64), 0.5) OVER () AS med_days_left
      FROM `{PROJECT}.rh_curated_Bright_Test.vw_trial_status` v
      WHERE v.status = 'active'
    )
    SELECT
      tier,
      tier_band,
      COUNT(*) AS active_trials,
      COUNT(DISTINCT customer_key) AS distinct_customers,
      MIN(days_left) AS min_days_left,
      ROUND(ANY_VALUE(med_days_left), 2) AS median_days_left,
      ROUND(AVG(days_left), 2) AS mean_days_left,
      MAX(days_left) AS max_days_left,
      COUNTIF(days_left = 4) AS days_left_eq_4,
      COUNTIF(days_left > 4) AS days_left_gt_4
    FROM act
    GROUP BY 1, 2
    """
    for r in run_q(client, "active trials in vw_trial_status", sql_act):
        print(f"  tier / tier_band             : {r['tier']} ({r['tier_band']})")
        print(f"  active_trials                : {r['active_trials']} ({r['distinct_customers']} distinct customers)")
        print(f"  days_left min / med / max    : {r['min_days_left']} / {r['median_days_left']} / {r['max_days_left']} (mean={r['mean_days_left']})")
        print(f"  days_left breakdown          : {r['days_left_eq_4']} at 4 days left (2026-11-01), {r['days_left_gt_4']} at 34 days left (2026-12-01)")

    print("\n================================================================================")
    print("5. TOTAL TRIAL EFV, ACTIVE-SUBSCRIBER ltv_12m, AND SUM(total_spend_gbp) CONTROLS")
    print("================================================================================")
    sql_trial_efv = f"""
    SELECT
      COUNT(*) AS active_trial_customers,
      ROUND(SUM(c.sub_efv_3m), 2) AS total_trial_efv_3m,
      ROUND(SUM(c.efv_6m - COALESCE((SELECT o.efv_6m FROM `{PROJECT}.rh_curated_Bright_Test.customer_ltv_horizons` o WHERE o.customer_key = c.customer_key), 0)), 2) AS note_placeholder,
      ROUND(SUM(c.sub_efv_12m), 2) AS total_trial_efv_12m,
      ROUND(SUM(c.ltv_12m), 2) AS active_trial_customers_ltv_12m
    FROM `{PROJECT}.rh_curated_Bright_Test.customer_ltv_horizons` c
    JOIN `{PROJECT}.rh_curated_Bright_Test.vw_trial_status` v
      ON c.customer_key = v.customer_key
    WHERE v.status = 'active'
      AND c.realised_value = 0
    """
    sql_trial_efv_exact = f"""
    SELECT
      COUNT(DISTINCT v.subscription_key) AS active_trials_count,
      COUNTIF(c.realised_value = 0) AS trial_only_customers,
      COUNTIF(c.realised_value > 0) AS trial_plus_prior_spend_customers,
      ROUND(SUM(IF(c.realised_value = 0, c.sub_efv_3m, 0)), 2) AS trial_only_sub_efv_3m,
      ROUND(SUM(IF(c.realised_value = 0, c.sub_efv_12m, 0)), 2) AS trial_only_sub_efv_12m,
      ROUND(SUM(c.sub_efv_3m), 2) AS active_trial_cust_sub_efv_3m,
      ROUND(SUM(c.sub_efv_12m), 2) AS active_trial_cust_sub_efv_12m
    FROM `{PROJECT}.rh_curated_Bright_Test.vw_trial_status` v
    JOIN `{PROJECT}.rh_curated_Bright_Test.customer_ltv_horizons` c
      ON v.customer_key = c.customer_key
    WHERE v.status = 'active'
    """
    tefv = run_q(client, "active trial EFV in customer_ltv_horizons", sql_trial_efv_exact)[0]
    print(f"  Active trials in customer_ltv_horizons : {tefv['active_trials_count']} ({tefv['trial_only_customers']} trial-only, {tefv['trial_plus_prior_spend_customers']} with prior spend/sub)")
    print(f"  Trial-only customers sub_efv_3m        : £{float(tefv['trial_only_sub_efv_3m']):,.2f}")
    print(f"  Trial-only customers sub_efv_12m       : £{float(tefv['trial_only_sub_efv_12m']):,.2f}")
    print(f"  All 122 active-trial custs sub_efv_3m  : £{float(tefv['active_trial_cust_sub_efv_3m']):,.2f}")
    print(f"  All 122 active-trial custs sub_efv_12m : £{float(tefv['active_trial_cust_sub_efv_12m']):,.2f}")

    if STEP4_RUNS_JSON.exists():
        runs_data = json.loads(STEP4_RUNS_JSON.read_text(encoding="utf-8"))
        b = runs_data["before_ltv"]
        a = runs_data["after_ltv"]
        ltv_diff = a["active_sub_sum_ltv_12m"] - b["active_sub_sum_ltv_12m"]
        ltv_pct = (ltv_diff / b["active_sub_sum_ltv_12m"]) * 100.0
        spend_diff = a["sum_total_spend_gbp"] - b["sum_total_spend_gbp"]

        print("\n--- Control 1: Active-Subscriber ltv_12m Before vs After (must move < 1%) ---")
        print(f"  BEFORE (pre-Step-4 baseline) : {b['active_subscribers']:,} active subscribers | SUM(ltv_12m) = £{b['active_sub_sum_ltv_12m']:,.2f} | AVG(ltv_12m) = £{b['active_sub_avg_ltv_12m']:,.2f} | SUM(sub_efv_12m) = £{b['active_sub_sum_efv_12m']:,.2f}")
        print(f"  AFTER  (Step 4 wired in)     : {a['active_subscribers']:,} active subscribers | SUM(ltv_12m) = £{a['active_sub_sum_ltv_12m']:,.2f} | AVG(ltv_12m) = £{a['active_sub_avg_ltv_12m']:,.2f} | SUM(sub_efv_12m) = £{a['active_sub_sum_efv_12m']:,.2f}")
        print(f"  DIFF (AFTER - BEFORE)        : {a['active_subscribers'] - b['active_subscribers']:+d} active subscribers | SUM(ltv_12m) diff = £{ltv_diff:+,.2f} ({ltv_pct:+.4f}%) -> {'PASS (< 1%)' if abs(ltv_pct) < 1.0 else 'FAIL (>= 1%)'}")

        print("\n--- Control 2: SUM(total_spend_gbp) Before vs After (must be unchanged) ---")
        print(f"  BEFORE SUM(total_spend_gbp)  : £{b['sum_total_spend_gbp']:,.2f} (SUM(realised_value) = £{b['sum_realised_value']:,.2f})")
        print(f"  AFTER  SUM(total_spend_gbp)  : £{a['sum_total_spend_gbp']:,.2f} (SUM(realised_value) = £{a['sum_realised_value']:,.2f})")
        print(f"  DIFF                         : £{spend_diff:+,.2f} -> {'PASS (unchanged)' if abs(spend_diff) < 0.005 else 'FAIL'}")

    print("\n================================================================================")
    print("6. RESULTS OF ALL FOUR TRIAL TESTS IN dq_gate_results")
    print("================================================================================")
    sql_tests = f"""
    SELECT
      run_ts,
      run_date,
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `{PROJECT}.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = (SELECT MAX(run_ts) FROM `{PROJECT}.rh_curated_Bright_Test.dq_gate_results`)
      AND test IN (
        'trial_conversion_no_duplicates',
        'trial_waterfall_exact_sum',
        'trial_no_exploit_in_active',
        'trial_conversion_1st_pay_wow_shift'
      )
    ORDER BY severity DESC, test
    """
    for r in run_q(client, "4 trial tests in dq_gate_results", sql_tests):
        print(
            f"  [{r['severity']}] {r['test']:<36} : {r['result']} "
            f"(source={r['source_value']}, curated={r['curated_value']}) | {r['detail']}"
        )

    sql_all_hard = f"""
    SELECT
      COUNTIF(severity = 'HARD' AND result = 'PASS') AS hard_pass,
      COUNTIF(severity = 'HARD' AND result != 'PASS') AS hard_fail,
      COUNTIF(severity = 'WARN' AND result = 'PASS') AS warn_pass,
      COUNTIF(severity = 'WARN' AND result = 'WARN') AS warn_triggered
    FROM `{PROJECT}.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = (SELECT MAX(run_ts) FROM `{PROJECT}.rh_curated_Bright_Test.dq_gate_results`)
    """
    s_hard = run_q(client, "all dq_gate_results summary", sql_all_hard)[0]
    print(f"\n  Full Nightly DQ Gate Summary: HARD PASS={s_hard['hard_pass']}, HARD FAIL={s_hard['hard_fail']}, WARN PASS={s_hard['warn_pass']}, WARN={s_hard['warn_triggered']}")

    print("\n================================================================================")
    print("7. MANUAL JOB RUN STATES, BYTES BILLED, AND BACKUP PATHS")
    print("================================================================================")
    if STEP4_RUNS_JSON.exists():
        runs_data = json.loads(STEP4_RUNS_JSON.read_text(encoding="utf-8"))
        nj = runs_data["nightly_job"]
        lj = runs_data["ltv_job"]
        print("  Nightly Build Job:")
        print(f"    Display Name : {nj['display_name']}")
        print(f"    Run Resource : {nj['run_name']}")
        print(f"    State        : {nj['state']} ({nj['elapsed_seconds']}s)")
        print(f"    Backup SQL   : {nj['backup_sql']}")
        print(f"    Backup JSON  : {nj['backup_json']}")
        print("  Customer LTV Horizons Job:")
        print(f"    Display Name : {lj['display_name']}")
        print(f"    Run Resource : {lj['run_name']}")
        print(f"    State        : {lj['state']} ({lj['elapsed_seconds']}s)")
        print(f"    Dry-run Bytes: {lj['dry_run_bytes']:,} bytes ({lj['dry_run_bytes']/(1024**2):.2f} MB)")
        print(f"    Backup SQL   : {lj['backup_sql']}")
        print(f"    Backup JSON  : {lj['backup_json']}")

    sql_jobs = f"""
    SELECT
      job_id,
      creation_time,
      state,
      statement_type,
      total_bytes_processed,
      total_bytes_billed
    FROM `{PROJECT}.region-europe-west2.INFORMATION_SCHEMA.JOBS_BY_PROJECT`
    WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 60 MINUTE)
      AND user_email = '{SA}'
      AND job_id LIKE 'scheduled_query_%'
    ORDER BY creation_time DESC
    LIMIT 6
    """
    print("\n  Recent BigQuery Scheduled Job Executions (INFORMATION_SCHEMA.JOBS_BY_PROJECT):")
    for r in run_q(client, "recent jobs bytes", sql_jobs):
        bp = r["total_bytes_processed"] or 0
        bb = r["total_bytes_billed"] or 0
        print(
            f"    job_id={r['job_id']} | time={r['creation_time']} | type={r['statement_type']:<22} | "
            f"state={r['state']} | processed={bp:,} B ({bp/(1024**2):.2f} MB) | billed={bb:,} B ({bb/(1024**2):.2f} MB)"
        )

if __name__ == "__main__":
    main()

