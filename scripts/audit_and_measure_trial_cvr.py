import os
import re
import subprocess
import sys
from google.cloud import bigquery
from google.cloud import bigquery_datatransfer_v1
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
PROJECT_NUM = "613976204637"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_user_token():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token"],
        capture_output=True, text=True, check=True, env=env
    )
    return res.stdout.strip().splitlines()[-1].strip()

def get_bq_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, credentials=Credentials(token), location=LOCATION)

def run_q(client, sql):
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry = client.query(sql, job_config=dry_cfg)
    if dry.total_bytes_processed > 1024**3:
        raise RuntimeError(f"Query exceeds 1 GB limit: {dry.total_bytes_processed}")
    print(f"[Dry run: {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024**2):.2f} MB)]")
    cfg = bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})
    return list(client.query(sql, job_config=cfg).result())

def find_trial_10pct_lines(text):
    hits = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, 1):
        low = line.lower()
        if any(k in low for k in ["trial", "special-offer", "special_offer", "0.10", "0.1 ", "* 0.1", "10%"]):
            ctx = "\n".join(lines[max(0, idx - 4):min(len(lines), idx + 3)]).lower()
            if ("trial" in ctx or "special-offer" in ctx or "special_offer" in ctx) and (
                re.search(r"\b0\.10?\b", ctx) or "10%" in ctx or "ten percent" in ctx or "conversion" in ctx or "cvr" in ctx or "value_multiplier" in ctx or "trial_status" in ctx
            ):
                hits.append((idx, line.strip()))
    return hits

