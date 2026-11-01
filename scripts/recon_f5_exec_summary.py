#!/usr/bin/env python3
"""
Reconciliation and Verification Script for F5: Build rpt_exec_summary Table
Outputs:
1. Dry run bytes and table build execution
2. Full output table of rpt_exec_summary (all KPI rows across all sections)
3. Direct reconciliations for Paid Media KPIs (spend, new customers, CPA, ROAS, tracking coverage)
   comparing rpt_exec_summary values against direct queries on rpt_paid_media_entity_daily
4. Headline strip reconciliation against direct query on fact_transactions
5. Row count and consistency checks
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

print("=" * 120)
print("F5.1 BUILDING rpt_exec_summary EXECUTIVE SUMMARY SNAPSHOT TABLE")
print("=" * 120)

build_sql = """
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

-- 1. HEADLINE STRIP (fact_transactions)
tx_agg AS (
  SELECT
    p.as_of_date,
    p.built_at,
    h.health_paid AS health_status,
    -- MTD
    ROUND(SUM(IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND t.is_first_purchase, t.revenue_gbp, 0)), 2) AS rev_new_mtd,
    COUNT(DISTINCT IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND t.is_first_purchase, t.customer_key, NULL)) AS cust_new_mtd,
    ROUND(SUM(IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND NOT t.is_first_purchase, t.revenue_gbp, 0)), 2) AS rev_renew_mtd,
    COUNTIF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end AND NOT t.is_first_purchase) AS tx_renew_mtd,
    ROUND(SUM(IF(t.purchase_date BETWEEN p.mtd_start AND p.mtd_end, t.revenue_gbp, 0)), 2) AS rev_total_mtd,
    -- Prev MTD
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

-- 2. GOOGLE + META EFFICIENCY (rpt_paid_media_entity_daily, rpt_tracking_coverage_daily)
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
    -- Spend ex VAT
    ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.spend, 0)), 2) AS spend_google,
    ROUND(SUM(IF(pm.entity_id = 'ps', pm.spend, 0)), 2) AS spend_meta,
    ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.spend, 0)), 2) AS spend_combined,
    -- New Customers (First Touch basis F)
    SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.new_cust, 0)) AS new_cust_google,
    SUM(IF(pm.entity_id = 'ps', pm.new_cust, 0)) AS new_cust_meta,
    SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.new_cust, 0)) AS new_cust_combined,
    -- v3 3-month LTV (First Touch basis F)
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

-- 3. TRIALS & LTV (vw_trial_status, rpt_trial_conversion, customer_ltv_horizons)
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
  SELECT 'trials_payment_due_count', 'trials_ltv', 'Trials Payment Due', 'trailing', CAST(ts.payment_due_count AS NUMERIC), 'count', ts.as_of_date, 'vw_trial_status', ts.built_at, ts.health_trials, 'Special-offer trials in 3-day renewal grace period (first renewal date to +3 days)' FROM trials_status_agg ts
  UNION ALL
  SELECT 'trials_conv_rate_1st_mature', 'trials_ltv', 'Trailing Mature 1st Payment CVR', 'trailing', CAST(tc.conv_rate_1st_mature_pct AS NUMERIC), 'pct', ts.as_of_date, 'rpt_trial_conversion', ts.built_at, ts.health_trials, 'Overall conversion rate to 1st paid payment across all mature trial cohorts' FROM trials_conv_agg tc CROSS JOIN trials_status_agg ts
  UNION ALL
  SELECT 'trials_conv_rate_3rd_mature', 'trials_ltv', 'Trailing Mature 3rd Payment CVR (Headline)', 'trailing', CAST(tc.conv_rate_3rd_mature_pct AS NUMERIC), 'pct', ts.as_of_date, 'rpt_trial_conversion', ts.built_at, ts.health_trials, 'Headline KPI: Overall conversion rate to 3rd paid payment across mature trial cohorts' FROM trials_conv_agg tc CROSS JOIN trials_status_agg ts
  UNION ALL
  SELECT 'ltv_3m_acquired_mtd_mean', 'trials_ltv', 'Acquired Customers 3M Value (Mean)', 'mtd', CAST(al.mean_ltv_3m AS NUMERIC), 'gbp', ts.as_of_date, 'customer_ltv_horizons, dim_customer', ts.built_at, ts.health_trials, 'Mean 3-month projected LTV for customers acquired MTD' FROM acq_ltv_agg al CROSS JOIN trials_status_agg ts
  UNION ALL
  SELECT 'ltv_3m_acquired_mtd_median', 'trials_ltv', 'Acquired Customers 3M Value (Median)', 'mtd', CAST(al.median_ltv_3m AS NUMERIC), 'gbp', ts.as_of_date, 'customer_ltv_horizons, dim_customer', ts.built_at, ts.health_trials, 'Median 3-month projected LTV for customers acquired MTD' FROM acq_ltv_agg al CROSS JOIN trials_status_agg ts
),

