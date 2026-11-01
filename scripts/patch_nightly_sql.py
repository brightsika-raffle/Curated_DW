#!/usr/bin/env python3
"""
scripts/patch_nightly_sql.py

Applies the Section 3 changes into sql/nightly_build_and_test.sql.
"""

import re

SQL_PATH = "sql/nightly_build_and_test.sql"

def main():
    with open(SQL_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. fact_ga4_purchases
    p1_old = """CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
(
  event_date          DATE,
  user_pseudo_id      STRING,
  user_id             STRING,
  first_purchase_ts   TIMESTAMP,
  ga_session_id       INT64,
  session_source      STRING,
  session_medium      STRING,
  session_campaign    STRING,
  device_category     STRING,
  bt_events_that_day  INT64,
  loaded_at           TIMESTAMP
)"""
    p1_new = """CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
(
  event_date          DATE,
  user_pseudo_id      STRING,
  user_id             STRING,
  first_purchase_ts   TIMESTAMP,
  ga_session_id       INT64,
  session_source      STRING,
  session_medium      STRING,
  session_campaign    STRING,
  device_category     STRING,
  bt_events_that_day  INT64,
  ga4_reliable        BOOLEAN,
  loaded_at           TIMESTAMP
)"""
    assert p1_old in content, "p1_old not found"
    content = content.replace(p1_old, p1_new, 1)

    p1_insert_old = """INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
  (event_date, user_pseudo_id, user_id, first_purchase_ts, ga_session_id,
   session_source, session_medium, session_campaign, device_category,
   bt_events_that_day, loaded_at)"""
    p1_insert_new = """INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
  (event_date, user_pseudo_id, user_id, first_purchase_ts, ga_session_id,
   session_source, session_medium, session_campaign, device_category,
   bt_events_that_day, ga4_reliable, loaded_at)"""
    assert p1_insert_old in content, "p1_insert_old not found"
    content = content.replace(p1_insert_old, p1_insert_new, 1)

    p1_select_old = """SELECT
  event_date,
  user_pseudo_id,
  NULLIF(TRIM(COALESCE(user_id, max_user_id)), '') AS user_id,
  event_ts AS first_purchase_ts,
  ga_session_id,
  session_source,
  session_medium,
  session_campaign,
  device_category,
  bt_events_that_day,
  CURRENT_TIMESTAMP() AS loaded_at
FROM raw
WHERE rn = 1;"""
    p1_select_new = """SELECT
  event_date,
  user_pseudo_id,
  NULLIF(TRIM(COALESCE(user_id, max_user_id)), '') AS user_id,
  event_ts AS first_purchase_ts,
  ga_session_id,
  session_source,
  session_medium,
  session_campaign,
  device_category,
  bt_events_that_day,
  event_date < DATE '2026-10-24' AS ga4_reliable,
  CURRENT_TIMESTAMP() AS loaded_at
FROM raw
WHERE rn = 1;"""
    assert p1_select_old in content, "p1_select_old not found"
    content = content.replace(p1_select_old, p1_select_new, 1)

    # 2. rpt_paid_media_page_daily
    p2_old = """SELECT
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
  DATE_DIFF(CURRENT_DATE('Europe/London'), COALESCE(s.d, w.d), DAY) < 7 AS maturing
FROM spend_both_bases s
FULL OUTER JOIN wh_agg w
  ON s.d = w.d
 AND s.basis = w.basis
 AND s.channel = w.channel
 AND s.sub = w.sub;"""
    p2_new = """SELECT
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
  ON COALESCE(s.d, w.d) = tc.d;"""
    assert p2_old in content, "p2_old not found"
    content = content.replace(p2_old, p2_new, 1)

    # 3. rpt_paid_media_page_status
    p3_old = """  (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE channel_group = 'Affiliates') AS affiliates_to;"""
    p3_new = """  (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE channel_group = 'Affiliates') AS affiliates_to,
  COALESCE((
    SELECT ga4_sessions_reliable
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
    WHERE d = DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  ), FALSE) AS ga4_reliable;"""
    assert p3_old in content, "p3_old not found"
    content = content.replace(p3_old, p3_new, 1)

    # 4. rpt_visit_source_daily
    p4_insert_old = """INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
  (d, consent, channel, source, medium, landings, page_views, ga4_complete, built_at)"""
    p4_insert_new = """INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
  (d, consent, channel, source, medium, landings, page_views, ga4_complete, ga4_reliable, built_at)"""
    assert p4_insert_old in content, "p4_insert_old not found"
    content = content.replace(p4_insert_old, p4_insert_new, 1)

    p4_select_old = """  COALESCE(l.landings, 0) AS landings,
  COALESCE(p.page_views, 0) AS page_views,
  TRUE AS ga4_complete,
  CURRENT_TIMESTAMP() AS built_at
FROM landings_agg l"""
    p4_select_new = """  COALESCE(l.landings, 0) AS landings,
  COALESCE(p.page_views, 0) AS page_views,
  TRUE AS ga4_complete,
  COALESCE(l.d, p.d) < DATE '2026-10-24' AS ga4_reliable,
  CURRENT_TIMESTAMP() AS built_at
FROM landings_agg l"""
    assert p4_select_old in content, "p4_select_old not found"
    content = content.replace(p4_select_old, p4_select_new, 1)

    # 5. rpt_organic_social_weekly
    p5_old = """  FROM platform_rollup
  GROUP BY 1
)
SELECT * FROM platform_rollup
UNION ALL
SELECT * FROM total_rollup;"""
    p5_new = """  FROM platform_rollup
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
FROM unioned u;"""
    assert p5_old in content, "p5_old not found"
    content = content.replace(p5_old, p5_new, 1)

    # 6. DQ Gate: early_warning_tracking CTE + new hard tests CTEs
    p6_cte_old = """early_warning_tracking AS (
  SELECT
    y.session_starts AS y_sessions,
    y.orders AS y_orders,
    m.med_sessions,
    m.med_orders,
    ROUND(SAFE_DIVIDE(y.session_starts, m.med_sessions), 2) AS session_ratio,
    ROUND(SAFE_DIVIDE(y.orders, m.med_orders), 2) AS order_ratio,
    ROUND(SAFE_DIVIDE(SAFE_DIVIDE(y.session_starts, m.med_sessions), SAFE_DIVIDE(y.orders, m.med_orders)), 2) AS divergence_ratio,
    IF(SAFE_DIVIDE(SAFE_DIVIDE(y.session_starts, m.med_sessions), SAFE_DIVIDE(y.orders, m.med_orders)) > 1.5, 'WARN', 'PASS') AS test_result
  FROM (
    SELECT
      COALESCE(NULLIF(c.ga4_sessions, 0), v.landings, 0) AS session_starts,
      c.orders
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` c
    LEFT JOIN (
      SELECT d, SUM(landings) AS landings
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
      GROUP BY 1
    ) v ON c.d = v.d
    WHERE c.d = DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  ) y
  CROSS JOIN (
    SELECT
      PERCENTILE_CONT(COALESCE(NULLIF(c.ga4_sessions, 0), v.landings, 0), 0.5) OVER() AS med_sessions,
      PERCENTILE_CONT(c.orders, 0.5) OVER() AS med_orders
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` c
    LEFT JOIN (
      SELECT d, SUM(landings) AS landings
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
      GROUP BY 1
    ) v ON c.d = v.d
    WHERE c.d IN (
      DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 8 DAY),
      DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 15 DAY),
      DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 22 DAY),
      DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 29 DAY)
    )
    LIMIT 1
  ) m
),"""

    p6_cte_new = """early_warning_tracking AS (
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
ga4_flags AS (
  SELECT
    (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`)
    + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`)
    + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`)
    + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`)
    + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`)
    + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_exec_summary`) AS null_flags_count
),
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
),"""
    assert p6_cte_old in content, "p6_cte_old not found"
    content = content.replace(p6_cte_old, p6_cte_new, 1)

    p6_union_old = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tracking_early_warning_spike', 'WARN',
               test_result, ROUND(CAST(med_sessions AS NUMERIC), 0), y_sessions,
               IF(test_result = 'WARN',
                  'GA4 visits jumped without orders; possible tracking change',
                  CONCAT('Yesterday GA4 visits/orders ratio (', CAST(divergence_ratio AS STRING), ') in line with 4-week weekday median'))
FROM early_warning_tracking"""

    p6_union_new = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tracking_early_warning_spike', 'WARN',
               test_result, ROUND(CAST(med_sessions AS NUMERIC), 0), y_sessions,
               detail FROM early_warning_tracking
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_flag_populated', 'HARD',
               IF(null_flags_count = 0, 'PASS', 'FAIL'), 0, null_flags_count,
               'Every table reading GA4 has non-null ga4_reliable flag' FROM ga4_flags
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tracking_early_warning_incomplete_guard', 'HARD',
               IF(fixture_test_result = 'WARN', 'PASS', 'FAIL'), 0, IF(fixture_test_result = 'WARN', 0, 1),
               'Early-warning test returns WARN when ga4_complete is false (tested with incomplete fixture day)' FROM early_warning_guard"""
    assert p6_union_old in content, "p6_union_old not found"
    content = content.replace(p6_union_old, p6_union_new, 1)

    # 7. vw_report_suite_health
    p7_health_old = """  -- 5. Tracking Health (GA4 daily export lag up to 2 days ok)
  SELECT
    'Tracking health' AS report,
    'rpt_tracking_coverage_daily' AS tables_read,
    MAX(built_at) AS built_at,
    MAX(d) AS latest_data_date,
    FALSE AS is_failed,
    FALSE AS has_caveat,
    'Fresh (daily coverage and consent tracking)' AS caveat_reason
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`"""

    # We also want to check whether trial_mature_test has the tracking_health_metrics CTE added
    p7_cte_anchor = """trial_mature_test AS (
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
),"""

    p7_cte_replacement = """trial_mature_test AS (
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
),"""
    assert p7_cte_anchor in content, "p7_cte_anchor not found"
    content = content.replace(p7_cte_anchor, p7_cte_replacement, 1)

    p7_health_new = """  -- 5. Tracking Health (GA4 daily export lag up to 2 days ok)
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
  GROUP BY thm.latest_d, thm.ga4_complete, thm.ga4_sessions_reliable, thm.start_date, thm.consent_pct_7d, thm.unassigned_pct_7d, thm.parity_pct, thm.ga4_rows, thm.web_checkouts"""
    assert p7_health_old in content, "p7_health_old not found"
    content = content.replace(p7_health_old, p7_health_new, 1)

    # 8. rpt_exec_summary notes & ga4_reliable column
    p8_org_old = """organic_rows AS (
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
)"""

    p8_org_new = """organic_rows AS (
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
)"""
    assert p8_org_old in content, "p8_org_old not found"
    content = content.replace(p8_org_old, p8_org_new, 1)

    p8_select_old = """    CONCAT('Rank ', CAST(rank AS STRING), ' charity purchases count MTD') AS note
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
SELECT * FROM charity_rows;"""

    p8_select_new = """    CONCAT('Rank ', CAST(rank AS STRING), ' charity purchases count MTD') AS note
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
FROM unioned u;"""
    assert p8_select_old in content, "p8_select_old not found"
    content = content.replace(p8_select_old, p8_select_new, 1)

    with open(SQL_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    print("Successfully patched sql/nightly_build_and_test.sql with all Section 3 updates.")

if __name__ == "__main__":
    main()
