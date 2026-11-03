import os, sys

SQL_FILE = "sql/nightly_build_and_test.sql"

def main():
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # Normalize to \n
    content = content.replace("\r\n", "\n")

    # 1. Update entities and meta_camps CTE in rpt_paid_media_entity_daily
    old_entities = """-- 1. Entities
entities AS (
  SELECT entity_id, name, kind, parent_id, grp, wordseed, in_parent_total
  FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity`
),
meta_camps AS (
  SELECT entity_id, SUBSTR(entity_id, 2) AS cid, grp, name
  FROM entities
  WHERE kind = 'campaign'
),"""

    new_entities = """-- 1. Entities
entities AS (
  SELECT entity_id, name, kind, parent_id, grp, wordseed, in_parent_total, active_from, active_to
  FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity`
),
meta_camps AS (
  SELECT
    entity_id,
    SUBSTR(entity_id, 2) AS cid,
    grp,
    name,
    parent_id,
    active_from,
    active_to,
    COUNT(*) OVER (PARTITION BY LOWER(TRIM(name))) AS name_cnt
  FROM entities
  WHERE kind = 'campaign'
),"""

    old_entities = old_entities.replace("\r\n", "\n")
    new_entities = new_entities.replace("\r\n", "\n")

    if old_entities not in content:
        raise ValueError("Could not find old_entities in sql/nightly_build_and_test.sql!")
    content = content.replace(old_entities, new_entities, 1)
    print("Replaced old_entities successfully.")

    # 2. Update with_sub and with_entity_ids in rpt_paid_media_entity_daily
    old_with_sub = """with_sub AS (
  SELECT
    c.*,
    CASE
      WHEN c.utm_camp IN (
        '120251664492040306', '120251663454160306', '120251664356230306', '120251848542080306',
        '120251921753650306', '120252068484400306', '120251141172590306', '120251561496800306',
        '120251562168700306', '120251609587710306', '120251663948900306', '120251677235360306',
        'aug26_241', 'ugcaug26_241', 'sept_pr'
      ) OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\\bpr\\b|_pr|pr_')
        OR REGEXP_CONTAINS(COALESCE(c.tw_cid, ''), r'(?i)\\bpr\\b|_pr|pr_')
        THEN 'PR'
      WHEN c.utm_camp IN (
        '120251680751690306', '120249596570560306',
        'house_3_for_1', 'house_win_both', '2026_jul-aug_conversion_dorset_rmg'
      ) OR c.tw_cid = '120249596570560306'
        OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\\brmg\\b|_rmg|rmg_')
        THEN 'RMG'
      WHEN c.utm_camp IN (
        '120251664103950306', '120249596570570306',
        'rtgaug26_241', 'house_none'
      ) OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\\brtg\\b|_rtg|rtg_')
        THEN 'RTG'
      ELSE 'Unknown'
    END AS ps_sub,
    CASE
      WHEN c.gads_subchannel_override IS NOT NULL THEN c.gads_subchannel_override
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)brand') THEN 'Brand'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)competitor|omaze') THEN 'Competitor'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)demand gen|(\\b|_)(?i)dg($|\\b|_)') THEN 'Demand Gen'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)pmax|performance max') THEN 'PMAX'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)generic') THEN 'Generic'
      ELSE 'Unknown'
    END AS g_sub
  FROM classified_touches c
),
with_entity_ids AS (
  SELECT
    c.*,
    -- Entity IDs array matching assemble2.py
    CASE 
      WHEN c.channel = 'Paid Social' THEN
        ARRAY_CONCAT(
          ['ps',
           CASE
             WHEN leg.campaign_id IS NOT NULL THEN 'ps-unk'
             WHEN c.ps_sub IN ('PR', 'RMG', 'RTG') THEN CONCAT('ps-', LOWER(c.ps_sub))
             ELSE 'ps-unk'
           END],
          IF(mc.entity_id IS NOT NULL, [mc.entity_id], ARRAY<STRING>[])
        )
      WHEN c.channel = 'Google' THEN
        CASE c.g_sub
          WHEN 'Brand' THEN ['g-brand'] -- g-brand NOT in gb
          WHEN 'Competitor' THEN ['gb', 'g-comp']
          WHEN 'Generic' THEN ['gb', 'g-gen']
          WHEN 'Demand Gen' THEN ['gb', 'g-dg']
          WHEN 'PMAX' THEN ['gb', 'g-pmax']
          ELSE ['gb', 'g-unk']
        END
      WHEN c.channel = 'Bing' THEN
        CASE 
          WHEN REGEXP_CONTAINS(LOWER(IFNULL(c.utm_camp, '')), r'competitor|omaze') THEN ['gb', 'b-comp']
          WHEN REGEXP_CONTAINS(LOWER(IFNULL(c.utm_camp, '')), r'generic') THEN ['gb', 'b-gen']
          ELSE ['gb', 'b-unk']
        END
      WHEN c.channel = 'Affiliates' THEN ['aff']
      ELSE ARRAY<STRING>[]
    END AS entity_ids
  FROM with_sub c
  LEFT JOIN meta_camps mc ON c.extracted_meta_cid = mc.cid
  LEFT JOIN meta_legacy leg ON c.extracted_meta_cid = leg.campaign_id
  WHERE c.channel IN ('Paid Social', 'Google', 'Bing', 'Affiliates')
),"""

    new_with_sub = """with_sub AS (
  SELECT
    c.*,
    COALESCE(mc_id.entity_id, mc_name.entity_id) AS matched_meta_entity_id,
    COALESCE(mc_id.parent_id, mc_name.parent_id) AS matched_meta_parent_id,
    CASE
      WHEN c.utm_camp IN (
        '120251664492040306', '120251663454160306', '120251664356230306', '120251848542080306',
        '120251921753650306', '120252068484400306', '120251141172590306', '120251561496800306',
        '120251562168700306', '120251609587710306', '120251663948900306', '120251677235360306',
        'aug26_241', 'ugcaug26_241', 'sept_pr'
      ) OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\\bpr\\b|_pr|pr_')
        OR REGEXP_CONTAINS(COALESCE(c.tw_cid, ''), r'(?i)\\bpr\\b|_pr|pr_')
        THEN 'PR'
      WHEN c.utm_camp IN (
        '120251680751690306', '120249596570560306',
        'house_3_for_1', 'house_win_both', '2026_jul-aug_conversion_dorset_rmg'
      ) OR c.tw_cid = '120249596570560306'
        OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\\brmg\\b|_rmg|rmg_')
        THEN 'RMG'
      WHEN c.utm_camp IN (
        '120251664103950306', '120249596570570306',
        'rtgaug26_241', 'house_none'
      ) OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\\brtg\\b|_rtg|rtg_')
        THEN 'RTG'
      ELSE 'Unknown'
    END AS ps_sub,
    CASE
      WHEN c.gads_subchannel_override IS NOT NULL THEN c.gads_subchannel_override
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)brand') THEN 'Brand'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)competitor|omaze') THEN 'Competitor'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)demand gen|(\\b|_)(?i)dg($|\\b|_)') THEN 'Demand Gen'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)pmax|performance max') THEN 'PMAX'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)generic') THEN 'Generic'
      ELSE 'Unknown'
    END AS g_sub
  FROM classified_touches c
  LEFT JOIN meta_camps mc_id
    ON c.extracted_meta_cid = mc_id.cid
  LEFT JOIN meta_camps mc_name
    ON LOWER(TRIM(c.utm_camp)) = LOWER(TRIM(mc_name.name))
   AND mc_id.entity_id IS NULL
   AND (mc_name.name_cnt = 1 OR c.click_date BETWEEN mc_name.active_from AND mc_name.active_to)
),
with_entity_ids AS (
  SELECT
    c.*,
    -- Entity IDs array matching assemble2.py
    CASE 
      WHEN c.channel = 'Paid Social' THEN
        ARRAY_CONCAT(
          ['ps',
           CASE
             WHEN leg.campaign_id IS NOT NULL THEN 'ps-unk'
             WHEN c.matched_meta_parent_id IS NOT NULL THEN c.matched_meta_parent_id
             WHEN c.ps_sub IN ('PR', 'RMG', 'RTG') THEN CONCAT('ps-', LOWER(c.ps_sub))
             ELSE 'ps-unk'
           END],
          IF(c.matched_meta_entity_id IS NOT NULL, [c.matched_meta_entity_id], ARRAY<STRING>[])
        )
      WHEN c.channel = 'Google' THEN
        CASE c.g_sub
          WHEN 'Brand' THEN ['g-brand'] -- g-brand NOT in gb
          WHEN 'Competitor' THEN ['gb', 'g-comp']
          WHEN 'Generic' THEN ['gb', 'g-gen']
          WHEN 'Demand Gen' THEN ['gb', 'g-dg']
          WHEN 'PMAX' THEN ['gb', 'g-pmax']
          ELSE ['gb', 'g-unk']
        END
      WHEN c.channel = 'Bing' THEN
        CASE 
          WHEN REGEXP_CONTAINS(LOWER(IFNULL(c.utm_camp, '')), r'competitor|omaze') THEN ['gb', 'b-comp']
          WHEN REGEXP_CONTAINS(LOWER(IFNULL(c.utm_camp, '')), r'generic') THEN ['gb', 'b-gen']
          ELSE ['gb', 'b-unk']
        END
      WHEN c.channel = 'Affiliates' THEN ['aff']
      ELSE ARRAY<STRING>[]
    END AS entity_ids
  FROM with_sub c
  LEFT JOIN meta_legacy leg ON c.extracted_meta_cid = leg.campaign_id
  WHERE c.channel IN ('Paid Social', 'Google', 'Bing', 'Affiliates')
),"""

    old_with_sub = old_with_sub.replace("\r\n", "\n")
    new_with_sub = new_with_sub.replace("\r\n", "\n")

    if old_with_sub not in content:
        raise ValueError("Could not find old_with_sub in sql/nightly_build_and_test.sql!")
    content = content.replace(old_with_sub, new_with_sub, 1)
    print("Replaced old_with_sub successfully.")

    # 3. Add V4 DQ test CTEs into SECTION 21
    old_dq_cte_anchor = """    (SELECT GREATEST(
       DATE_DIFF(CURRENT_DATE('Europe/London'), meta_to, DAY),
       DATE_DIFF(CURRENT_DATE('Europe/London'), google_to, DAY)
     )
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`) AS freshness_max_days_behind
),"""

    new_dq_cte = """    (SELECT GREATEST(
       DATE_DIFF(CURRENT_DATE('Europe/London'), meta_to, DAY),
       DATE_DIFF(CURRENT_DATE('Europe/London'), google_to, DAY)
     )
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`) AS freshness_max_days_behind
),
meta_unk_quality AS (
  SELECT
    COUNTIF(unk_share_pct > 10.0) AS days_over_10pct,
    ROUND(MAX(unk_share_pct), 2) AS max_unk_share_pct,
    STRING_AGG(IF(unk_share_pct > 10.0, CONCAT(CAST(d AS STRING), ' (', basis, ': ', CAST(unk_share_pct AS STRING), '%)'), NULL), ', ') AS bad_days
  FROM (
    SELECT
      d,
      basis,
      ROUND(SAFE_DIVIDE(SUM(IF(entity_id = 'ps-unk', wh_rev, 0)), SUM(IF(entity_id = 'ps', wh_rev, 0))) * 100, 2) AS unk_share_pct
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
    WHERE d BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)
                AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
      AND basis IN ('F', 'L')
    GROUP BY 1, 2
    HAVING SUM(IF(entity_id = 'ps', wh_rev, 0)) > 0
  )
),
meta_unk_top_unmatched AS (
  SELECT
    COALESCE(
      STRING_AGG(CONCAT(IFNULL(utm_camp, '(no_utm_campaign)'), ' (£', CAST(rev AS STRING), ')'), ', ' ORDER BY rev DESC LIMIT 5),
      'None'
    ) AS top_unmatched
  FROM (
    SELECT
      NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '') AS utm_camp,
      ROUND(SUM(t.revenue_gbp), 2) AS rev
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
    JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
      ON t.transaction_id = touch.transaction_id
    WHERE touch.last_touch_timestamp IS NOT NULL
      AND DATE(touch.last_touch_timestamp, 'Europe/London') BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)
                                                               AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
      AND DATE_DIFF(DATE(t.purchase_at, 'Europe/London'), DATE(touch.last_touch_timestamp, 'Europe/London'), DAY) BETWEEN 0 AND 7
      AND REGEXP_CONTAINS(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'utm_source=(meta|fb|ig|facebook|instagram|meta_fb|meta_ig|an|th)|tw_source=(fb|ig)|fbclid')
      AND NOT EXISTS (
        SELECT 1 FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity` dim
        WHERE dim.kind = 'campaign'
          AND (
            SUBSTR(dim.entity_id, 2) = NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&](?:utm_campaign|tw_campaign)=([^&#]*)'), '')
            OR LOWER(TRIM(dim.name)) = LOWER(TRIM(NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '')))
          )
      )
      AND NOT (
        REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), ''), r'(?i)\\bpr\\b|_pr|pr_|\\brmg\\b|_rmg|rmg_|\\brtg\\b|_rtg|rtg_')
        OR REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]tw_campaign=([^&#]*)'), ''), r'(?i)\\bpr\\b|_pr|pr_|\\brmg\\b|_rmg|rmg_|\\brtg\\b|_rtg|rtg_')
      )
    GROUP BY 1
  )
),"""

    old_dq_cte_anchor = old_dq_cte_anchor.replace("\r\n", "\n")
    new_dq_cte = new_dq_cte.replace("\r\n", "\n")

    if old_dq_cte_anchor not in content:
        raise ValueError("Could not find old_dq_cte_anchor in sql/nightly_build_and_test.sql!")
    content = content.replace(old_dq_cte_anchor, new_dq_cte, 1)
    print("Replaced old_dq_cte_anchor successfully.")

    # 4. Add V4 DQ test to SELECT list of dq_gate_results
    old_dq_select_anchor = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_entity_freshness', 'WARN',
               IF(freshness_max_days_behind <= 2, 'PASS', 'WARN'), 2, freshness_max_days_behind,
               'meta_to and google_to are within 2 days of today' FROM paid_media_entity_quality"""

    new_dq_select = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_entity_freshness', 'WARN',
               IF(freshness_max_days_behind <= 2, 'PASS', 'WARN'), 2, freshness_max_days_behind,
               'meta_to and google_to are within 2 days of today' FROM paid_media_entity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_unknown_revenue_share', 'WARN',
               IF(days_over_10pct = 0, 'PASS', 'WARN'), 10.0, max_unk_share_pct,
               IF(days_over_10pct = 0,
                  CONCAT('ps-unk share <= 10% for all of last 7 days (max ', CAST(max_unk_share_pct AS STRING), '%). Top unmatched: ', top_unmatched),
                  CONCAT('Days in last 7 with ps-unk > 10%: ', bad_days, '. Top unmatched: ', top_unmatched)
               )
FROM meta_unk_quality CROSS JOIN meta_unk_top_unmatched"""

    old_dq_select_anchor = old_dq_select_anchor.replace("\r\n", "\n")
    new_dq_select = new_dq_select.replace("\r\n", "\n")

    if old_dq_select_anchor not in content:
        raise ValueError("Could not find old_dq_select_anchor in sql/nightly_build_and_test.sql!")
    content = content.replace(old_dq_select_anchor, new_dq_select, 1)
    print("Replaced old_dq_select_anchor successfully.")

    # Write updated file (preserving \n)
    with open(SQL_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print(f"Updated {SQL_FILE} successfully.")

if __name__ == "__main__":
    main()