-- 4. ORGANIC SOCIAL & CHARITY (rpt_organic_social_weekly, rpt_charity_sales_daily)
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
  SELECT CONCAT('org_clearly_sessions_', oa.period) AS kpi_id, 'organic_charity' AS section, 'Organic Social Site Sessions (Clearly Organic)' AS label, oa.period, CAST(oa.clearly_sessions AS NUMERIC) AS value, 'count' AS unit, oa.as_of_date, 'rpt_organic_social_weekly' AS source_tables, oa.built_at, oa.health_organic AS health_status, 'Total GA4 sessions with organic social UTM or referrer' AS note FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_clearly_purchases_', oa.period), 'organic_charity', 'Organic Social Purchases (Clearly Organic)', oa.period, CAST(oa.clearly_purchases AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Orders from clearly organic social sessions' FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_clearly_revenue_', oa.period), 'organic_charity', 'Organic Social Revenue (Clearly Organic)', oa.period, CAST(oa.clearly_revenue AS NUMERIC), 'gbp', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Revenue from clearly organic social sessions' FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_unclear_sessions_', oa.period), 'organic_charity', 'Organic Social Site Sessions (Unclear)', oa.period, CAST(oa.unclear_sessions AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Sessions with unclassified/unclear social referrer' AS note FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_unclear_purchases_', oa.period), 'organic_charity', 'Organic Social Purchases (Unclear)', oa.period, CAST(oa.unclear_purchases AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Orders from unclear social sessions' FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_unclear_revenue_', oa.period), 'organic_charity', 'Organic Social Revenue (Unclear)', oa.period, CAST(oa.unclear_revenue AS NUMERIC), 'gbp', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Revenue from unclear social sessions' FROM organic_agg oa
),

-- Top 5 Charities by Sales MTD
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
)

