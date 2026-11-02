"""
scripts/prompt_t_full_analysis.py

Comprehensive analysis for AGENT PROMPT T:
First-Time Customer (FTC) Revenue with No Campaign Credit, Before and After 19 Oct.

Covers:
- Base population & £0 orders check
- T1: Classification definitions (URL-regex vs dim_channel_last_touch)
- T2: Reproduction of the 7%/43% and 0%/22% claims for 31 Aug and 31 Oct
- T3: Weekly trend from 24 Aug to latest complete week, baseline (31 Aug - 18 Oct) vs post (19 Oct onwards)
- T4: Breakdown of bucket b by landing_host and landing_path (baseline vs post)
- T5: Join comparison with old table rh_warehouse.fact_transaction_attribution
- T6: Reconciliation against rpt_paid_media_entity_daily
- T7: Scaled campaign estimate for w/c 19 Oct and 26 Oct
- Cost and bytes accounting via INFORMATION_SCHEMA.JOBS_BY_PROJECT
"""

import os, sys, subprocess, json, datetime
from decimal import Decimal
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

PROJECT_ID = "rh-ga4-491819"
LOCATION = "europe-west2"
SERVICE_ACCOUNT = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

# Tracking queries and job IDs for cost reporting
EXECUTED_JOBS = []

def get_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SERVICE_ACCOUNT}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    creds = Credentials(token)
    return bigquery.Client(project=PROJECT_ID, credentials=creds, location=LOCATION)

def execute_query(client, sql, description):
    print(f"\n=======================================================")
    print(f"QUERY: {description}")
    print("=======================================================")
    job_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry = client.query(sql, job_config=job_cfg, location=LOCATION)
    bytes_scanned = dry.total_bytes_processed or 0
    mb_scanned = bytes_scanned / (1024 * 1024)
    gb_scanned = bytes_scanned / (1024 * 1024 * 1024)
    print(f"Dry-run scan: {bytes_scanned:,} bytes ({mb_scanned:.2f} MB / {gb_scanned:.4f} GB)")
    if gb_scanned > 15.0:
        raise ValueError(f"Aborting: scan of {gb_scanned:.2f} GB exceeds 15 GB limit!")

    exec_cfg = bigquery.QueryJobConfig(use_query_cache=False)
    job = client.query(sql, job_config=exec_cfg, location=LOCATION)
    rows = list(job.result())
    EXECUTED_JOBS.append({
        "job_id": job.job_id,
        "description": description,
        "bytes_scanned": bytes_scanned,
        "query": sql.strip()
    })
    print(f"Executed job ID: {job.job_id} | Rows returned: {len(rows)}")
    return rows