def main():
    print("================================================================================")
    print("1. AUDIT OF FIXED 10% TRIAL CONVERSION ACROSS REPO, SCHEDULED QUERIES, AND BQ")
    print("================================================================================")

    print("\n--- A. Repository Files ---")
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in (".git", "scratch", "__pycache__", "backups")]
        for fn in sorted(files):
            if fn.endswith((".sql", ".py", ".md")) and fn != "audit_and_measure_trial_cvr.py":
                path = os.path.join(root, fn)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        txt = f.read()
                    hits = find_trial_10pct_lines(txt)
                    if hits:
                        for ln, snippet in hits:
                            print(f"  REPO: {path}:{ln} -> {snippet}")
                except Exception as e:
                    print(f"  ERROR reading {path}: {e}")

    print("\n--- B. BigQuery Scheduled Queries (europe-west2 & us) ---")
    user_token = get_user_token()
    dts_client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=Credentials(user_token))
    for loc in ["europe-west2", "us"]:
        parent = f"projects/{PROJECT_NUM}/locations/{loc}"
        try:
            for cfg in dts_client.list_transfer_configs(parent=parent):
                q = cfg.params.get("query", "")
                if q:
                    hits = find_trial_10pct_lines(q)
                    if hits or "special-offer" in q.lower() or "trial_status" in q.lower() or "subscriber_ltv_output" in q.lower():
                        print(f"  SCHEDULED QUERY: {cfg.display_name} ({cfg.name}) [disabled={cfg.disabled}]")
                        for ln, snippet in hits:
                            print(f"    line {ln}: {snippet}")
        except Exception as e:
            print(f"  Error listing scheduled queries in {loc}: {e}")

    client = get_bq_client()
    print("\n--- C. BigQuery INFORMATION_SCHEMA (VIEWS & ROUTINES in rh_curated_Bright_Test & rh_warehouse) ---")
    for ds in ["rh_curated_Bright_Test", "rh_warehouse"]:
        sql_views = f"""
        SELECT table_name, view_definition
        FROM `{PROJECT}.{ds}.INFORMATION_SCHEMA.VIEWS`
        ORDER BY table_name
        """
        for r in run_q(client, sql_views):
            vdef = r["view_definition"] or ""
            hits = find_trial_10pct_lines(vdef)
            if hits or "special-offer" in vdef.lower() or "trial" in r["table_name"].lower():
                print(f"  VIEW: {ds}.{r['table_name']}")
                for ln, snippet in hits:
                    print(f"    line {ln}: {snippet}")

    print("\n================================================================================")
    print("2. REAL TRIAL-TO-PAID CONVERSION FROM SOURCES (fact_subscriptions, fact_transactions)")
    print("================================================================================")

    sql_overall = f"""
    WITH paid_sub_tx AS (
      SELECT
        subscription_key,
        MIN(purchase_date) AS first_paid_date
      FROM `{PROJECT}.rh_curated_Bright_Test.fact_transactions`
      WHERE subscription_key IS NOT NULL
        AND revenue_gbp > 0
        AND NOT IFNULL(is_free_entry, FALSE)
      GROUP BY subscription_key
    ),
    trials AS (
      SELECT
        s.subscription_key,
        s.created_date AS start_date,
        s.status,
        p.first_paid_date,
        p.subscription_key IS NOT NULL AS is_converted,
        IF(p.subscription_key IS NOT NULL, GREATEST(DATE_DIFF(p.first_paid_date, s.created_date, DAY), 0), NULL) AS days_to_convert
      FROM `{PROJECT}.rh_curated_Bright_Test.fact_subscriptions` s
      LEFT JOIN paid_sub_tx p USING (subscription_key)
      WHERE s.is_special_offer_trial
    ),
    med AS (
      SELECT
        PERCENTILE_CONT(CAST(days_to_convert AS FLOAT64), 0.5) OVER () AS overall_med_days
      FROM trials
      WHERE is_converted
      LIMIT 1
    )
    SELECT
      COUNT(*) AS total_trials,
      COUNTIF(is_converted) AS total_converted,
      COUNTIF(NOT is_converted AND status = 'ACTIVE') AS total_active,
      COUNTIF(NOT is_converted AND status != 'ACTIVE') AS total_lapsed,
      ROUND(SAFE_DIVIDE(COUNTIF(is_converted), COUNT(*)) * 100, 2) AS overall_cvr_pct,
      ROUND(AVG(days_to_convert), 2) AS overall_mean_days,
      ROUND(ANY_VALUE(m.overall_med_days), 2) AS overall_median_days
    FROM trials
    CROSS JOIN med m
    """
    ov = run_q(client, sql_overall)[0]
    print(
        f"OVERALL ALL-TIME: trials={ov['total_trials']}, converted={ov['total_converted']}, "
        f"active={ov['total_active']}, lapsed={ov['total_lapsed']}, "
        f"cvr={ov['overall_cvr_pct']:.2f}%, mean_days={ov['overall_mean_days']:.2f}, median_days={ov['overall_median_days']:.2f}"
    )

    print("\n================================================================================")
    print("3. TABLE OUTPUT: rh_curated_Bright_Test.rpt_trial_conversion")
    print("================================================================================")
    sql_rpt = f"""
    SELECT
      cohort,
      tier,
      tier_band,
      trials,
      converted,
      active_trials,
      lapsed_trials,
      conversion_rate,
      median_days,
      mean_days,
      trailing_90d_trials,
      trailing_90d_converted,
      trailing_90d_tier_cvr,
      trailing_90d_overall_cvr,
      trailing_90d_conversion_rate
    FROM `{PROJECT}.rh_curated_Bright_Test.rpt_trial_conversion`
    ORDER BY cohort, tier
    """
    rpt_rows = run_q(client, sql_rpt)
    print(f"{'cohort':<12} {'tier':<8} {'band':<6} {'trials':>7} {'conv':>6} {'act':>5} {'lps':>6} {'cvr':>8} {'med_d':>7} {'mean_d':>7} {'90d_n':>6} {'90d_c':>6} {'90d_tier':>9} {'90d_all':>8} {'90d_eff':>8}")
    print("-" * 126)
    for r in rpt_rows:
        print(
            f"{str(r['cohort']):<12} {r['tier']:<8} {r['tier_band']:<6} "
            f"{r['trials']:>7} {r['converted']:>6} {r['active_trials']:>5} {r['lapsed_trials']:>6} "
            f"{float(r['conversion_rate']):>8.4f} "
            f"{str(r['median_days'] if r['median_days'] is not None else 'None'):>7} "
            f"{str(r['mean_days'] if r['mean_days'] is not None else 'None'):>7} "
            f"{r['trailing_90d_trials']:>6} {r['trailing_90d_converted']:>6} "
            f"{float(r['trailing_90d_tier_cvr']):>9.4f} {float(r['trailing_90d_overall_cvr']):>8.4f} "
            f"{float(r['trailing_90d_conversion_rate']):>8.4f}"
        )

    print("\n================================================================================")
    print("4. VIEW SUMMARY: rh_curated_Bright_Test.vw_trial_status")
    print("================================================================================")
    sql_vw = f"""
    SELECT
      status,
      tier,
      tier_band,
      COUNT(*) AS trial_rows,
      MIN(start_date) AS min_start_date,
      MAX(start_date) AS max_start_date,
      ROUND(AVG(days_in), 2) AS avg_days_in,
      ROUND(AVG(days_to_convert), 2) AS avg_days_to_convert
    FROM `{PROJECT}.rh_curated_Bright_Test.vw_trial_status`
    GROUP BY 1, 2, 3
    ORDER BY 1, 2
    """
    for r in run_q(client, sql_vw):
        print(
            f"status={r['status']:<10} | tier={r['tier']:<8} ({r['tier_band']:<4}) | "
            f"rows={r['trial_rows']:>5} | start={r['min_start_date']}..{r['max_start_date']} | "
            f"avg_days_in={r['avg_days_in']} | avg_days_to_convert={r['avg_days_to_convert']}"
        )

    print("\n================================================================================")
    print("5. DYNAMIC CVR IN rh_curated_Bright_Test.customer_ltv_horizons (ACTIVE UNPAID TRIALS)")
    print("================================================================================")
    sql_ltv = f"""
    SELECT
      v.tier,
      v.tier_band,
      COUNT(*) AS active_unpaid_trials,
      COUNT(DISTINCT v.customer_key) AS distinct_customers,
      ROUND(AVG(h.sub_efv_3m), 2) AS avg_sub_efv_3m,
      ROUND(AVG(h.sub_efv_12m), 2) AS avg_sub_efv_12m,
      ROUND(AVG(h.ltv_3m), 2) AS avg_ltv_3m,
      ROUND(AVG(h.ltv_12m), 2) AS avg_ltv_12m
    FROM `{PROJECT}.rh_curated_Bright_Test.vw_trial_status` v
    LEFT JOIN `{PROJECT}.rh_curated_Bright_Test.customer_ltv_horizons` h
      ON v.customer_key = h.customer_key
    WHERE v.status = 'active'
    GROUP BY 1, 2
    """
    for r in run_q(client, sql_ltv):
        print(
            f"tier={r['tier']} ({r['tier_band']}) | active_unpaid_trials={r['active_unpaid_trials']} | "
            f"customers={r['distinct_customers']} | avg_sub_efv_3m=£{r['avg_sub_efv_3m']:.2f} | "
            f"avg_sub_efv_12m=£{r['avg_sub_efv_12m']:.2f} | avg_ltv_3m=£{r['avg_ltv_3m']:.2f} | avg_ltv_12m=£{r['avg_ltv_12m']:.2f}"
        )

if __name__ == "__main__":
    main()
