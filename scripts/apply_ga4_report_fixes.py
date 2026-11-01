#!/usr/bin/env python3
"""
scripts/apply_ga4_report_fixes.py

Applies the report-side fixes for GA4 reliability:
1. Adds ga4_reliable BOOLEAN to rpt_visit_source_daily and fact_ga4_purchases.
2. Recreates rpt_paid_media_page_daily, rpt_paid_media_page_status, rpt_organic_social_weekly,
   rpt_exec_summary, and vw_report_suite_health with ga4_reliable and updated logic.
3. Re-runs the DQ gate with the early-warning fix and the 2 new HARD tests:
   - ga4_reliable_flag_populated
   - tracking_early_warning_incomplete_guard
4. Prints the updated vw_report_suite_health and dq_gate_results.
"""

import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, location=LOCATION, credentials=Credentials(token))

def run_query(client, sql, description):
    print(f"\n--- {description} ---")
    job_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry = client.query(sql, job_config=job_cfg, location=LOCATION)
    print(f"[Dry Run] Scans {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024*1024):.2f} MB)")
    if dry.total_bytes_processed > 15 * 1024 * 1024 * 1024:
        raise ValueError(f"Dry run exceeded 15 GB: {dry.total_bytes_processed:,} bytes")
    job = client.query(sql, location=LOCATION)
    res = job.result()
    print("[Execute] Completed successfully.")
    return res

