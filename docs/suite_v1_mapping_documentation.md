# Suite V1: Mapping Touches to Paid Media Entities

## Current Build SQL in `sql/nightly_build_and_test.sql`

Before this fix, touch mapping to paid media entities was defined in `rpt_paid_media_entity_daily` (SECTION 17) via the following CTEs:

```sql
-- 1. Entities
entities AS (
  SELECT entity_id, name, kind, parent_id, grp, wordseed, in_parent_total
  FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity`
),
meta_camps AS (
  SELECT entity_id, SUBSTR(entity_id, 2) AS cid, grp, name
  FROM entities
  WHERE kind = 'campaign'
),
meta_legacy AS (
  SELECT campaign_id, campaign_name
  FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_meta_legacy_campaign_map`
),

-- ... classification of touches ...

with_sub AS (
  SELECT
    c.*,
    CASE
      WHEN c.utm_camp IN (
        '120251664492040306', '120251663454160306', '120251664356230306', '120251848542080306',
        '120251921753650306', '120252068484400306', '120251141172590306', '120251561496800306',
        '120251562168700306', '120251609587710306', '120251663948900306', '120251677235360306',
        'aug26_241', 'ugcaug26_241', 'sept_pr'
      ) OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\bpr\b|_pr|pr_')
        OR REGEXP_CONTAINS(COALESCE(c.tw_cid, ''), r'(?i)\bpr\b|_pr|pr_')
        THEN 'PR'
      WHEN c.utm_camp IN (
        '120251680751690306', '120249596570560306',
        'house_3_for_1', 'house_win_both', '2026_jul-aug_conversion_dorset_rmg'
      ) OR c.tw_cid = '120249596570560306'
        OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\brmg\b|_rmg|rmg_')
        THEN 'RMG'
      WHEN c.utm_camp IN (
        '120251664103950306', '120249596570570306',
        'rtgaug26_241', 'house_none'
      ) OR REGEXP_CONTAINS(COALESCE(c.utm_camp, ''), r'(?i)\brtg\b|_rtg|rtg_')
        THEN 'RTG'
      ELSE 'Unknown'
    END AS ps_sub,
    CASE
      WHEN c.gads_subchannel_override IS NOT NULL THEN c.gads_subchannel_override
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)brand') THEN 'Brand'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)competitor|omaze') THEN 'Competitor'
      WHEN REGEXP_CONTAINS(COALESCE(c.extracted_google_camp_name, ''), r'(?i)demand gen|(\b|_)(?i)dg($|\b|_)') THEN 'Demand Gen'
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
)
```

## Problem Identified
`extracted_meta_cid` matched only the numeric ID pattern (`^1202[0-9]{10,20}$`). When marketing migrated UTM links in mid-October to campaign names (e.g. `2026_Oct_London-Cornwall_241_Urgency_EndsToday`), `extracted_meta_cid` returned NULL. Because the name had no `PR`, `RMG`, or `RTG` substrings, the group fallback returned `Unknown`, and the revenue was assigned to `['ps', 'ps-unk']` with no campaign entity ID.