def main():
    client = get_client()

    # -------------------------------------------------------------
    # 0. Check £0 orders and base population validation
    # -------------------------------------------------------------
    sql_base_check = """
    SELECT
      COUNT(*) AS total_ftc_all,
      COUNTIF(revenue_gbp = 0) AS zero_rev_orders,
      COUNTIF(revenue_gbp = 0 AND is_free_entry) AS zero_rev_free_entry,
      COUNTIF(revenue_gbp = 0 AND NOT IFNULL(is_free_entry, FALSE)) AS zero_rev_non_free,
      COUNTIF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE)) AS qualifying_base_orders,
      ROUND(SUM(IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), revenue_gbp, 0)), 2) AS qualifying_base_revenue
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
    WHERE is_first_purchase = TRUE;
    """
    rows_base = execute_query(client, sql_base_check, "Base population and £0 FTC check")
    for r in rows_base:
        print(dict(r))

    # -------------------------------------------------------------
    # T1 & T2: 31 Aug vs 31 Oct reproduction under both definitions
    # -------------------------------------------------------------
    sql_t2 = """
    WITH base AS (
      SELECT
        t.transaction_id,
        DATE(t.purchase_at, 'Europe/London') AS order_date,
        t.revenue_gbp,
        touch.last_touch_url,
        dim.channel_group,
        dim.is_attributed,
        -- URL Regex definition:
        -- a: no touch row or last_touch_url null/empty
        -- b: landing link present but no campaign tag (no utm_*, gclid, gbraid, wbraid, fbclid, clickref)
        -- c: tagged
        CASE
          WHEN touch.last_touch_url IS NULL OR TRIM(touch.last_touch_url) = '' THEN 'a_no_landing_link'
          WHEN NOT REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](utm_[a-z0-9_]+|gclid|gbraid|wbraid|fbclid|clickref)=') THEN 'b_no_campaign_tag'
          ELSE 'c_tagged'
        END AS url_bucket,
        -- Dimension table definition:
        CASE
          WHEN dim.channel_group = 'Unattributed' OR NOT dim.is_attributed THEN 'dim_a_unattributed'
          WHEN dim.channel_group = 'Direct / untagged' THEN 'dim_b_direct_untagged'
          ELSE 'dim_c_tagged'
        END AS dim_bucket
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch` dim
        ON t.channel_last_touch_key = dim.channel_key
      WHERE t.is_first_purchase = TRUE
        AND t.revenue_gbp > 0
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND DATE(t.purchase_at, 'Europe/London') IN (DATE '2026-08-31', DATE '2026-10-31')
    ),
    day_totals AS (
      SELECT
        order_date,
        COUNT(*) AS total_orders,
        SUM(revenue_gbp) AS total_revenue
      FROM base
      GROUP BY 1
    ),
    url_rollup AS (
      SELECT
        b.order_date,
        'url_regex' AS definition,
        b.url_bucket AS bucket,
        COUNT(*) AS orders,
        SUM(b.revenue_gbp) AS revenue,
        ROUND(SAFE_DIVIDE(COUNT(*), dt.total_orders) * 100, 2) AS pct_orders,
        ROUND(SAFE_DIVIDE(SUM(b.revenue_gbp), dt.total_revenue) * 100, 2) AS pct_revenue
      FROM base b
      JOIN day_totals dt ON b.order_date = dt.order_date
      GROUP BY 1, 2, 3, dt.total_orders, dt.total_revenue
    ),
    dim_rollup AS (
      SELECT
        b.order_date,
        'dim_table' AS definition,
        b.dim_bucket AS bucket,
        COUNT(*) AS orders,
        SUM(b.revenue_gbp) AS revenue,
        ROUND(SAFE_DIVIDE(COUNT(*), dt.total_orders) * 100, 2) AS pct_orders,
        ROUND(SAFE_DIVIDE(SUM(b.revenue_gbp), dt.total_revenue) * 100, 2) AS pct_revenue
      FROM base b
      JOIN day_totals dt ON b.order_date = dt.order_date
      GROUP BY 1, 2, 3, dt.total_orders, dt.total_revenue
    )
    SELECT * FROM url_rollup
    UNION ALL
    SELECT * FROM dim_rollup
    ORDER BY order_date, definition, bucket;
    """
    rows_t2 = execute_query(client, sql_t2, "T2: 31 Aug vs 31 Oct reproduction")
    print(f"{'Date':<12} | {'Definition':<10} | {'Bucket':<22} | {'Orders':<8} | {'Revenue (£)':<12} | {'% Orders':<10} | {'% Revenue':<10}")
    print("-" * 95)
    for r in rows_t2:
        print(f"{str(r['order_date']):<12} | {r['definition']:<10} | {r['bucket']:<22} | {r['orders']:<8} | {r['revenue']:<12.2f} | {r['pct_orders']:<10.2f} | {r['pct_revenue']:<10.2f}")

    # -------------------------------------------------------------
    # T3: Weekly Trend from 24 Aug to latest complete week
    # -------------------------------------------------------------
    sql_t3 = """
    WITH date_coverage AS (
      SELECT
        MIN(IF(last_touch_url IS NOT NULL AND TRIM(last_touch_url) != '', purchase_date, NULL)) AS first_url_date
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch`
    ),
    base AS (
      SELECT
        t.transaction_id,
        DATE(t.purchase_at, 'Europe/London') AS order_date,
        DATE_TRUNC(DATE(t.purchase_at, 'Europe/London'), WEEK(MONDAY)) AS week_commencing,
        t.revenue_gbp,
        touch.last_touch_url,
        CASE
          WHEN touch.last_touch_url IS NULL OR TRIM(touch.last_touch_url) = '' THEN 'a'
          WHEN NOT REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](utm_[a-z0-9_]+|gclid|gbraid|wbraid|fbclid|clickref)=') THEN 'b'
          ELSE 'c'
        END AS bucket
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      WHERE t.is_first_purchase = TRUE
        AND t.revenue_gbp > 0
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND DATE(t.purchase_at, 'Europe/London') >= DATE '2026-08-24'
        AND DATE(t.purchase_at, 'Europe/London') <= DATE '2026-11-01'
    )
    SELECT
      week_commencing,
      COUNT(*) AS total_orders,
      ROUND(SUM(revenue_gbp), 2) AS total_revenue,
      -- Orders by bucket
      COUNTIF(bucket = 'a') AS orders_a,
      COUNTIF(bucket = 'b') AS orders_b,
      COUNTIF(bucket = 'c') AS orders_c,
      COUNTIF(bucket IN ('a', 'b')) AS orders_ab,
      -- Revenue by bucket
      ROUND(SUM(IF(bucket = 'a', revenue_gbp, 0)), 2) AS rev_a,
      ROUND(SUM(IF(bucket = 'b', revenue_gbp, 0)), 2) AS rev_b,
      ROUND(SUM(IF(bucket = 'c', revenue_gbp, 0)), 2) AS rev_c,
      ROUND(SUM(IF(bucket IN ('a', 'b'), revenue_gbp, 0)), 2) AS rev_ab,
      -- % Revenue by bucket
      ROUND(SAFE_DIVIDE(SUM(IF(bucket = 'a', revenue_gbp, 0)), SUM(revenue_gbp)) * 100, 2) AS pct_rev_a,
      ROUND(SAFE_DIVIDE(SUM(IF(bucket = 'b', revenue_gbp, 0)), SUM(revenue_gbp)) * 100, 2) AS pct_rev_b,
      ROUND(SAFE_DIVIDE(SUM(IF(bucket = 'c', revenue_gbp, 0)), SUM(revenue_gbp)) * 100, 2) AS pct_rev_c,
      ROUND(SAFE_DIVIDE(SUM(IF(bucket IN ('a', 'b'), revenue_gbp, 0)), SUM(revenue_gbp)) * 100, 2) AS pct_rev_ab
    FROM base
    GROUP BY 1
    ORDER BY 1;
    """
    rows_t3 = execute_query(client, sql_t3, "T3: Weekly Trend")
    print(f"\n{'Week Commencing':<16} | {'Orders':<8} | {'Revenue (£)':<12} | {'% Rev a':<8} | {'% Rev b':<8} | {'% Rev a+b':<10} | {'% Rev c (Tagged)':<18}")
    print("-" * 95)
    for r in rows_t3:
        print(f"{str(r['week_commencing']):<16} | {r['total_orders']:<8} | {r['total_revenue']:<12.2f} | {r['pct_rev_a']:<8.2f} | {r['pct_rev_b']:<8.2f} | {r['pct_rev_ab']:<10.2f} | {r['pct_rev_c']:<18.2f}")

    # Coverage start date
    sql_cov = """
    SELECT
      MIN(IF(last_touch_url IS NOT NULL AND TRIM(last_touch_url) != '', purchase_date, NULL)) AS first_url_date,
      MIN(purchase_date) AS min_purchase_date,
      MAX(purchase_date) AS max_purchase_date
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch`;
    """
    rows_cov = execute_query(client, sql_cov, "T3: Coverage start date")
    for r in rows_cov:
        print("Coverage start:", dict(r))

    # -------------------------------------------------------------
    # T4: What sits in bucket b after 19 Oct (top 15 host & path)
    # -------------------------------------------------------------
    sql_t4 = """
    WITH base AS (
      SELECT
        t.transaction_id,
        DATE(t.purchase_at, 'Europe/London') AS order_date,
        IF(DATE(t.purchase_at, 'Europe/London') < DATE '2026-10-19', 'baseline', 'post') AS period,
        t.revenue_gbp,
        touch.last_touch_url,
        LOWER(REGEXP_EXTRACT(touch.last_touch_url, r'^https?://([^/?#]+)')) AS landing_host,
        COALESCE(REGEXP_EXTRACT(touch.last_touch_url, r'^https?://[^/?#]+(/[^?#]*)'), '/') AS landing_path
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      WHERE t.is_first_purchase = TRUE
        AND t.revenue_gbp > 0
        AND NOT IFNULL(t.is_free_entry, FALSE)
        -- Exclude pre-coverage before 31 Aug
        AND DATE(t.purchase_at, 'Europe/London') >= DATE '2026-08-31'
        AND DATE(t.purchase_at, 'Europe/London') <= DATE '2026-11-01'
        AND touch.last_touch_url IS NOT NULL AND TRIM(touch.last_touch_url) != ''
        AND NOT REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](utm_[a-z0-9_]+|gclid|gbraid|wbraid|fbclid|clickref)=')
    )
    SELECT
      period,
      landing_host,
      landing_path,
      COUNT(*) AS orders,
      ROUND(SUM(revenue_gbp), 2) AS revenue,
      ROUND(AVG(revenue_gbp), 2) AS aov,
      ANY_VALUE(REGEXP_REPLACE(last_touch_url, r'([?&](email|user_id|uid|customer_id|token)=)[^&#]+', r'\1[REDACTED]')) AS sample_url
    FROM base
    GROUP BY 1, 2, 3
    ORDER BY period DESC, revenue DESC
    LIMIT 30;
    """
    rows_t4 = execute_query(client, sql_t4, "T4: Bucket b Breakdown by Host and Path")
    print(f"\n{'Period':<10} | {'Host':<24} | {'Path':<28} | {'Orders':<8} | {'Revenue (£)':<12} | {'Sample URL'}")
    print("-" * 115)
    for r in rows_t4:
        print(f"{r['period']:<10} | {str(r['landing_host']):<24} | {str(r['landing_path']):<28} | {r['orders']:<8} | {r['revenue']:<12.2f} | {str(r['sample_url'])[:60]}")

    # Specific checks on bucket b (own domain, checkout return, klaviyo, etc.)
    sql_t4_checks = """
    WITH base AS (
      SELECT
        t.transaction_id,
        DATE(t.purchase_at, 'Europe/London') AS order_date,
        IF(DATE(t.purchase_at, 'Europe/London') < DATE '2026-10-19', 'baseline', 'post') AS period,
        t.revenue_gbp,
        touch.last_touch_url,
        LOWER(touch.last_touch_url) AS l_url,
        LOWER(REGEXP_EXTRACT(touch.last_touch_url, r'^https?://([^/?#]+)')) AS landing_host,
        COALESCE(REGEXP_EXTRACT(touch.last_touch_url, r'^https?://[^/?#]+(/[^?#]*)'), '/') AS landing_path
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      WHERE t.is_first_purchase = TRUE
        AND t.revenue_gbp > 0
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND DATE(t.purchase_at, 'Europe/London') >= DATE '2026-08-31'
        AND DATE(t.purchase_at, 'Europe/London') <= DATE '2026-11-01'
        AND touch.last_touch_url IS NOT NULL AND TRIM(touch.last_touch_url) != ''
        AND NOT REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](utm_[a-z0-9_]+|gclid|gbraid|wbraid|fbclid|clickref)=')
    )
    SELECT
      period,
      CASE
        WHEN landing_host LIKE '%wowcher%' THEN 'Wowcher sub-domain'
        WHEN REGEXP_CONTAINS(landing_path, r'checkout|basket|cart|order-received|wc-api') THEN 'Checkout / payment return'
        WHEN REGEXP_CONTAINS(l_url, r'klaviyo|_kx=') THEN 'Klaviyo link without utm'
        WHEN landing_host IN ('rafflehouse.com', 'www.rafflehouse.com', 'btr.rafflehouse.com') AND landing_path = '/' THEN 'Own domain: Homepage (direct / untagged)'
        WHEN landing_host IN ('rafflehouse.com', 'www.rafflehouse.com', 'btr.rafflehouse.com') AND landing_path LIKE '/competitions%' THEN 'Own domain: Competitions page'
        WHEN landing_host IN ('rafflehouse.com', 'www.rafflehouse.com', 'btr.rafflehouse.com') THEN 'Own domain: Other internal page'
        ELSE 'External referrer / other host'
      END AS url_category,
      COUNT(*) AS orders,
      ROUND(SUM(revenue_gbp), 2) AS revenue,
      ROUND(SAFE_DIVIDE(SUM(revenue_gbp), SUM(SUM(revenue_gbp)) OVER(PARTITION BY period)) * 100, 2) AS pct_of_period_bucket_b
    FROM base
    GROUP BY 1, 2
    ORDER BY period DESC, revenue DESC;
    """
    rows_t4_checks = execute_query(client, sql_t4_checks, "T4: Category breakdown of bucket b")
    print(f"\n{'Period':<10} | {'URL Category':<45} | {'Orders':<8} | {'Revenue (£)':<12} | {'% of Bucket b':<15}")
    print("-" * 100)
    for r in rows_t4_checks:
        print(f"{r['period']:<10} | {r['url_category']:<45} | {r['orders']:<8} | {r['revenue']:<12.2f} | {r['pct_of_period_bucket_b']:<15.2f}")

    # -------------------------------------------------------------
    # T5: Compare with old table rh_warehouse.fact_transaction_attribution
    # -------------------------------------------------------------
    sql_t5 = """
    WITH curated_base AS (
      SELECT
        t.transaction_id,
        DATE(t.purchase_at, 'Europe/London') AS order_date,
        t.revenue_gbp,
        touch.last_touch_url AS cur_url,
        CASE
          WHEN touch.last_touch_url IS NULL OR TRIM(touch.last_touch_url) = '' THEN 'a'
          WHEN NOT REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](utm_[a-z0-9_]+|gclid|gbraid|wbraid|fbclid|clickref)=') THEN 'b'
          ELSE 'c'
        END AS cur_bucket
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      WHERE t.is_first_purchase = TRUE
        AND t.revenue_gbp > 0
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND DATE(t.purchase_at, 'Europe/London') >= DATE '2026-08-31'
        AND DATE(t.purchase_at, 'Europe/London') <= DATE '2026-11-01'
    ),
    joined AS (
      SELECT
        c.transaction_id,
        c.order_date,
        c.revenue_gbp,
        c.cur_bucket,
        old.transaction_id AS old_tx_id,
        old.last_touch_url AS old_url,
        CASE
          WHEN old.transaction_id IS NULL THEN 'no_match'
          WHEN old.last_touch_url IS NULL OR TRIM(old.last_touch_url) = '' THEN 'old_a'
          WHEN NOT REGEXP_CONTAINS(LOWER(old.last_touch_url), r'[?&](utm_[a-z0-9_]+|gclid|gbraid|wbraid|fbclid|clickref)=') THEN 'old_b'
          ELSE 'old_c'
        END AS old_bucket
      FROM curated_base c
      LEFT JOIN `rh-ga4-491819.rh_warehouse.fact_transaction_attribution` old
        ON CAST(c.transaction_id AS STRING) = CAST(old.transaction_id AS STRING)
    )
    SELECT
      COUNT(*) AS total_curated_ftc,
      COUNT(old_tx_id) AS matched_old_ftc,
      ROUND(SAFE_DIVIDE(COUNT(old_tx_id), COUNT(*)) * 100, 2) AS match_rate_pct,
      cur_bucket,
      old_bucket,
      COUNT(*) AS orders,
      ROUND(SUM(revenue_gbp), 2) AS revenue
    FROM joined
    GROUP BY cur_bucket, old_bucket
    ORDER BY cur_bucket, old_bucket;
    """
    rows_t5 = execute_query(client, sql_t5, "T5: Compare with rh_warehouse.fact_transaction_attribution [RAW SOURCE / LEGACY]")
    print(f"\n{'Cur Bucket':<12} | {'Old Bucket':<15} | {'Orders':<8} | {'Revenue (£)':<12}")
    print("-" * 55)
    for r in rows_t5:
        print(f"{r['cur_bucket']:<12} | {r['old_bucket']:<15} | {r['orders']:<8} | {r['revenue']:<12.2f}")

    # -------------------------------------------------------------
    # T6: Totals Unchanged vs rpt_paid_media_entity_daily
    # -------------------------------------------------------------
    sql_t6 = """
    WITH ftc_weekly AS (
      SELECT
        DATE_TRUNC(DATE(t.purchase_at, 'Europe/London'), WEEK(MONDAY)) AS week_commencing,
        COUNT(*) AS ftc_orders,
        ROUND(SUM(t.revenue_gbp), 2) AS ftc_revenue
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      WHERE t.is_first_purchase = TRUE
        AND t.revenue_gbp > 0
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND DATE(t.purchase_at, 'Europe/London') >= DATE '2026-08-24'
        AND DATE(t.purchase_at, 'Europe/London') <= DATE '2026-11-01'
      GROUP BY 1
    ),
    pm_entities AS (
      SELECT
        DATE_TRUNC(d, WEEK(MONDAY)) AS week_commencing,
        SUM(IF(entity_id IN ('ps', 'gb', 'g-brand', 'aff'), new_cust, 0)) AS paid_new_cust,
        ROUND(SUM(IF(entity_id IN ('ps', 'gb', 'g-brand', 'aff'), no_rev + ns_rev, 0)), 2) AS paid_rev_new
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
      WHERE basis = 'L'
        AND d >= DATE '2026-08-24'
        AND d <= DATE '2026-11-01'
      GROUP BY 1
    )
    SELECT
      f.week_commencing,
      f.ftc_orders,
      p.paid_new_cust AS pm_paid_orders,
      f.ftc_orders - IFNULL(p.paid_new_cust, 0) AS diff_orders,
      f.ftc_revenue,
      p.paid_rev_new AS pm_paid_revenue,
      ROUND(f.ftc_revenue - IFNULL(p.paid_rev_new, 0), 2) AS diff_revenue
    FROM ftc_weekly f
    LEFT JOIN pm_entities p ON f.week_commencing = p.week_commencing
    ORDER BY 1;
    """
    rows_t6 = execute_query(client, sql_t6, "T6: Reconciliation with rpt_paid_media_entity_daily")
    print(f"\n{'Week Commencing':<16} | {'FTC Orders':<12} | {'PM Paid Orders':<16} | {'Diff Orders':<12} | {'FTC Rev (£)':<14} | {'PM Paid Rev (£)':<18} | {'Diff Rev (£)':<12}")
    print("-" * 115)
    for r in rows_t6:
        print(f"{str(r['week_commencing']):<16} | {str(r['ftc_orders']):<12} | {str(r['pm_paid_orders']):<16} | {str(r['diff_orders']):<12} | {str(r['ftc_revenue']):<14} | {str(r['pm_paid_revenue']):<18} | {str(r['diff_revenue']):<12}")

    # -------------------------------------------------------------
    # T7: Impact on Campaign Reading (w/c 19 Oct and w/c 26 Oct)
    # -------------------------------------------------------------
    sql_t7 = """
    WITH ftc_classified AS (
      SELECT
        t.transaction_id,
        DATE(t.purchase_at, 'Europe/London') AS order_date,
        DATE_TRUNC(DATE(t.purchase_at, 'Europe/London'), WEEK(MONDAY)) AS week_commencing,
        IF(DATE(t.purchase_at, 'Europe/London') < DATE '2026-10-19', 'baseline', 'post') AS period,
        t.revenue_gbp,
        touch.last_touch_url,
        CASE
          WHEN touch.last_touch_url IS NULL OR TRIM(touch.last_touch_url) = '' THEN 'a'
          WHEN NOT REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](utm_[a-z0-9_]+|gclid|gbraid|wbraid|fbclid|clickref)=') THEN 'b'
          ELSE 'c'
        END AS bucket,
        CASE
          WHEN touch.last_touch_url IS NOT NULL
           AND REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](gclid|gbraid|wbraid|gad_source|gad_campaignid)=|utm_source=google')
            THEN 'Google'
          WHEN touch.last_touch_url IS NOT NULL
           AND (REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&]fbclid=') OR REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'utm_source=(meta|fb|ig|facebook|instagram)'))
            THEN 'Meta'
          WHEN touch.last_touch_url IS NOT NULL
           AND REGEXP_CONTAINS(LOWER(touch.last_touch_url), r'[?&](utm_[a-z0-9_]+|clickref)=')
            THEN 'Other Tagged'
          ELSE 'Untracked (a+b)'
        END AS regex_platform
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      WHERE t.is_first_purchase = TRUE
        AND t.revenue_gbp > 0
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND DATE(t.purchase_at, 'Europe/London') >= DATE '2026-08-31'
        AND DATE(t.purchase_at, 'Europe/London') <= DATE '2026-11-01'
    ),
    baseline_totals AS (
      SELECT
        SUM(revenue_gbp) AS base_total_rev,
        SUM(IF(regex_platform IN ('Google', 'Meta', 'Other Tagged'), revenue_gbp, 0)) AS base_tagged_rev,
        SUM(IF(regex_platform = 'Meta', revenue_gbp, 0)) AS base_meta_rev,
        SUM(IF(regex_platform = 'Google', revenue_gbp, 0)) AS base_google_rev,
        SUM(IF(regex_platform = 'Other Tagged', revenue_gbp, 0)) AS base_other_rev,
        SUM(IF(regex_platform = 'Untracked (a+b)', revenue_gbp, 0)) AS base_untracked_rev
      FROM ftc_classified
      WHERE period = 'baseline'
    ),
    post_weeks AS (
      SELECT
        week_commencing,
        COUNT(*) AS total_orders,
        SUM(revenue_gbp) AS total_revenue,
        -- Meta
        COUNTIF(regex_platform = 'Meta') AS meta_orders,
        SUM(IF(regex_platform = 'Meta', revenue_gbp, 0)) AS meta_rev,
        -- Google
        COUNTIF(regex_platform = 'Google') AS google_orders,
        SUM(IF(regex_platform = 'Google', revenue_gbp, 0)) AS google_rev,
        -- Other Tagged
        COUNTIF(regex_platform = 'Other Tagged') AS other_orders,
        SUM(IF(regex_platform = 'Other Tagged', revenue_gbp, 0)) AS other_rev,
        -- Untracked
        COUNTIF(regex_platform = 'Untracked (a+b)') AS untracked_orders,
        SUM(IF(regex_platform = 'Untracked (a+b)', revenue_gbp, 0)) AS untracked_rev
      FROM ftc_classified
      WHERE week_commencing IN (DATE '2026-10-19', DATE '2026-10-26')
      GROUP BY 1
    )
    SELECT
      pw.week_commencing,
      -- Reported
      pw.meta_orders,
      pw.meta_rev AS reported_meta_rev,
      ROUND(SAFE_DIVIDE(pw.meta_rev, pw.total_revenue) * 100, 2) AS reported_meta_share_pct,
      pw.google_orders,
      pw.google_rev AS reported_google_rev,
      ROUND(SAFE_DIVIDE(pw.google_rev, pw.total_revenue) * 100, 2) AS reported_google_share_pct,
      pw.untracked_orders,
      pw.untracked_rev AS untracked_pool_rev,
      ROUND(SAFE_DIVIDE(pw.untracked_rev, pw.total_revenue) * 100, 2) AS untracked_share_pct,
      pw.total_revenue,
      -- Baseline tagged shares
      ROUND(SAFE_DIVIDE(bt.base_meta_rev, bt.base_tagged_rev) * 100, 2) AS base_meta_pct_of_tagged,
      ROUND(SAFE_DIVIDE(bt.base_google_rev, bt.base_tagged_rev) * 100, 2) AS base_google_pct_of_tagged,
      -- Scaled estimates [ESTIMATE]: reallocating untracked pool by baseline tagged share
      ROUND(pw.meta_rev + (pw.untracked_rev * SAFE_DIVIDE(bt.base_meta_rev, bt.base_tagged_rev)), 2) AS est_scaled_meta_rev,
      ROUND(pw.google_rev + (pw.untracked_rev * SAFE_DIVIDE(bt.base_google_rev, bt.base_tagged_rev)), 2) AS est_scaled_google_rev
    FROM post_weeks pw
    CROSS JOIN baseline_totals bt
    ORDER BY pw.week_commencing;
    """
    rows_t7 = execute_query(client, sql_t7, "T7: Impact on Campaign Reading")
    print(f"\n{'Week':<12} | {'Platform':<8} | {'Reported Rev (£)':<18} | {'Reported %':<12} | {'Estimated Scaled Rev (£)':<25}")
    print("-" * 85)
    for r in rows_t7:
        print(f"{str(r['week_commencing']):<12} | Meta     | £{r['reported_meta_rev']:<16.2f} | {r['reported_meta_share_pct']:<10.2f}% | £{r['est_scaled_meta_rev']:<22.2f} [ESTIMATE]")
        print(f"{str(r['week_commencing']):<12} | Google   | £{r['reported_google_rev']:<16.2f} | {r['reported_google_share_pct']:<10.2f}% | £{r['est_scaled_google_rev']:<22.2f} [ESTIMATE]")
        print(f"{str(r['week_commencing']):<12} | Untracked| £{r['untracked_pool_rev']:<16.2f} | {r['untracked_share_pct']:<10.2f}% | Reallocated ({r['base_meta_pct_of_tagged']}% Meta / {r['base_google_pct_of_tagged']}% Google)")
        print("-" * 85)

    # -------------------------------------------------------------
    # JOBS & COST REPORTING via INFORMATION_SCHEMA.JOBS_BY_PROJECT
    # -------------------------------------------------------------
    job_ids = [j["job_id"] for j in EXECUTED_JOBS]
    formatted_job_ids = ", ".join(f"'{jid}'" for jid in job_ids)
    sql_costs = f"""
    SELECT
      job_id,
      creation_time,
      total_bytes_billed,
      ROUND(total_bytes_billed / (1024 * 1024 * 1024), 4) AS gb_billed,
      ROUND((total_bytes_billed / (1024 * 1024 * 1024 * 1024)) * 6.25 * 0.775, 4) AS cost_gbp
    FROM `region-europe-west2`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE job_id IN ({formatted_job_ids})
    ORDER BY creation_time ASC;
    """
    rows_costs = execute_query(client, sql_costs, "Job Cost Verification via INFORMATION_SCHEMA")
    print("\n=======================================================")
    print("QUERY COST AND BYTES AUDIT")
    print("=======================================================")
    cost_map = {r["job_id"]: r for r in rows_costs}
    total_bytes = 0
    total_cost_gbp = Decimal("0")
    for j in EXECUTED_JOBS:
        jid = j["job_id"]
        r = cost_map.get(jid)
        if r:
            bytes_b = r["total_bytes_billed"] or 0
            gb_b = r["gb_billed"]
            cost = Decimal(str(r["cost_gbp"] or 0))
        else:
            bytes_b = j["bytes_scanned"]
            gb_b = round(bytes_b / (1024**3), 4)
            cost = Decimal(str(round((bytes_b / (1024**4)) * 6.25 * 0.775, 4)))
        total_bytes += bytes_b
        total_cost_gbp += cost
        print(f"[{jid}] {j['description']}: {bytes_b:,} bytes billed ({gb_b} GB) | £{cost:.4f}")
    print("-" * 75)
    print(f"TOTAL: {total_bytes:,} bytes ({total_bytes / (1024**3):.4f} GB) | Estimated Cost: £{total_cost_gbp:.4f}")

if __name__ == "__main__":
    main()