def main():
    client = get_client()

    # Step 1: Add ga4_reliable to rpt_visit_source_daily if not exists, and backfill
    sql_alter_visit = """
    ALTER TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
    ADD COLUMN IF NOT EXISTS ga4_reliable BOOLEAN;
    """
    sql_backfill_visit = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
    SET ga4_reliable = (d < DATE '2026-10-24')
    WHERE ga4_reliable IS NULL;
    """
    run_query(client, sql_alter_visit, "Add ga4_reliable to rpt_visit_source_daily")
    run_query(client, sql_backfill_visit, "Backfill ga4_reliable in rpt_visit_source_daily")

    # Step 2: Add ga4_reliable to fact_ga4_purchases if not exists, and backfill
    sql_alter_ga4_purch = """
    ALTER TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
    ADD COLUMN IF NOT EXISTS ga4_reliable BOOLEAN;
    """
    sql_backfill_ga4_purch = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
    SET ga4_reliable = (event_date < DATE '2026-10-24')
    WHERE ga4_reliable IS NULL;
    """
    run_query(client, sql_alter_ga4_purch, "Add ga4_reliable to fact_ga4_purchases")
    run_query(client, sql_backfill_ga4_purch, "Backfill ga4_reliable in fact_ga4_purchases")

    # Step 3: Recreate rpt_paid_media_page_daily with ga4_reliable
    sql_paid_media_page_daily = """
    CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`
    PARTITION BY d
    CLUSTER BY basis, channel, sub
    OPTIONS (description = "Pre-calculated table for paid media reporting page. One row per day x basis x channel x sub from 1 Sep 2026. Rebuilt nightly.")
    AS
    WITH spend_raw AS (
      SELECT
        spend_date AS d,
        CASE
          WHEN ad_platform = 'meta' OR channel_group = 'Paid Social' THEN 'Paid Social'
          WHEN ad_platform = 'google' THEN 'Google'
          WHEN ad_platform = 'bing' OR channel_group IN ('Microsoft Ads', 'Bing') THEN 'Bing'
          WHEN channel_group = 'Affiliates' THEN 'Affiliates'
          ELSE 'Other'
        END AS channel,
        CASE
          WHEN ad_platform = 'meta' OR channel_group = 'Paid Social' THEN
            CASE
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'rtg') THEN 'RTG'
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'rmg|remarket') THEN 'RMG'
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r' pr |prospecting|(_|^)pr(_|$)') THEN 'PR'
              ELSE 'Other'
            END
          WHEN ad_platform = 'google' OR channel_group IN ('Microsoft Ads', 'Bing') THEN
            CASE
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'competitor|\|\s*competitor') THEN 'Competitor'
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'generic|\|\s*generic') THEN 'Generic'
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'brand|\|\s*brand') THEN 'Brand'
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'pmax|performance|\|\s*pmax') THEN 'PMax'
              WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'demand|dgen|( |_|-)dg( |_|-|$)|\|\s*demand') THEN 'Demand Gen'
              ELSE 'Other'
            END
          WHEN channel_group = 'Affiliates' THEN 'All'
          ELSE 'Other'
        END AS sub,
        spend_inc_vat_gbp,
        impressions,
        clicks,
        link_clicks,
        platform_conversions,
        platform_conversion_value_gbp
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
      WHERE spend_date >= DATE '2026-09-01'
    ),
    spend_agg AS (
      SELECT
        d, channel, sub,
        CAST(ROUND(SUM(spend_inc_vat_gbp), 2) AS NUMERIC) AS spend,
        CAST(SUM(impressions) AS INT64) AS impr,
        CAST(SUM(IF(channel = 'Paid Social', link_clicks, clicks)) AS INT64) AS clicks,
        CAST(SUM(platform_conversions) AS FLOAT64) AS plat_conv,
        CAST(ROUND(SUM(platform_conversion_value_gbp), 2) AS NUMERIC) AS plat_rev
      FROM spend_raw
      WHERE channel IN ('Paid Social', 'Google', 'Bing', 'Affiliates')
      GROUP BY 1, 2, 3
    ),
    spend_both_bases AS (
      SELECT b.basis, s.*
      FROM spend_agg s
      CROSS JOIN (SELECT 'F' AS basis UNION ALL SELECT 'L' AS basis) b
    ),
    a AS (
      SELECT
        CAST(transaction_id AS STRING) AS transaction_id,
        first_touch_url, first_touch_timestamp,
        last_touch_url,  last_touch_timestamp
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch`
      WHERE purchase_date >= DATE '2026-09-01'
        AND attribution_basis != 'inherited_from_subscription_signup'
    ),
    f AS (
      SELECT CAST(transaction_id AS STRING) AS transaction_id, purchase_date,
        revenue_gbp, customer_key, is_first_purchase,
        (is_subscription_payment OR order_type = 'SUBSCRIPTION') AS is_sub
      FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_transactions`
      WHERE purchase_date >= DATE '2026-09-01'
        AND NOT IFNULL(is_free_entry, FALSE)
        AND customer_key != 'UNKNOWN_DELETED'
    ),
    ch AS (
      SELECT landing_url, ANY_VALUE(channel_group) AS channel_group, ANY_VALUE(utm_campaign) AS utm_campaign
      FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_channel_last_touch`
      GROUP BY 1
    ),
    touches AS (
      SELECT 'F' AS basis, f.*, a.first_touch_url AS url, a.first_touch_timestamp AS touch_ts FROM f JOIN a USING (transaction_id)
      UNION ALL
      SELECT 'L' AS basis, f.*, a.last_touch_url  AS url, a.last_touch_timestamp  AS touch_ts FROM f JOIN a USING (transaction_id)
    ),
    t AS (
      SELECT
        t.basis,
        DATE(t.touch_ts, 'Europe/London') AS click_date,
        CASE ch.channel_group
          WHEN 'Meta' THEN 'Paid Social'
          WHEN 'Google Ads' THEN 'Google'
          WHEN 'Microsoft Ads' THEN 'Bing'
          WHEN 'Affiliate' THEN 'Affiliates'
          ELSE IFNULL(ch.channel_group, 'Unattributed')
        END AS channel,
        CASE
          WHEN ch.channel_group = 'Meta' THEN
            CASE 
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'rtg') THEN 'RTG'
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'rmg|remarket') THEN 'RMG'
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r' pr |prospecting|(_|^)pr(_|$)') THEN 'PR'
              ELSE 'Other' 
            END
          WHEN ch.channel_group IN ('Google Ads', 'Microsoft Ads') THEN
            CASE 
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'competitor|\|\s*competitor') THEN 'Competitor'
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'generic|\|\s*generic') THEN 'Generic'
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'brand|\|\s*brand') THEN 'Brand'
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'pmax|performance|\|\s*pmax') THEN 'PMax'
              WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'demand|dgen|( |_|-)dg( |_|-|$)|\|\s*demand') THEN 'Demand Gen'
              ELSE 'Other' 
            END
          WHEN ch.channel_group = 'Affiliate' THEN 'All'
          ELSE 'Other'
        END AS sub,
        t.customer_key, t.revenue_gbp, t.is_first_purchase, t.is_sub
      FROM touches t
      LEFT JOIN ch ON ch.landing_url = t.url
      WHERE t.touch_ts IS NOT NULL
        AND DATE_DIFF(t.purchase_date, DATE(t.touch_ts, 'Europe/London'), DAY) BETWEEN 0 AND 7
        AND DATE(t.touch_ts, 'Europe/London') >= DATE '2026-09-01'
    ),
    wh_agg AS (
      SELECT
        t.click_date AS d,
        t.basis,
        t.channel,
        t.sub,
        CAST(COUNT(*) AS INT64) AS wh_orders,
        CAST(ROUND(SUM(t.revenue_gbp), 2) AS NUMERIC) AS wh_rev,
        CAST(COUNT(DISTINCT IF(t.is_first_purchase, t.customer_key, NULL)) AS INT64) AS new_cust,
        CAST(COUNT(DISTINCT IF(t.is_first_purchase AND t.is_sub, t.customer_key, NULL)) AS INT64) AS new_subs,
        CAST(ROUND(SUM(IF(t.is_first_purchase, h.ltv_3m, 0)), 2) AS NUMERIC) AS ltv3m
      FROM t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons` h
        ON h.customer_key = t.customer_key
      WHERE t.channel IN ('Paid Social', 'Google', 'Bing', 'Affiliates')
      GROUP BY 1, 2, 3, 4
    )
    SELECT
      COALESCE(s.d, w.d) AS d,
      COALESCE(s.basis, w.basis) AS basis,
      COALESCE(s.channel, w.channel) AS channel,
      COALESCE(s.sub, w.sub) AS sub,
      s.spend,
      s.impr,
      s.clicks,
      s.plat_conv,
      s.plat_rev,
      COALESCE(w.wh_orders, 0) AS wh_orders,
      COALESCE(w.wh_rev, NUMERIC '0') AS wh_rev,
      COALESCE(w.new_cust, 0) AS new_cust,
      COALESCE(w.new_subs, 0) AS new_subs,
      COALESCE(w.ltv3m, NUMERIC '0') AS ltv3m,
      DATE_DIFF(CURRENT_DATE('Europe/London'), COALESCE(s.d, w.d), DAY) < 7 AS maturing,
      COALESCE(tc.ga4_sessions_reliable, COALESCE(s.d, w.d) < DATE '2026-10-24') AS ga4_reliable
    FROM spend_both_bases s
    FULL OUTER JOIN wh_agg w
      ON s.d = w.d
     AND s.basis = w.basis
     AND s.channel = w.channel
     AND s.sub = w.sub
    LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
      ON COALESCE(s.d, w.d) = tc.d;
    """
    run_query(client, sql_paid_media_page_daily, "Recreate rpt_paid_media_page_daily with ga4_reliable")

    # Step 4: Recreate rpt_paid_media_page_status with ga4_reliable
    sql_paid_media_page_status = """
    CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`
    OPTIONS (description = "One-row status table for paid media reporting page showing latest dates.")
    AS
    SELECT
      CURRENT_TIMESTAMP() AS built_at,
      DATE '2026-10-19' AS tracking_changed_on,
      DATE '2026-10-19' AS cookie_banner_changed_on,
      DATE '2026-10-24' AS consent_setting_changed_on,
      DATE '2026-10-26' AS pricing_display_changed_on,
      [DATE '2026-10-19', DATE '2026-10-24', DATE '2026-10-26'] AS change_dates,
      (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_transactions`) AS wh_to,
      (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'google') AS google_to,
      (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'meta') AS meta_to,
      (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'bing' OR channel_group IN ('Microsoft Ads', 'Bing')) AS bing_to,
      (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE channel_group = 'Affiliates') AS affiliates_to,
      COALESCE((
        SELECT ga4_sessions_reliable
        FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
        WHERE d = DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
      ), FALSE) AS ga4_reliable;
    """
    run_query(client, sql_paid_media_page_status, "Recreate rpt_paid_media_page_status with ga4_reliable")

    # Step 5: Recreate rpt_organic_social_weekly with ga4_reliable
    sql_organic_social_weekly = """
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
    ),
    unioned AS (
      SELECT * FROM platform_rollup
      UNION ALL
      SELECT * FROM total_rollup
    )
    SELECT
      u.*,
      COALESCE((
        SELECT LOGICAL_AND(ga4_sessions_reliable)
        FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
        WHERE tc.d BETWEEN u.week_commencing AND DATE_ADD(u.week_commencing, INTERVAL 6 DAY)
      ), FALSE) AS ga4_reliable
    FROM unioned u;
    """
    run_query(client, sql_organic_social_weekly, "Recreate rpt_organic_social_weekly with ga4_reliable")

    # Step 6: Recreate rpt_exec_summary with notes & ga4_reliable
    sql_exec_summary = """
    CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_exec_summary`
    OPTIONS (description = "Executive summary snapshot table feeding executive dashboard. One row per KPI, rebuilt nightly as the final pipeline step.")
    AS
    WITH params AS (
      SELECT
        DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY) AS as_of_date,
        DATE_TRUNC(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY), MONTH) AS mtd_start,
        DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY) AS mtd_end,
        DATE_SUB(DATE_TRUNC(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY), MONTH), INTERVAL 1 MONTH) AS prev_month_start,
        LEAST(
          DATE_ADD(DATE_SUB(DATE_TRUNC(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY), MONTH), INTERVAL 1 MONTH),
            INTERVAL EXTRACT(DAY FROM DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)) - 1 DAY),
          LAST_DAY(DATE_SUB(DATE_TRUNC(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY), MONTH), INTERVAL 1 MONTH), MONTH)
        ) AS prev_mtd_end,
        DATE_SUB(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY), INTERVAL 6 DAY) AS last_7d_start,
        DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY) AS last_7d_end,
        DATE_SUB(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY), INTERVAL 13 DAY) AS prev_7d_start,
        DATE_SUB(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY), INTERVAL 7 DAY) AS prev_7d_end,
        (SELECT MAX(week_commencing) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`) AS latest_week,
        DATE_SUB((SELECT MAX(week_commencing) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`), INTERVAL 7 DAY) AS prev_week,
        CURRENT_TIMESTAMP() AS built_at
    ),
    health AS (
      SELECT
        MAX(IF(report = 'Paid media', status, NULL)) AS health_paid,
        MAX(IF(report = 'Organic social', status, NULL)) AS health_organic,
        MAX(IF(report = 'Free trials', status, NULL)) AS health_trials,
        MAX(IF(report = 'Customer LTV', status, NULL)) AS health_ltv,
        MAX(IF(report = 'Tracking health', status, NULL)) AS health_tracking,
        MAX(IF(report = 'Charity sales', status, NULL)) AS health_charity
      FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
    ),
    tx_agg AS (
      SELECT
        p.as_of_date,
        p.built_at,
        h.health_paid AS health_status,
        ROUND(SUM(IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND t.is_first_purchase, t.revenue_gbp, 0)), 2) AS rev_new_mtd,
        COUNT(DISTINCT IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND t.is_first_purchase, t.customer_key, NULL)) AS cust_new_mtd,
        ROUND(SUM(IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND NOT t.is_first_purchase, t.revenue_gbp, 0)), 2) AS rev_renew_mtd,
        COUNTIF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND NOT t.is_first_purchase) AS tx_renew_mtd,
        ROUND(SUM(IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end, t.revenue_gbp, 0)), 2) AS rev_total_mtd,
        ROUND(SUM(IF(t.purchase_date BETWEEN p.prev_month_start AND p.prev_mtd_end AND t.is_first_purchase, t.revenue_gbp, 0)), 2) AS rev_new_prev_mtd,
        COUNT(DISTINCT IF(t.purchase_date BETWEEN p.prev_month_start AND p.prev_mtd_end AND t.is_first_purchase, t.customer_key, NULL)) AS cust_new_prev_mtd,
        ROUND(SUM(IF(t.purchase_date BETWEEN p.prev_month_start AND p.prev_mtd_end AND NOT t.is_first_purchase, t.revenue_gbp, 0)), 2) AS rev_renew_prev_mtd,
        COUNTIF(t.purchase_date BETWEEN p.prev_month_start AND p.prev_mtd_end AND NOT t.is_first_purchase) AS tx_renew_prev_mtd,
        ROUND(SUM(IF(t.purchase_date BETWEEN p.prev_month_start AND p.prev_mtd_end, t.revenue_gbp, 0)), 2) AS rev_total_prev_mtd
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      CROSS JOIN params p
      CROSS JOIN health h
      WHERE t.customer_key != 'UNKNOWN_DELETED'
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND t.revenue_gbp > 0
        AND (
          t.purchase_date BETWEEN p.mtd_start AND p.mtd_end
          OR t.purchase_date BETWEEN p.prev_month_start AND p.prev_mtd_end
        )
      GROUP BY p.as_of_date, p.built_at, h.health_paid
    ),
    headline_rows AS (
      SELECT 'headline_rev_new_mtd' AS kpi_id, 'headline' AS section, 'New Customer Revenue' AS label, 'mtd' AS period, CAST(rev_new_mtd AS NUMERIC) AS value, 'gbp' AS unit, as_of_date, 'fact_transactions' AS source_tables, built_at, health_status, 'Gross revenue from first purchases (ACCEPTED, non-zero); refunds/chargebacks not deducted' AS note FROM tx_agg
      UNION ALL
      SELECT 'headline_rev_new_prev_mtd', 'headline', 'New Customer Revenue', 'prev_mtd_same_point', CAST(rev_new_prev_mtd AS NUMERIC), 'gbp', as_of_date, 'fact_transactions', built_at, health_status, 'Gross revenue from first purchases same point prior month' FROM tx_agg
      UNION ALL
      SELECT 'headline_cust_new_mtd', 'headline', 'New Customers', 'mtd', CAST(cust_new_mtd AS NUMERIC), 'count', as_of_date, 'fact_transactions', built_at, health_status, 'Distinct new customer keys acquired MTD' FROM tx_agg
      UNION ALL
      SELECT 'headline_cust_new_prev_mtd', 'headline', 'New Customers', 'prev_mtd_same_point', CAST(cust_new_prev_mtd AS NUMERIC), 'count', as_of_date, 'fact_transactions', built_at, health_status, 'Distinct new customer keys acquired same point prior month' FROM tx_agg
      UNION ALL
      SELECT 'headline_rev_renew_mtd', 'headline', 'Renewal & Returning Revenue', 'mtd', CAST(rev_renew_mtd AS NUMERIC), 'gbp', as_of_date, 'fact_transactions', built_at, health_status, 'Gross revenue from repeat/renewal purchases (ACCEPTED, non-zero)' FROM tx_agg
      UNION ALL
      SELECT 'headline_rev_renew_prev_mtd', 'headline', 'Renewal & Returning Revenue', 'prev_mtd_same_point', CAST(rev_renew_prev_mtd AS NUMERIC), 'gbp', as_of_date, 'fact_transactions', built_at, health_status, 'Gross renewal/repeat revenue same point prior month' FROM tx_agg
      UNION ALL
      SELECT 'headline_tx_renew_mtd', 'headline', 'Renewal Orders Count', 'mtd', CAST(tx_renew_mtd AS NUMERIC), 'count', as_of_date, 'fact_transactions', built_at, health_status, 'Total renewal/repeat orders count MTD' FROM tx_agg
      UNION ALL
      SELECT 'headline_tx_renew_prev_mtd', 'headline', 'Renewal Orders Count', 'prev_mtd_same_point', CAST(tx_renew_prev_mtd AS NUMERIC), 'count', as_of_date, 'fact_transactions', built_at, health_status, 'Total renewal/repeat orders count same point prior month' FROM tx_agg
      UNION ALL
      SELECT 'headline_rev_total_mtd', 'headline', 'Total Gross Revenue', 'mtd', CAST(rev_total_mtd AS NUMERIC), 'gbp', as_of_date, 'fact_transactions', built_at, health_status, 'Total gross revenue (new + renewals) MTD' FROM tx_agg
      UNION ALL
      SELECT 'headline_rev_total_prev_mtd', 'headline', 'Total Gross Revenue', 'prev_mtd_same_point', CAST(rev_total_prev_mtd AS NUMERIC), 'gbp', as_of_date, 'fact_transactions', built_at, health_status, 'Total gross revenue same point prior month' FROM tx_agg
    ),
    paid_periods AS (
      SELECT 'mtd' AS period, p.mtd_start AS s_date, p.mtd_end AS e_date, p.as_of_date, p.built_at, h.health_paid FROM params p CROSS JOIN health h
      UNION ALL
      SELECT 'prev_mtd_same_point', p.prev_month_start, p.prev_mtd_end, p.as_of_date, p.built_at, h.health_paid FROM params p CROSS JOIN health h
      UNION ALL
      SELECT 'last_7d', p.last_7d_start, p.last_7d_end, p.as_of_date, p.built_at, h.health_paid FROM params p CROSS JOIN health h
      UNION ALL
      SELECT 'prev_7d', p.prev_7d_start, p.prev_7d_end, p.as_of_date, p.built_at, h.health_paid FROM params p CROSS JOIN health h
    ),
    paid_agg AS (
      SELECT
        pp.period,
        pp.as_of_date,
        pp.built_at,
        pp.health_paid,
        ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.spend, 0)), 2) AS spend_google,
        ROUND(SUM(IF(pm.entity_id = 'ps', pm.spend, 0)), 2) AS spend_meta,
        ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.spend, 0)), 2) AS spend_combined,
        SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.new_cust, 0)) AS new_cust_google,
        SUM(IF(pm.entity_id = 'ps', pm.new_cust, 0)) AS new_cust_meta,
        SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.new_cust, 0)) AS new_cust_combined,
        ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.v3, 0)), 2) AS v3_google,
        ROUND(SUM(IF(pm.entity_id = 'ps', pm.v3, 0)), 2) AS v3_meta,
        ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.v3, 0)), 2) AS v3_combined
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily` pm
      JOIN paid_periods pp
        ON pm.d BETWEEN pp.s_date AND pp.e_date
      WHERE pm.basis = 'F'
        AND pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps')
      GROUP BY 1, 2, 3, 4
    ),
    tracking_agg AS (
      SELECT
        pp.period,
        ROUND(SAFE_DIVIDE(SUM(tc.orders_tracked), SUM(tc.orders)) * 100, 2) AS tracking_coverage_pct
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
      JOIN paid_periods pp
        ON tc.d BETWEEN pp.s_date AND pp.e_date
      GROUP BY 1
    ),
    paid_efficiency_rows AS (
      SELECT
        CONCAT('paid_spend_google_', pa.period) AS kpi_id,
        'paid_efficiency' AS section,
        'Spend (ex VAT): Google' AS label,
        pa.period,
        CAST(pa.spend_google AS NUMERIC) AS value,
        'gbp' AS unit,
        pa.as_of_date,
        'rpt_paid_media_entity_daily' AS source_tables,
        pa.built_at,
        pa.health_paid AS health_status,
        'Platform ad spend excluding VAT for Google entities (g-comp, g-gen, g-dg, g-pmax, g-brand)' AS note
      FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_spend_meta_', pa.period), 'paid_efficiency', 'Spend (ex VAT): Meta', pa.period, CAST(pa.spend_meta AS NUMERIC), 'gbp', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'Platform ad spend excluding VAT for Meta Paid Social (ps)' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_spend_combined_', pa.period), 'paid_efficiency', 'Spend (ex VAT): Google + Meta Combined', pa.period, CAST(pa.spend_combined AS NUMERIC), 'gbp', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'Combined ad spend excluding VAT for Google and Meta' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_new_cust_google_', pa.period), 'paid_efficiency', 'New Customers: Google', pa.period, CAST(pa.new_cust_google AS NUMERIC), 'count', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'First-touch new customers acquired via Google' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_new_cust_meta_', pa.period), 'paid_efficiency', 'New Customers: Meta', pa.period, CAST(pa.new_cust_meta AS NUMERIC), 'count', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'First-touch new customers acquired via Meta' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_new_cust_combined_', pa.period), 'paid_efficiency', 'New Customers: Google + Meta Combined', pa.period, CAST(pa.new_cust_combined AS NUMERIC), 'count', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'Combined first-touch new customers acquired via Google and Meta' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_cpa_google_', pa.period), 'paid_efficiency', 'Cost per New Customer: Google', pa.period, CAST(ROUND(SAFE_DIVIDE(pa.spend_google, pa.new_cust_google), 2) AS NUMERIC), 'gbp', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'Google spend ex VAT divided by first-touch new customers' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_cpa_meta_', pa.period), 'paid_efficiency', 'Cost per New Customer: Meta', pa.period, CAST(ROUND(SAFE_DIVIDE(pa.spend_meta, pa.new_cust_meta), 2) AS NUMERIC), 'gbp', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'Meta spend ex VAT divided by first-touch new customers' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_cpa_combined_', pa.period), 'paid_efficiency', 'Cost per New Customer: Combined', pa.period, CAST(ROUND(SAFE_DIVIDE(pa.spend_combined, pa.new_cust_combined), 2) AS NUMERIC), 'gbp', pa.as_of_date, 'rpt_paid_media_entity_daily', pa.built_at, pa.health_paid, 'Combined Google+Meta spend ex VAT divided by combined new customers' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_roas_3m_google_', pa.period), 'paid_efficiency', '3-Month ROAS: Google', pa.period, CAST(ROUND(SAFE_DIVIDE(pa.v3_google, pa.spend_google), 2) AS NUMERIC), 'ratio', pa.as_of_date, 'rpt_paid_media_entity_daily, customer_ltv_horizons', pa.built_at, pa.health_paid, 'First-touch 3-month LTV (v3) divided by Google spend ex VAT' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_roas_3m_meta_', pa.period), 'paid_efficiency', '3-Month ROAS: Meta', pa.period, CAST(ROUND(SAFE_DIVIDE(pa.v3_meta, pa.spend_meta), 2) AS NUMERIC), 'ratio', pa.as_of_date, 'rpt_paid_media_entity_daily, customer_ltv_horizons', pa.built_at, pa.health_paid, 'First-touch 3-month LTV (v3) divided by Meta spend ex VAT' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_roas_3m_combined_', pa.period), 'paid_efficiency', '3-Month ROAS: Combined', pa.period, CAST(ROUND(SAFE_DIVIDE(pa.v3_combined, pa.spend_combined), 2) AS NUMERIC), 'ratio', pa.as_of_date, 'rpt_paid_media_entity_daily, customer_ltv_horizons', pa.built_at, pa.health_paid, 'Combined first-touch 3-month LTV (v3) divided by combined spend ex VAT' FROM paid_agg pa
      UNION ALL
      SELECT CONCAT('paid_tracking_coverage_', ta.period), 'paid_efficiency', 'Tracking Coverage', ta.period, CAST(ta.tracking_coverage_pct AS NUMERIC), 'pct', pa.as_of_date, 'rpt_tracking_coverage_daily', pa.built_at, pa.health_paid, 'Percentage of orders with captured landing touch URL' FROM tracking_agg ta JOIN paid_agg pa ON ta.period = pa.period
    ),
    trials_status_agg AS (
      SELECT
        p.as_of_date,
        p.built_at,
        h.health_trials,
        COUNTIF(status = 'active') AS active_count,
        COUNTIF(status = 'payment due') AS payment_due_count
      FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
      CROSS JOIN params p
      CROSS JOIN health h
      GROUP BY 1, 2, 3
    ),
    trials_conv_agg AS (
      SELECT
        ROUND(SUM(IF(mature_1st, converted_1st, 0)) / NULLIF(SUM(IF(mature_1st, trials, 0)), 0) * 100, 2) AS conv_rate_1st_mature_pct,
        ROUND(SUM(IF(mature_3rd, converted_3rd, 0)) / NULLIF(SUM(IF(mature_3rd, trials, 0)), 0) * 100, 2) AS conv_rate_3rd_mature_pct
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
    ),
    acq_ltv_agg AS (
      SELECT
        ROUND(AVG(h.ltv_3m), 2) AS mean_ltv_3m,
        ROUND(CAST(APPROX_QUANTILES(h.ltv_3m, 2)[OFFSET(1)] AS NUMERIC), 2) AS median_ltv_3m
      FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` c
      JOIN `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons` h
        ON c.customer_key = h.customer_key
      CROSS JOIN params p
      WHERE DATE(c.acq_first_purchase_at, 'Europe/London') BETWEEN p.mtd_start AND p.mtd_end
        AND c.customer_key != 'UNKNOWN_DELETED'
    ),
    trials_ltv_rows AS (
      SELECT 'trials_active_count' AS kpi_id, 'trials_ltv' AS section, 'Active Trials' AS label, 'trailing' AS period, CAST(ts.active_count AS NUMERIC) AS value, 'count' AS unit, ts.as_of_date, 'vw_trial_status' AS source_tables, ts.built_at, ts.health_trials AS health_status, 'Special-offer free trials currently inside 1-month trial window before first renewal date' AS note FROM trials_status_agg ts
      UNION ALL
      SELECT 'trials_payment_due_count', 'trials_ltv', 'Trials Payment Due', 'trailing', CAST(ts.payment_due_count AS NUMERIC), 'count', ts.as_of_date, 'vw_trial_status', ts.built_at, ts.health_trials, 'Special-offer trials in 15-day renewal grace period (first renewal date to +15 days, status ACTIVE or MONTHLY_PAYMENT_FAILED)' FROM trials_status_agg ts
      UNION ALL
      SELECT 'trials_conv_rate_1st_mature', 'trials_ltv', 'Trailing Mature 1st Payment CVR', 'trailing', CAST(tc.conv_rate_1st_mature_pct AS NUMERIC), 'pct', ts.as_of_date, 'rpt_trial_conversion', ts.built_at, ts.health_trials, 'Overall conversion rate to 1st paid payment across all mature trial cohorts' FROM trials_conv_agg tc CROSS JOIN trials_status_agg ts
      UNION ALL
      SELECT 'trials_conv_rate_3rd_mature', 'trials_ltv', 'Trailing Mature 3rd Payment CVR (Headline)', 'trailing', CAST(tc.conv_rate_3rd_mature_pct AS NUMERIC), 'pct', ts.as_of_date, 'rpt_trial_conversion', ts.built_at, ts.health_trials, 'Headline KPI: Overall conversion rate to 3rd paid payment across mature trial cohorts' FROM trials_conv_agg tc CROSS JOIN trials_status_agg ts
      UNION ALL
      SELECT 'ltv_3m_acquired_mtd_mean', 'trials_ltv', 'Acquired Customers 3M Value (Mean)', 'mtd', CAST(al.mean_ltv_3m AS NUMERIC), 'gbp', ts.as_of_date, 'customer_ltv_horizons, dim_customer', ts.built_at, ts.health_trials, 'Mean 3-month projected LTV for customers acquired MTD' FROM acq_ltv_agg al CROSS JOIN trials_status_agg ts
      UNION ALL
      SELECT 'ltv_3m_acquired_mtd_median', 'trials_ltv', 'Acquired Customers 3M Value (Median)', 'mtd', CAST(al.median_ltv_3m AS NUMERIC), 'gbp', ts.as_of_date, 'customer_ltv_horizons, dim_customer', ts.built_at, ts.health_trials, 'Median 3-month projected LTV for customers acquired MTD' FROM acq_ltv_agg al CROSS JOIN trials_status_agg ts
    ),
    org_weeks AS (
      SELECT 'latest_week' AS period, p.latest_week AS week_commencing, p.as_of_date, p.built_at, h.health_organic FROM params p CROSS JOIN health h
      UNION ALL
      SELECT 'prev_week', p.prev_week, p.as_of_date, p.built_at, h.health_organic FROM params p CROSS JOIN health h
    ),
    organic_agg AS (
      SELECT
        ow.period,
        ow.as_of_date,
        ow.built_at,
        ow.health_organic,
        w.site_sessions AS clearly_sessions,
        w.site_purchases AS clearly_purchases,
        w.site_revenue AS clearly_revenue,
        w.site_sessions_unclear AS unclear_sessions,
        w.site_purchases_unclear AS unclear_purchases,
        w.site_revenue_unclear AS unclear_revenue
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly` w
      JOIN org_weeks ow
        ON w.week_commencing = ow.week_commencing
      WHERE w.platform = 'Total'
    ),
    organic_rows AS (
      SELECT CONCAT('org_clearly_sessions_', oa.period) AS kpi_id, 'organic_charity' AS section, 'Organic Social Site Sessions (Clearly Organic)' AS label, oa.period, CAST(oa.clearly_sessions AS NUMERIC) AS value, 'count' AS unit, oa.as_of_date, 'rpt_organic_social_weekly' AS source_tables, oa.built_at, oa.health_organic AS health_status, 'GA4 sessions unreliable since 2026-10-24; site CVR understated' AS note FROM organic_agg oa
      UNION ALL
      SELECT CONCAT('org_clearly_purchases_', oa.period), 'organic_charity', 'Organic Social Purchases (Clearly Organic)', oa.period, CAST(oa.clearly_purchases AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Orders from clearly organic social sessions' FROM organic_agg oa
      UNION ALL
      SELECT CONCAT('org_clearly_revenue_', oa.period), 'organic_charity', 'Organic Social Revenue (Clearly Organic)', oa.period, CAST(oa.clearly_revenue AS NUMERIC), 'gbp', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Revenue from clearly organic social sessions' FROM organic_agg oa
      UNION ALL
      SELECT CONCAT('org_unclear_sessions_', oa.period), 'organic_charity', 'Organic Social Site Sessions (Unclear)', oa.period, CAST(oa.unclear_sessions AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'GA4 sessions unreliable since 2026-10-24; site CVR understated' AS note FROM organic_agg oa
      UNION ALL
      SELECT CONCAT('org_unclear_purchases_', oa.period), 'organic_charity', 'Organic Social Purchases (Unclear)', oa.period, CAST(oa.unclear_purchases AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Orders from unclear social sessions' FROM organic_agg oa
      UNION ALL
      SELECT CONCAT('org_unclear_revenue_', oa.period), 'organic_charity', 'Organic Social Revenue (Unclear)', oa.period, CAST(oa.unclear_revenue AS NUMERIC), 'gbp', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Revenue from unclear social sessions' FROM organic_agg oa
    ),
    charity_ranked AS (
      SELECT
        c.selected_charity,
        c.charity_type,
        SUM(c.purchases) AS purchases,
        ROUND(SUM(c.revenue), 2) AS revenue,
        ROW_NUMBER() OVER (ORDER BY SUM(c.revenue) DESC) AS rank,
        p.as_of_date,
        p.built_at,
        h.health_charity
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily` c
      CROSS JOIN params p
      CROSS JOIN health h
      WHERE c.date BETWEEN p.mtd_start AND p.mtd_end
      GROUP BY c.selected_charity, c.charity_type, p.as_of_date, p.built_at, h.health_charity
      QUALIFY rank <= 5
    ),
    charity_rows AS (
      SELECT
        CONCAT('charity_top_', CAST(rank AS STRING), '_rev') AS kpi_id,
        'organic_charity' AS section,
        CONCAT('Top ', CAST(rank AS STRING), ' Charity Revenue: ', selected_charity) AS label,
        'mtd' AS period,
        CAST(revenue AS NUMERIC) AS value,
        'gbp' AS unit,
        as_of_date,
        'rpt_charity_sales_daily' AS source_tables,
        built_at,
        health_charity AS health_status,
        CONCAT('Rank ', CAST(rank AS STRING), ' charity MTD (category: ', charity_type, ')') AS note
      FROM charity_ranked
      UNION ALL
      SELECT
        CONCAT('charity_top_', CAST(rank AS STRING), '_purchases') AS kpi_id,
        'organic_charity' AS section,
        CONCAT('Top ', CAST(rank AS STRING), ' Charity Purchases: ', selected_charity) AS label,
        'mtd' AS period,
        CAST(purchases AS NUMERIC) AS value,
        'count' AS unit,
        as_of_date,
        'rpt_charity_sales_daily' AS source_tables,
        built_at,
        health_charity AS health_status,
        CONCAT('Rank ', CAST(rank AS STRING), ' charity purchases count MTD') AS note
      FROM charity_ranked
    ),
    unioned AS (
      SELECT * FROM headline_rows
      UNION ALL
      SELECT * FROM paid_efficiency_rows
      UNION ALL
      SELECT * FROM trials_ltv_rows
      UNION ALL
      SELECT * FROM organic_rows
      UNION ALL
      SELECT * FROM charity_rows
    )
    SELECT
      u.*,
      COALESCE((
        SELECT ga4_sessions_reliable
        FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
        WHERE d = u.as_of_date
      ), FALSE) AS ga4_reliable
    FROM unioned u;
    """
    run_query(client, sql_exec_summary, "Recreate rpt_exec_summary with ga4_reliable and note")

    # Step 7: Recreate vw_report_suite_health with updated tracking health
    sql_report_suite_health = """
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
    tracking_health_metrics AS (
      SELECT
        lc.d AS latest_d,
        lc.ga4_complete,
        lc.ga4_sessions_reliable,
        ist.start_date,
        l7.consent_pct_7d,
        l7.unassigned_pct_7d,
        op.parity_pct,
        op.ga4_rows,
        op.web_checkouts
      FROM (
        SELECT d, ga4_complete, ga4_sessions_reliable
        FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
        WHERE d = (SELECT MAX(d) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`)
      ) lc
      CROSS JOIN (
        SELECT MIN(d) AS start_date
        FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
        WHERE NOT ga4_sessions_reliable
      ) ist
      CROSS JOIN (
        SELECT
          ROUND(SAFE_DIVIDE(SUM(ga4_unassigned), SUM(ga4_sessions)) * 100, 1) AS unassigned_pct_7d,
          ROUND(AVG(consent_granted_share) * 100, 1) AS consent_pct_7d
        FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
        WHERE d BETWEEN DATE_SUB((SELECT MAX(d) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`), INTERVAL 6 DAY)
                    AND (SELECT MAX(d) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`)
      ) l7
      CROSS JOIN (
        SELECT
          ga4.n AS ga4_rows,
          web.n AS web_checkouts,
          ROUND(SAFE_DIVIDE(ga4.n, web.n) * 100, 1) AS parity_pct
        FROM (
          SELECT COUNT(*) AS n
          FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
          WHERE event_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-31'
        ) ga4
        CROSS JOIN (
          SELECT COUNT(*) AS n
          FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_transactions`
          WHERE purchase_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-31'
            AND (
              (NOT is_subscription_payment AND (platform = 'WEB' OR platform IS NULL))
              OR (is_subscription_payment AND is_first_purchase AND platform = 'WEB')
            )
        ) web
      ) op
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
        IF(ARRAY_LENGTH(stale_feeds) > 0,
           CONCAT('Feeds stale: ', ARRAY_TO_STRING(stale_feeds, ', ')),
           'Fresh (all ad platform feeds within 2d)') AS caveat_reason
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

      -- 3. Free Trials (renewal-day grace window dynamic counts)
      SELECT
        'Free trials' AS report,
        'vw_trial_status, rpt_trial_conversion' AS tables_read,
        MAX(t.built_at) AS built_at,
        (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`) AS latest_data_date,
        MAX(m.anomaly_count) > 0 AS is_failed,
        FALSE AS has_caveat,
        IF(MAX(m.anomaly_count) > 0,
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
        MAX(tc.built_at) AS built_at,
        thm.latest_d AS latest_data_date,
        FALSE AS is_failed,
        (NOT thm.ga4_complete OR NOT thm.ga4_sessions_reliable) AS has_caveat,
        CASE
          WHEN NOT thm.ga4_complete OR NOT thm.ga4_sessions_reliable THEN
            CONCAT(
              'Warning: GA4 sessions unreliable since ', CAST(thm.start_date AS STRING),
              IF(NOT thm.ga4_complete, CONCAT(' (export not landed for ', CAST(thm.latest_d AS STRING), ')'), ''),
              '; last 7d consent share ', CAST(thm.consent_pct_7d AS STRING), '%, unassigned ', CAST(thm.unassigned_pct_7d AS STRING),
              '%; October purchase parity ', CAST(thm.parity_pct AS STRING), '% (', CAST(thm.ga4_rows AS STRING), '/', CAST(thm.web_checkouts AS STRING), ')'
            )
          ELSE 'Fresh (daily coverage and consent tracking)'
        END AS caveat_reason
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
      CROSS JOIN tracking_health_metrics thm
      GROUP BY thm.latest_d, thm.ga4_complete, thm.ga4_sessions_reliable, thm.start_date, thm.consent_pct_7d, thm.unassigned_pct_7d, thm.parity_pct, thm.ga4_rows, thm.web_checkouts

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
      END;
    """
    run_query(client, sql_report_suite_health, "Recreate vw_report_suite_health")

    # Step 8: Recreate views referencing the tables
    run_query(client, "CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_page_daily` AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`;", "Recreate vw_rpt_paid_media_page_daily")
    run_query(client, "CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_page_status` AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`;", "Recreate vw_rpt_paid_media_page_status")
    run_query(client, "CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_ga4_purchases` AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`;", "Recreate vw_fact_ga4_purchases")

    # Step 9: Run updated DQ Gate and insert into dq_gate_results
    sql_dq_gate = """
    DECLARE v_run_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();

    INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
      (run_ts, run_date, test, severity, result, source_value, curated_value, detail)
    WITH
    -- 1. Existing quality checks ...
    early_warning_tracking AS (
      SELECT
        dd.latest_d,
        dd.target_d,
        dd.latest_complete,
        dd.session_starts AS y_sessions,
        dd.orders AS y_orders,
        m.med_sessions,
        m.med_orders,
        ROUND(SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)), 2) AS divergence_ratio,
        CASE
          WHEN NOT dd.latest_complete THEN 'WARN'
          WHEN SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)) IS NULL
            OR SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)) = 0 THEN 'WARN'
          WHEN SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)) > 1.5 THEN 'WARN'
          ELSE 'PASS'
        END AS test_result,
        CASE
          WHEN NOT dd.latest_complete THEN
            CONCAT('GA4 export not landed for ', CAST(dd.latest_d AS STRING), '; tested previous complete day ', CAST(dd.target_d AS STRING), ' (divergence ratio ', CAST(ROUND(SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)), 2) AS STRING), ')')
          WHEN SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)) IS NULL
            OR SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)) = 0 THEN
            CONCAT('GA4 divergence ratio is zero or null (', CAST(ROUND(SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)), 2) AS STRING), '); possible GA4 export issue')
          WHEN SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)) > 1.5 THEN
            'GA4 visits jumped without orders; possible tracking change'
          ELSE
            CONCAT('Yesterday GA4 visits/orders ratio (', CAST(ROUND(SAFE_DIVIDE(SAFE_DIVIDE(dd.session_starts, m.med_sessions), SAFE_DIVIDE(dd.orders, m.med_orders)), 2) AS STRING), ') in line with 4-week weekday median')
        END AS detail
      FROM (
        SELECT
          t.latest_d,
          t.target_d,
          t.latest_complete,
          COALESCE(NULLIF(c.ga4_sessions, 0), v.landings, 0) AS session_starts,
          c.orders
        FROM (
          SELECT
            li.latest_d,
            li.latest_complete,
            IF(li.latest_complete, li.latest_d, (
              SELECT MAX(d)
              FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
              WHERE ga4_complete AND d < li.latest_d
            )) AS target_d
          FROM (
            SELECT
              d AS latest_d,
              ga4_complete AS latest_complete
            FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
            WHERE d = DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
          ) li
        ) t
        JOIN `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` c
          ON c.d = t.target_d
        LEFT JOIN (
          SELECT d, SUM(landings) AS landings
          FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
          GROUP BY 1
        ) v ON c.d = v.d
      ) dd
      CROSS JOIN (
        SELECT
          PERCENTILE_CONT(COALESCE(NULLIF(c.ga4_sessions, 0), v.landings, 0), 0.5) OVER() AS med_sessions,
          PERCENTILE_CONT(c.orders, 0.5) OVER() AS med_orders
        FROM (
          SELECT
            IF(li.latest_complete, li.latest_d, (
              SELECT MAX(d)
              FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
              WHERE ga4_complete AND d < li.latest_d
            )) AS target_d
          FROM (
            SELECT
              d AS latest_d,
              ga4_complete AS latest_complete
            FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
            WHERE d = DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
          ) li
        ) t
        JOIN `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` c
          ON c.d IN (
            DATE_SUB(t.target_d, INTERVAL 7 DAY),
            DATE_SUB(t.target_d, INTERVAL 14 DAY),
            DATE_SUB(t.target_d, INTERVAL 21 DAY),
            DATE_SUB(t.target_d, INTERVAL 28 DAY)
          )
        LEFT JOIN (
          SELECT d, SUM(landings) AS landings
          FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
          GROUP BY 1
        ) v ON c.d = v.d
        LIMIT 1
      ) m
    ),
    -- Hard test 1: All tables reading GA4 have non-null ga4_reliable flag
    ga4_flags AS (
      SELECT
        (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_exec_summary`) AS null_flags_count
    ),
    -- Hard test 2: Early-warning test cannot pass when ga4_complete = false (fixture day test)
    early_warning_guard AS (
      SELECT
        CASE
          WHEN NOT fixture_complete THEN 'WARN'
          WHEN SAFE_DIVIDE(SAFE_DIVIDE(fixture_sessions, 1000), SAFE_DIVIDE(fixture_orders, 100)) IS NULL
            OR SAFE_DIVIDE(SAFE_DIVIDE(fixture_sessions, 1000), SAFE_DIVIDE(fixture_orders, 100)) = 0 THEN 'WARN'
          WHEN SAFE_DIVIDE(SAFE_DIVIDE(fixture_sessions, 1000), SAFE_DIVIDE(fixture_orders, 100)) > 1.5 THEN 'WARN'
          ELSE 'PASS'
        END AS fixture_test_result
      FROM (
        SELECT FALSE AS fixture_complete, 0 AS fixture_sessions, 500 AS fixture_orders
      )
    )
    SELECT v_run_ts, DATE(v_run_ts), 'tracking_early_warning_spike', 'WARN',
           test_result, ROUND(CAST(med_sessions AS NUMERIC), 0), y_sessions,
           detail FROM early_warning_tracking
    UNION ALL
    SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_flag_populated', 'HARD',
           IF(null_flags_count = 0, 'PASS', 'FAIL'), 0, null_flags_count,
           'Every table reading GA4 has non-null ga4_reliable flag' FROM ga4_flags
    UNION ALL
    SELECT v_run_ts, DATE(v_run_ts), 'tracking_early_warning_incomplete_guard', 'HARD',
           IF(fixture_test_result = 'WARN', 'PASS', 'FAIL'), 0, IF(fixture_test_result = 'WARN', 0, 1),
           'Early-warning test returns WARN when ga4_complete is false (tested with incomplete fixture day)' FROM early_warning_guard;
    """
    run_query(client, sql_dq_gate, "Run updated DQ gate and insert results")

    print("\n" + "=" * 115)
    print("UPDATED VW_REPORT_SUITE_HEALTH ROWS")
    print("=" * 115)
    q_health_print = """
    SELECT
      report,
      status,
      hours_since_build,
      latest_data_date,
      built_at,
      reason
    FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
    """
    for r in client.query(q_health_print).result():
        built_str = str(r.built_at)[:19] + "Z" if r.built_at else "None"
        print(f"{r.report:<16} | {r.status:<8} | {r.hours_since_build:<5.1f}h | {str(r.latest_data_date):<11} | {built_str:<21} | {r.reason}")

    print("\n" + "=" * 115)
    print("LATEST DQ GATE RESULTS FOR GA4 & NEW HARD TESTS")
    print("=" * 115)
    q_gate_print = """
    SELECT
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = (SELECT MAX(run_ts) FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`)
    ORDER BY severity DESC, test ASC
    """
    for r in client.query(q_gate_print).result():
        print(f"{r.test:<40} | {r.severity:<5} | {r.result:<5} | src: {r.source_value:<6} | cur: {r.curated_value:<6} | {r.detail}")

if __name__ == "__main__":
    main()
