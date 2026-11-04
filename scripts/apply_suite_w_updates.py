import os, sys

SQL_FILE = "sql/nightly_build_and_test.sql"

def main():
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace("\r\n", "\n")

    # 1. Add stg_meta_segment_daily DDL in SECTION 7 / 8c
    old_ddl_anchor = """CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
(
  spend_date                      DATE,
  account_id                      STRING,
  campaign_id                     STRING,
  campaign_name                   STRING,
  campaign_objective              STRING,
  adset_id                        STRING,
  ad_id                           STRING,
  device                          STRING,
  spend_gbp                       NUMERIC,
  impressions                     INT64,
  reach                           INT64,
  clicks                          INT64,
  link_clicks                     INT64,
  platform_conversions            NUMERIC,
  platform_conversion_value_gbp   NUMERIC,
  source_system                   STRING,   -- 'meta_transfer' or 'meta_csv'
  loaded_at                       TIMESTAMP
)
PARTITION BY spend_date
OPTIONS (description = 'Landing slot for Meta Ads daily data (DTS Facebook Ads transfer or weekly Ads Manager CSV). Empty until Meta is connected.');"""

    new_ddl = old_ddl_anchor + """\n
-- 8c.1b Meta audience segments table (user_segment_key: prospecting, engaged, existing, unknown, null)
CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
(
  spend_date                      DATE,
  campaign_id                     STRING,
  campaign_name                   STRING,
  adset_id                        STRING,
  adset_name                      STRING,
  user_segment_key                STRING,
  spend_gbp                       NUMERIC,
  impressions                     INT64,
  link_clicks                     INT64,
  purchases                       NUMERIC,
  purchase_value_gbp              NUMERIC,
  loaded_at                       TIMESTAMP
)
PARTITION BY spend_date
CLUSTER BY campaign_id, user_segment_key
OPTIONS (description = 'Meta Ads daily ad set level audience segments (prospecting, engaged, existing, unknown, null).');"""

    old_ddl_anchor = old_ddl_anchor.replace("\r\n", "\n")
    new_ddl = new_ddl.replace("\r\n", "\n")

    if old_ddl_anchor not in content:
        raise ValueError("Could not find old_ddl_anchor in sql/nightly_build_and_test.sql!")
    content = content.replace(old_ddl_anchor, new_ddl, 1)
    print("Added stg_meta_segment_daily DDL successfully.")

    # 2. Add meta_segment_quality CTE in SECTION 21
    old_dq_anchor = """tagging_exc AS (
  SELECT
    COALESCE(SUM(purchasing_visitors), 0) AS purchasing_visitors,"""

    new_dq_cte = """meta_segment_quality AS (
  SELECT
    COUNTIF(
      rpt_spend > 0 AND (
        ABS(seg_spend - rpt_spend) > 1.0 AND SAFE_DIVIDE(ABS(seg_spend - rpt_spend), rpt_spend) > 0.005
      )
    ) AS days_with_mismatch,
    MAX(IF(rpt_spend > 0, ABS(seg_spend - rpt_spend), 0)) AS max_diff_gbp,
    STRING_AGG(
      IF(rpt_spend > 0 AND ABS(seg_spend - rpt_spend) > 1.0 AND SAFE_DIVIDE(ABS(seg_spend - rpt_spend), rpt_spend) > 0.005,
         CONCAT(CAST(d AS STRING), ' (seg: £', CAST(ROUND(seg_spend, 2) AS STRING), ', rpt: £', CAST(ROUND(rpt_spend, 2) AS STRING), ', diff: £', CAST(ROUND(seg_spend - rpt_spend, 2) AS STRING), ')'),
         NULL),
      '; '
    ) AS mismatch_days,
    COUNTIF(null_spend > 10.0) AS null_segment_bad_days,
    MAX(null_spend) AS max_null_spend,
    STRING_AGG(
      IF(null_spend > 10.0,
         CONCAT(CAST(d AS STRING), ' (£', CAST(ROUND(null_spend, 2) AS STRING), ')'),
         NULL),
      '; '
    ) AS bad_null_days
  FROM (
    SELECT
      days.d,
      COALESCE(s.spend, 0) AS seg_spend,
      COALESCE(r.spend, 0) AS rpt_spend,
      COALESCE(s.null_spend, 0) AS null_spend
    FROM (
      SELECT d
      FROM UNNEST(GENERATE_DATE_ARRAY(
        DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY),
        DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
      )) d
    ) days
    LEFT JOIN (
      SELECT
        spend_date AS d,
        ROUND(SUM(spend_gbp), 2) AS spend,
        ROUND(SUM(IF(user_segment_key = 'null', spend_gbp, 0)), 2) AS null_spend
      FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
      WHERE spend_date BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)
                           AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
      GROUP BY 1
    ) s ON days.d = s.d
    LEFT JOIN (
      SELECT
        d,
        ROUND(SUM(spend), 2) AS spend
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
      WHERE d BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)
                  AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
        AND entity_id = 'ps'
        AND basis = 'L'
      GROUP BY 1
    ) r ON days.d = r.d
  )
),
tagging_exc AS (
  SELECT
    COALESCE(SUM(purchasing_visitors), 0) AS purchasing_visitors,"""

    old_dq_anchor = old_dq_anchor.replace("\r\n", "\n")
    new_dq_cte = new_dq_cte.replace("\r\n", "\n")

    if old_dq_anchor not in content:
        raise ValueError("Could not find old_dq_anchor in sql/nightly_build_and_test.sql!")
    content = content.replace(old_dq_anchor, new_dq_cte, 1)
    print("Added meta_segment_quality CTE successfully.")

    # 3. Add W5 DQ tests to SELECT list of dq_gate_results
    old_dq_select_anchor = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_unknown_revenue_share', 'WARN',
               IF(days_over_10pct = 0, 'PASS', 'WARN'), 10.0, max_unk_share_pct,
               IF(days_over_10pct = 0,
                  CONCAT('ps-unk share <= 10% for all of last 7 days (max ', CAST(max_unk_share_pct AS STRING), '%). Top unmatched: ', top_unmatched),
                  CONCAT('Days in last 7 with ps-unk > 10%: ', bad_days, '. Top unmatched: ', top_unmatched)
               )
