import os, sys

def main():
    with open('sql/nightly_build_and_test.sql', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Replace the views with rpt_meta_audience_compare_daily table
    target_views = """CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_order_audience_label`     AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_ga4_prior_visit_daily`       AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_meta_audience_compare_daily`
OPTIONS (description = 'Daily Meta audience split: warehouse (Meta last touch, paid, first payment only) against Meta user_segment_key. Two buckets because engaged cannot be matched like for like yet. Totals differ: Meta counts 7-day click and view-through.')
AS
WITH w AS (
  SELECT purchase_date AS d, IF(audience_label = 'existing', 'existing', 'not_yet_customer') AS bucket,
    COUNTIF(audience_label = 'engaged') AS wh_engaged_part, COUNT(*) AS wh_orders, SUM(revenue_gbp) AS wh_revenue_gbp
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`
  WHERE channel_group = 'Meta' AND audience_label != 'free' AND NOT is_renewal
  GROUP BY 1, 2),
m AS (
  SELECT spend_date AS d, IF(user_segment_key = 'existing', 'existing', 'not_yet_customer') AS bucket,
    SUM(IF(user_segment_key = 'engaged', purchases, 0)) AS meta_engaged_part, SUM(purchases) AS meta_purchases, SUM(spend_gbp) AS meta_spend_gbp
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
  WHERE user_segment_key IN ('existing', 'engaged', 'prospecting')
  GROUP BY 1, 2)
SELECT d AS report_date, bucket, wh_orders, wh_engaged_part, wh_revenue_gbp, meta_purchases, meta_engaged_part, meta_spend_gbp
FROM w FULL JOIN m USING (d, bucket);"""

    repl_table = """CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_meta_audience_compare_daily`
OPTIONS (description = 'Daily Meta audience split: warehouse (Meta last touch, paid, first payment only) against Meta user_segment_key. Two buckets because engaged cannot be matched like for like yet. Rebuilt nightly.', labels = [('layer', 'report'), ('owner', 'bright_sika')])
AS
WITH w AS (
  SELECT purchase_date AS d, IF(audience_label = 'existing', 'existing', 'not_yet_customer') AS bucket,
    COUNTIF(audience_label = 'engaged') AS wh_engaged_part, COUNT(*) AS wh_orders, SUM(revenue_gbp) AS wh_revenue_gbp
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`
  WHERE channel_group = 'Meta' AND audience_label != 'free' AND NOT is_renewal
  GROUP BY 1, 2),
m AS (
  SELECT spend_date AS d, IF(user_segment_key = 'existing', 'existing', 'not_yet_customer') AS bucket,
    SUM(IF(user_segment_key = 'engaged', purchases, 0)) AS meta_engaged_part, SUM(purchases) AS meta_purchases, SUM(spend_gbp) AS meta_spend_gbp
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
  WHERE user_segment_key IN ('existing', 'engaged', 'prospecting')
  GROUP BY 1, 2)
SELECT d AS report_date, bucket, wh_orders, wh_engaged_part, wh_revenue_gbp, meta_purchases, meta_engaged_part, meta_spend_gbp
FROM w FULL JOIN m USING (d, bucket);"""

    assert target_views in content, "target_views not found in sql/nightly_build_and_test.sql"
    content = content.replace(target_views, repl_table, 1)

    # 2. Add meta_segment_days to fresh CTE
    target_fresh = """    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'google'), DAY) AS g_days,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'meta'),   DAY) AS m_days
),"""

    repl_fresh = """    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'google'), DAY) AS g_days,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'meta'),   DAY) AS m_days,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`), DAY) AS meta_segment_days
),"""

    assert target_fresh in content, "target_fresh not found in sql/nightly_build_and_test.sql"
    content = content.replace(target_fresh, repl_fresh, 1)

    # 3. Add fresh_meta_segment test
    target_test = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_meta_spend',   'WARN', IF(m_days   <= 3, 'PASS', 'WARN'), 3, m_days,   'Days since latest Meta spend day (manual upload)' FROM fresh"""

    repl_test = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_meta_spend',   'WARN', IF(m_days   <= 3, 'PASS', 'WARN'), 3, m_days,   'Days since latest Meta spend day (manual upload)' FROM fresh
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_meta_segment', 'HARD', IF(meta_segment_days <= 2, 'PASS', 'FAIL'), 2, meta_segment_days, 'Days since latest Meta segment spend day in stg_meta_segment_daily (limit 2)' FROM fresh"""

    assert target_test in content, "target_test not found in sql/nightly_build_and_test.sql"
    content = content.replace(target_test, repl_test, 1)

    with open('sql/nightly_build_and_test.sql', 'w', encoding='utf-8') as f:
        f.write(content)

    print("Successfully patched sql/nightly_build_and_test.sql for AX2.")

if __name__ == '__main__':
    main()