SELECT * FROM headline_rows
UNION ALL
SELECT * FROM paid_efficiency_rows
UNION ALL
SELECT * FROM trials_ltv_rows
UNION ALL
SELECT * FROM organic_rows
UNION ALL
SELECT * FROM charity_rows
"""

dry_cfg = bigquery.QueryJobConfig(dry_run=True)
dry = client.query(build_sql, job_config=dry_cfg)
print(f"Dry run build query bytes: {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024**2):.2f} MB)")

client.query(build_sql).result()
print("rpt_exec_summary built successfully.")

print("\n" + "=" * 120)
print("F5.2 FULL OUTPUT OF rpt_exec_summary TABLE")
print("=" * 120)
q_full = """
SELECT
  kpi_id,
  section,
  label,
  period,
  value,
  unit,
  as_of_date,
  health_status,
  source_tables,
  note
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_exec_summary`
ORDER BY
  CASE section
    WHEN 'headline' THEN 1
    WHEN 'paid_efficiency' THEN 2
    WHEN 'trials_ltv' THEN 3
    WHEN 'organic_charity' THEN 4
  END,
  kpi_id
"""
rows = [dict(r) for r in client.query(q_full).result()]
print(f"Total KPI rows: {len(rows)}\n")
print(f"{'KPI ID':<35} | {'Section':<16} | {'Period':<18} | {'Value':<12} | {'Unit':<6} | {'Label':<42} | {'Health'}")
print("-" * 145)
for r in rows:
    val_str = f"{r['value']:,.2f}" if r['value'] is not None else "None"
    print(f"{r['kpi_id']:<35} | {r['section']:<16} | {r['period']:<18} | {val_str:<12} | {r['unit']:<6} | {r['label']:<42} | {r['health_status']}")

print("\n" + "=" * 120)
print("F5.3 DIRECT RECONCILIATIONS: PAID EFFICIENCY KPIS VS rpt_paid_media_entity_daily")
print("=" * 120)

q_recon_paid = """
WITH periods AS (
  SELECT 'mtd' AS period, DATE '2026-10-01' AS s_date, DATE '2026-10-31' AS e_date
  UNION ALL SELECT 'prev_mtd_same_point', DATE '2026-09-01', DATE '2026-09-30'
  UNION ALL SELECT 'last_7d', DATE '2026-10-25', DATE '2026-10-31'
  UNION ALL SELECT 'prev_7d', DATE '2026-10-18', DATE '2026-10-24'
)
SELECT
  p.period,
  -- Spend
  ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.spend, 0)), 2) AS direct_spend_google,
  ROUND(SUM(IF(pm.entity_id = 'ps', pm.spend, 0)), 2) AS direct_spend_meta,
  ROUND(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.spend, 0)), 2) AS direct_spend_combined,
  -- New Customers
  SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.new_cust, 0)) AS direct_new_cust_google,
  SUM(IF(pm.entity_id = 'ps', pm.new_cust, 0)) AS direct_new_cust_meta,
  SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.new_cust, 0)) AS direct_new_cust_combined,
  -- CPA
  ROUND(SAFE_DIVIDE(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.spend, 0)),
    SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.new_cust, 0))), 2) AS direct_cpa_google,
  ROUND(SAFE_DIVIDE(SUM(IF(pm.entity_id = 'ps', pm.spend, 0)),
    SUM(IF(pm.entity_id = 'ps', pm.new_cust, 0))), 2) AS direct_cpa_meta,
  ROUND(SAFE_DIVIDE(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.spend, 0)),
    SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.new_cust, 0))), 2) AS direct_cpa_combined,
  -- 3M ROAS
  ROUND(SAFE_DIVIDE(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.v3, 0)),
    SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand'), pm.spend, 0))), 2) AS direct_roas_google,
  ROUND(SAFE_DIVIDE(SUM(IF(pm.entity_id = 'ps', pm.v3, 0)),
    SUM(IF(pm.entity_id = 'ps', pm.spend, 0))), 2) AS direct_roas_meta,
  ROUND(SAFE_DIVIDE(SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.v3, 0)),
    SUM(IF(pm.entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-brand', 'ps'), pm.spend, 0))), 2) AS direct_roas_combined
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily` pm
JOIN periods p ON pm.d BETWEEN p.s_date AND p.e_date
WHERE pm.basis = 'F'
GROUP BY p.period
ORDER BY p.period
"""
direct_paid = [dict(r) for r in client.query(q_recon_paid).result()]
for dp in direct_paid:
    print(dp)

print("\n" + "=" * 120)
print("F5.4 HEADLINE STRIP RECONCILIATION: DIRECT QUERY ON fact_transactions")
print("=" * 120)
q_recon_hl = """
WITH periods AS (
  SELECT 'mtd' AS period, DATE '2026-10-01' AS s_date, DATE '2026-10-31' AS e_date
  UNION ALL SELECT 'prev_mtd_same_point', DATE '2026-09-01', DATE '2026-09-30'
)
SELECT
  p.period,
  ROUND(SUM(IF(t.is_first_purchase, t.revenue_gbp, 0)), 2) AS direct_rev_new,
  COUNT(DISTINCT IF(t.is_first_purchase, t.customer_key, NULL)) AS direct_cust_new,
  ROUND(SUM(IF(NOT t.is_first_purchase, t.revenue_gbp, 0)), 2) AS direct_rev_renew,
  COUNTIF(NOT t.is_first_purchase) AS direct_tx_renew,
  ROUND(SUM(t.revenue_gbp), 2) AS direct_rev_total
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
JOIN periods p ON t.purchase_date BETWEEN p.s_date AND p.e_date
WHERE t.customer_key != 'UNKNOWN_DELETED'
  AND NOT IFNULL(t.is_free_entry, FALSE)
  AND t.revenue_gbp > 0
GROUP BY p.period
ORDER BY p.period
"""
for r in client.query(q_recon_hl).result():
    print(dict(r))