FROM meta_unk_quality CROSS JOIN meta_unk_top_unmatched"""

    new_dq_select = old_dq_select_anchor + """
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_segment_spend_parity', 'WARN',
               IF(days_with_mismatch = 0, 'PASS', 'WARN'), 1.0, ROUND(max_diff_gbp, 2),
               IF(days_with_mismatch = 0,
                  CONCAT('Segment spend matches rpt_paid_media_entity_daily within £1 and 0.5% for all days in last 7 (max diff: £', CAST(ROUND(max_diff_gbp, 2) AS STRING), ')'),
                  CONCAT('Days in last 7 with segment spend diff > £1 or 0.5%: ', mismatch_days)
               )
FROM meta_segment_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_segment_null_spend', 'WARN',
               IF(null_segment_bad_days = 0, 'PASS', 'WARN'), 10.0, ROUND(max_null_spend, 2),
               IF(null_segment_bad_days = 0,
                  CONCAT('Zero days in last 7 with spend on null segment > £10 (max null spend: £', CAST(ROUND(max_null_spend, 2) AS STRING), ')'),
                  CONCAT('Days in last 7 with spend on null segment > £10: ', bad_null_days)
               )
FROM meta_segment_quality"""

    old_dq_select_anchor = old_dq_select_anchor.replace("\r\n", "\n")
    new_dq_select = new_dq_select.replace("\r\n", "\n")

    if old_dq_select_anchor not in content:
        raise ValueError("Could not find old_dq_select_anchor in sql/nightly_build_and_test.sql!")
    content = content.replace(old_dq_select_anchor, new_dq_select, 1)
    print("Added W5 DQ tests to SELECT list successfully.")

    # Write updated file (preserving \n)
    with open(SQL_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print(f"Updated {SQL_FILE} successfully.")

if __name__ == "__main__":
    main()
