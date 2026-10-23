-- =====================================================================
-- RAFFLE HOUSE — NIGHTLY BUILD + DAILY TESTS   (scheduled query, one job)
-- Project rh-ga4-491819 · europe-west2 · owner Bright Sika · v1 28 Sept 2026
--
-- WHAT IT DOES, IN ORDER
--   1. Rebuilds every table in rh_curated_Bright_Test (full rebuild).
--   2. Re-creates the Power BI views (free — no bytes scanned).
--   3. Runs the reconciliation tests and APPENDS the results to
--      dq_gate_results, so every night leaves a dated PASS/FAIL record.
--   4. ASSERT: if any HARD test failed, the job FAILS — which triggers the
--      scheduled-query failure email. A quiet inbox means it passed.
--
-- WRITES ONLY to rh_curated_Bright_Test. Reads rh_warehouse and
-- google_ads_transfer. Never touches anything existing.
--
-- CONSISTENT SNAPSHOT: the tests read the source tables FOR SYSTEM_TIME AS
-- OF the moment each curated table was built (its loaded_at), so a source
-- refresh landing mid-run can never cause a false FAIL.
--
-- COST: ~1.4 GB build + ~0.5 GB tests ≈ 2 GB/night ≈ 60 GB/month ≈ $0.01
-- a night at on-demand list price — normally inside the free 1 TB/month.
--
-- SCHEDULE: every day 04:10 UTC (05:10 UK summer / 04:10 UK winter), after
-- the live rh_warehouse chain finishes at 03:15. Deliberately not on the hour.
-- =====================================================================

DECLARE v_run_ts   TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
DECLARE v_tx_ts    TIMESTAMP;
DECLARE v_sub_ts   TIMESTAMP;
DECLARE v_spend_ts TIMESTAMP;
DECLARE v_stat_ts  TIMESTAMP;
DECLARE v_ga4_ts   TIMESTAMP;

-- =====================================================================
-- SECTION 2 — dim_date  (2018–2028, UK financial year from 1 April)
-- Mark as date table in Power BI.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.dim_date`
CLUSTER BY date_key
OPTIONS (description = 'Calendar 2018-2028. UK financial year starts 1 April. Full rebuild nightly.')
AS
SELECT
  d                                                  AS date_key,
  EXTRACT(YEAR    FROM d)                            AS year,
  EXTRACT(QUARTER FROM d)                            AS quarter,
  EXTRACT(MONTH   FROM d)                            AS month_number,
  FORMAT_DATE('%B', d)                               AS month_name,
  FORMAT_DATE('%b', d)                               AS month_short,
  FORMAT_DATE('%Y-%m', d)                            AS year_month,
  DATE_TRUNC(d, MONTH)                               AS month_start,
  DATE_TRUNC(d, WEEK(MONDAY))                        AS week_start_monday,
  MOD(EXTRACT(DAYOFWEEK FROM d) + 5, 7) + 1          AS day_of_week,   -- 1=Mon..7=Sun
  FORMAT_DATE('%A', d)                               AS day_name,
  EXTRACT(DAYOFWEEK FROM d) IN (1,7)                 AS is_weekend,
  IF(EXTRACT(MONTH FROM d) >= 4, EXTRACT(YEAR FROM d), EXTRACT(YEAR FROM d) - 1)
                                                     AS fin_year_start,
  CONCAT('FY',
    CAST(IF(EXTRACT(MONTH FROM d) >= 4, EXTRACT(YEAR FROM d), EXTRACT(YEAR FROM d) - 1) AS STRING),
    '-',
    FORMAT('%02d', MOD(IF(EXTRACT(MONTH FROM d) >= 4, EXTRACT(YEAR FROM d) + 1, EXTRACT(YEAR FROM d)), 100)))
                                                     AS fin_year,
  DIV(MOD(EXTRACT(MONTH FROM d) + 8, 12), 3) + 1     AS fin_quarter,   -- Apr-Jun = 1
  CURRENT_TIMESTAMP()                                AS loaded_at
FROM UNNEST(GENERATE_DATE_ARRAY('2018-01-01','2028-12-31')) AS d;


-- =====================================================================
-- SECTION 3 — dim_customer  (no name, no raw email)
-- + UNKNOWN_DELETED surrogate for customers hard-deleted upstream.
-- dim_customer_pii is DEFERRED: raw PII needs its own restricted dataset.
-- =====================================================================

-- ACQUISITION ATTRIBUTES (added 28 Sept from the unit-economics playbook):
-- LTV curves have to be cut by how a customer was acquired — channel,
-- device/platform, geography, entry product. Those are fixed at the first
-- ACCEPTED order, so they belong on the customer, computed once, not
-- re-derived in every report. email_sha256 (lower+trim, SHA-256) is also
-- the exact normalisation Google Enhanced Conversions / Meta CAPI expect.
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.dim_customer`
CLUSTER BY customer_key
OPTIONS (description = 'One row per customer plus UNKNOWN_DELETED. Email hashed; name/surname not carried. acq_ columns fixed at first ACCEPTED order. Full rebuild nightly.')
AS
WITH first_order AS (
  SELECT
    customer_id,
    COALESCE(purchase_date, created_at)              AS acq_first_purchase_at,
    COALESCE(platform, 'Unknown')                    AS acq_platform,  -- NULL on ~82% of historic revenue
    raffle_id                                        AS acq_raffle_key,
    order_type                                       AS acq_order_type,
    subscription_id IS NOT NULL                      AS acq_via_subscription,
    CAST(total_cost AS NUMERIC) / 100                AS acq_first_order_gbp,
    COALESCE(attribution_first_touch, 'Unattributed') AS acq_channel_first_touch
  FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
  WHERE payment_status = 'ACCEPTED' AND customer_id IS NOT NULL
    AND COALESCE(purchase_date, created_at) IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY COALESCE(purchase_date, created_at), transaction_id) = 1
)
SELECT
  c.customer_id                                      AS customer_key,
  TO_HEX(SHA256(LOWER(TRIM(c.email))))               AS email_sha256,
  LOWER(SPLIT(c.email, '@')[SAFE_OFFSET(1)])         AS email_domain,
  c.country,
  -- country is 61% NULL and mixes names with ISO codes (UK 153k + GB 20k,
  -- USA/US, Ireland/IE). Geography cuts are only meaningful as UK / non-UK.
  CASE WHEN c.country IS NULL OR TRIM(c.country) = '' THEN 'Unknown'
       WHEN UPPER(TRIM(c.country)) IN ('UK','GB','GBR','UNITED KINGDOM') THEN 'UK'
       ELSE 'Non-UK' END                             AS country_group,
  c.created_at,
  DATE(c.created_at)                                 AS created_date,
  FORMAT_TIMESTAMP('%Y-%m', c.created_at)            AS signup_year_month,
  c.updated_at,
  c.is_verified,
  c.is_blocked,
  c.is_social_registration,
  c.referral_key,
  c.successful_referral_count,
  CAST(c.referral_credits AS NUMERIC)                AS referral_credits,
  c.klaviyo_id,
  c.gclid,
  c.fbclid,
  c.landing_page,
  f.acq_first_purchase_at,
  DATE(f.acq_first_purchase_at)                      AS acq_first_purchase_date,
  FORMAT_TIMESTAMP('%Y-%m', f.acq_first_purchase_at) AS acq_cohort_month,
  f.acq_platform,
  f.acq_raffle_key,
  f.acq_order_type,
  f.acq_via_subscription,
  f.acq_first_order_gbp,
  f.acq_channel_first_touch,
  f.customer_id IS NOT NULL                          AS has_purchased,
  FALSE                                              AS is_unknown_member,
  CURRENT_TIMESTAMP()                                AS loaded_at
FROM `rh-ga4-491819.rh_warehouse.dim_customers` c
LEFT JOIN first_order f ON f.customer_id = c.customer_id
UNION ALL
SELECT
  'UNKNOWN_DELETED', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
  'Unknown',
  CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE), CAST(NULL AS STRING), CAST(NULL AS TIMESTAMP),
  CAST(NULL AS BOOL), CAST(NULL AS BOOL), CAST(NULL AS BOOL), CAST(NULL AS STRING),
  CAST(NULL AS INT64), CAST(NULL AS NUMERIC), CAST(NULL AS STRING), CAST(NULL AS STRING),
  CAST(NULL AS STRING), CAST(NULL AS STRING),
  -- acq_ columns
  CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE), CAST(NULL AS STRING), CAST(NULL AS STRING),
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS BOOL), CAST(NULL AS NUMERIC),
  CAST(NULL AS STRING), TRUE,
  TRUE, CURRENT_TIMESTAMP();
-- acq_ types: platform/raffle_id/order_type are assumed STRING. If the UNION
-- errors on one, change only that CAST to the source type.
-- If this UNION errors on a type mismatch (e.g. successful_referral_count is
-- not INT64 in source), change ONLY that one CAST to the source type.


-- =====================================================================
-- SECTION 4 — dim_raffle  (~30 rows)
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle`
OPTIONS (description = 'One row per property raffle. Full rebuild nightly.')
AS
SELECT
  raffle_id                                          AS raffle_key,
  property_heading,
  property_location,
  CAST(ticket_price AS NUMERIC)                      AS ticket_price,
  default_tickets,
  start_at,
  ends_at,
  created_at,
  DATE(start_at)                                     AS start_date,
  DATE(ends_at)                                      AS end_date,
  DATE_DIFF(DATE(ends_at), DATE(start_at), DAY)      AS duration_days,
  is_closed,
  is_active,
  FALSE                                              AS is_unknown_member,
  CURRENT_TIMESTAMP()                                AS loaded_at
FROM `rh-ga4-491819.rh_warehouse.dim_raffles`
UNION ALL
SELECT 'UNKNOWN', 'Unknown raffle', CAST(NULL AS STRING), CAST(NULL AS NUMERIC),
       CAST(NULL AS INT64), CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP),
       CAST(NULL AS TIMESTAMP), CAST(NULL AS DATE), CAST(NULL AS DATE),
       CAST(NULL AS INT64), CAST(NULL AS BOOL), CAST(NULL AS BOOL),
       TRUE, CURRENT_TIMESTAMP();


-- =====================================================================
-- SECTION 5 — dim_subscription_model  (~9 rows)
-- `label` is the tier name. monthly_price_gbp is unit-safe: if the source
-- holds pence (max > 100) it is divided by 100, and the unit used is
-- recorded in price_unit_detected so nobody has to guess.
-- tier_band thresholds are PROVISIONAL (<£20 Low / <£40 Mid / <£60 High /
-- above Very High) — awaiting Jordan's confirmation. 4 of 9 models have a
-- blank or NULL label, so tier_band (not label) is what Power BI should show.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model`
OPTIONS (description = 'One row per subscription tier. tier_band is PROVISIONAL pending Jordan. Full rebuild nightly.')
AS
WITH unit AS (
  SELECT IF(MAX(total_cost) > 100, 100, 1) AS divisor
  FROM `rh-ga4-491819.rh_warehouse.dim_subscription_models`
),
m AS (
  SELECT s.*, CAST(s.total_cost AS NUMERIC) / u.divisor AS price_gbp, u.divisor
  FROM `rh-ga4-491819.rh_warehouse.dim_subscription_models` s CROSS JOIN unit u
)
SELECT
  subscription_model_id                              AS subscription_model_key,
  label,
  price_gbp                                          AS monthly_price_gbp,
  IF(divisor = 100, 'pence', 'pounds')               AS price_unit_detected,
  num_of_tickets,
  bonus_tickets,
  is_bonus_active,
  is_active,
  created_at,
  -- Live prices checked 28 Sept: £10, £15, £25, £30, £30, £30, £50, £50, £75
  -- (source is PENCE). Bands set so £10/£15 = Low (matches the trial logic,
  -- which treats the £15 tier as Low). PROVISIONAL until Jordan confirms.
  CASE WHEN price_gbp < 20 THEN 'Low'        -- £10, £15
       WHEN price_gbp < 40 THEN 'Mid'        -- £25, £30
       WHEN price_gbp < 60 THEN 'High'       -- £50
       ELSE 'Very High' END                          AS tier_band,  -- £75
  FALSE                                              AS is_unknown_member,
  CURRENT_TIMESTAMP()                                AS loaded_at
FROM m
UNION ALL
SELECT 'UNKNOWN', 'Unknown tier', CAST(NULL AS NUMERIC), CAST(NULL AS STRING),
       CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS BOOL), CAST(NULL AS BOOL),
       CAST(NULL AS TIMESTAMP), 'Unknown', TRUE, CURRENT_TIMESTAMP();
-- If the UNKNOWN row errors on a type (e.g. bonus_tickets not INT64), change
-- only that CAST to match the source type.


-- =====================================================================
-- SECTION 6 — dim_channel_first_touch / dim_channel_last_touch   (v3, 28 Sept)
-- attribution_*_touch holds the LANDING URL, not a channel name (checked
-- live: ~15-16k distinct URLs). Grain stays one row per URL — that is the key
-- the fact carries — and channel_group is DERIVED from click IDs and UTMs so
-- Power BI slices on ~11 groups, not 15,000 URLs. Physically duplicated so
-- both can sit on one visual.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_first_touch`
OPTIONS (description = 'First-touch attribution. Grain = one landing URL (the key the fact carries). channel_group is derived from the URL: click IDs and UTM parameters. Capture began 27 Aug 2026; older orders are Unattributed by design.')
AS
WITH u AS (
  SELECT DISTINCT attribution_first_touch AS url
  FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
  WHERE payment_status = 'ACCEPTED' AND attribution_first_touch IS NOT NULL
),
p AS (
  SELECT url, LOWER(url) AS l,
    LOWER(REGEXP_EXTRACT(url, r'[?&]utm_source=([^&#]+)'))   AS utm_source,
    LOWER(REGEXP_EXTRACT(url, r'[?&]utm_medium=([^&#]+)'))   AS utm_medium,
    REGEXP_EXTRACT(url, r'[?&]utm_campaign=([^&#]+)')        AS utm_campaign,
    LOWER(REGEXP_EXTRACT(url, r'^https?://([^/?#]+)'))       AS landing_host,
    REGEXP_EXTRACT(url, r'^https?://[^/?#]+(/[^?#]*)')       AS landing_path
  FROM u
),
c AS (
  SELECT p.*,
    CASE   -- order matters: explicit UTM intent first, then platform click IDs
      WHEN REGEXP_CONTAINS(IFNULL(utm_source,''), r'mentionme') THEN 'Referral (Mention Me)'
      WHEN utm_source = 'affiliate' OR REGEXP_CONTAINS(IFNULL(utm_medium,''), r'affiliate') OR REGEXP_CONTAINS(l, r'[?&]clickref=') THEN 'Affiliate'
      WHEN utm_source = 'klaviyo' OR utm_medium = 'email' THEN 'Email'
      WHEN utm_medium = 'sms' OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'attentive|sms') THEN 'SMS'
      WHEN REGEXP_CONTAINS(l, r'[?&](gclid|gbraid|wbraid|gad_source|gad_campaignid)=')
        OR (utm_source = 'google' AND REGEXP_CONTAINS(IFNULL(utm_medium,''), r'cpc|paid|ppc')) THEN 'Google Ads'
      WHEN REGEXP_CONTAINS(l, r'[?&]fbclid=') OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'^(facebook|fb|ig|instagram|meta)') THEN 'Meta'
      WHEN REGEXP_CONTAINS(l, r'[?&]msclkid=') OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'bing|microsoft') THEN 'Microsoft Ads'
      WHEN REGEXP_CONTAINS(l, r'[?&]ttclid=') OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'tiktok') THEN 'TikTok'
      WHEN REGEXP_CONTAINS(l, r'^https?://wowcher\.') THEN 'Wowcher'
      WHEN utm_source IS NOT NULL THEN 'Other tagged'
      ELSE 'Direct / untagged'
    END AS channel_group
  FROM p
)
SELECT
  TO_HEX(SHA256(url))                                   AS channel_key,     -- matches fact_transactions.channel_first_touch_key
  url                                                   AS landing_url,
  channel_group,
  CASE channel_group WHEN 'Google Ads' THEN 'google' WHEN 'Meta' THEN 'meta' ELSE NULL END AS ad_platform,  -- joins to spend scope
  channel_group IN ('Google Ads','Meta')                AS is_google_meta,
  utm_source, utm_medium, utm_campaign, landing_host, landing_path,
  TRUE                                                  AS is_attributed,
  CURRENT_TIMESTAMP()                                   AS loaded_at
FROM c
UNION ALL
SELECT TO_HEX(SHA256('Unattributed')), CAST(NULL AS STRING), 'Unattributed', CAST(NULL AS STRING), FALSE,
       CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
       FALSE, CURRENT_TIMESTAMP();

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch`
OPTIONS (description = 'Last-touch attribution. Grain = one landing URL (the key the fact carries). channel_group is derived from the URL: click IDs and UTM parameters. Capture began 27 Aug 2026; older orders are Unattributed by design.')
AS
WITH u AS (
  SELECT DISTINCT attribution_last_touch AS url
  FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
  WHERE payment_status = 'ACCEPTED' AND attribution_last_touch IS NOT NULL
),
p AS (
  SELECT url, LOWER(url) AS l,
    LOWER(REGEXP_EXTRACT(url, r'[?&]utm_source=([^&#]+)'))   AS utm_source,
    LOWER(REGEXP_EXTRACT(url, r'[?&]utm_medium=([^&#]+)'))   AS utm_medium,
    REGEXP_EXTRACT(url, r'[?&]utm_campaign=([^&#]+)')        AS utm_campaign,
    LOWER(REGEXP_EXTRACT(url, r'^https?://([^/?#]+)'))       AS landing_host,
    REGEXP_EXTRACT(url, r'^https?://[^/?#]+(/[^?#]*)')       AS landing_path
  FROM u
),
c AS (
  SELECT p.*,
    CASE   -- order matters: explicit UTM intent first, then platform click IDs
      WHEN REGEXP_CONTAINS(IFNULL(utm_source,''), r'mentionme') THEN 'Referral (Mention Me)'
      WHEN utm_source = 'affiliate' OR REGEXP_CONTAINS(IFNULL(utm_medium,''), r'affiliate') OR REGEXP_CONTAINS(l, r'[?&]clickref=') THEN 'Affiliate'
      WHEN utm_source = 'klaviyo' OR utm_medium = 'email' THEN 'Email'
      WHEN utm_medium = 'sms' OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'attentive|sms') THEN 'SMS'
      WHEN REGEXP_CONTAINS(l, r'[?&](gclid|gbraid|wbraid|gad_source|gad_campaignid)=')
        OR (utm_source = 'google' AND REGEXP_CONTAINS(IFNULL(utm_medium,''), r'cpc|paid|ppc')) THEN 'Google Ads'
      WHEN REGEXP_CONTAINS(l, r'[?&]fbclid=') OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'^(facebook|fb|ig|instagram|meta)') THEN 'Meta'
      WHEN REGEXP_CONTAINS(l, r'[?&]msclkid=') OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'bing|microsoft') THEN 'Microsoft Ads'
      WHEN REGEXP_CONTAINS(l, r'[?&]ttclid=') OR REGEXP_CONTAINS(IFNULL(utm_source,''), r'tiktok') THEN 'TikTok'
      WHEN REGEXP_CONTAINS(l, r'^https?://wowcher\.') THEN 'Wowcher'
      WHEN utm_source IS NOT NULL THEN 'Other tagged'
      ELSE 'Direct / untagged'
    END AS channel_group
  FROM p
)
SELECT
  TO_HEX(SHA256(url))                                   AS channel_key,     -- matches fact_transactions.channel_last_touch_key
  url                                                   AS landing_url,
  channel_group,
  CASE channel_group WHEN 'Google Ads' THEN 'google' WHEN 'Meta' THEN 'meta' ELSE NULL END AS ad_platform,  -- joins to spend scope
  channel_group IN ('Google Ads','Meta')                AS is_google_meta,
  utm_source, utm_medium, utm_campaign, landing_host, landing_path,
  TRUE                                                  AS is_attributed,
  CURRENT_TIMESTAMP()                                   AS loaded_at
FROM c
UNION ALL
SELECT TO_HEX(SHA256('Unattributed')), CAST(NULL AS STRING), 'Unattributed', CAST(NULL AS STRING), FALSE,
       CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
       FALSE, CURRENT_TIMESTAMP();


-- =====================================================================
-- SECTION 7 — fact_transactions  (ACCEPTED only, one pass)
-- total_cost is FLOAT64 PENCE → NUMERIC pounds here, once.
-- No partition expiry, ever: this is the business record.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
PARTITION BY purchase_date
CLUSTER BY customer_key, raffle_key
OPTIONS (description = 'One row per ACCEPTED transaction. revenue_gbp in pounds. No partition expiry by design. Full rebuild nightly.')
AS
WITH base AS (
  SELECT
    t.transaction_id,
    DATE(COALESCE(t.purchase_date, t.created_at))                  AS purchase_date,
    COALESCE(t.purchase_date, t.created_at)                        AS purchase_at,
    IF(t.purchase_date IS NULL, 'created_at', 'purchase_date')     AS purchase_date_source,  -- 144,746 rows use created_at
    IF(c.customer_id IS NULL, 'UNKNOWN_DELETED', t.customer_id)    AS customer_key,
    IF(r.raffle_id IS NULL, 'UNKNOWN', t.raffle_id)                AS raffle_key,
    t.subscription_id                                              AS subscription_key,
    t.competition_id,
    TO_HEX(SHA256(COALESCE(t.attribution_first_touch,'Unattributed'))) AS channel_first_touch_key,
    TO_HEX(SHA256(COALESCE(t.attribution_last_touch ,'Unattributed'))) AS channel_last_touch_key,
    CAST(t.total_cost AS NUMERIC) / 100                            AS revenue_gbp,
    t.ticket_count                                                 AS tickets,
    CAST(t.spent_credits AS NUMERIC)                               AS spent_credits,
    t.order_type,
    t.platform,
    t.card_type,
    t.payment_initiated_by,
    t.selected_charity,
    t.checkout_id,
    t.is_archive,
    t.total_cost = 0                                               AS is_free_entry,
    t.subscription_id IS NOT NULL                                  AS is_subscription_payment,
    EXTRACT(HOUR FROM COALESCE(t.purchase_date, t.created_at))     AS hour_of_day,
    DATE_DIFF(DATE(COALESCE(t.purchase_date, t.created_at)), DATE(c.created_at), DAY) AS customer_age_days_at_order
  FROM `rh-ga4-491819.rh_warehouse.fact_transactions` t
  LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_customers` c ON t.customer_id = c.customer_id
  LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_raffles`   r ON t.raffle_id   = r.raffle_id
  WHERE t.payment_status = 'ACCEPTED'
    AND COALESCE(t.purchase_date, t.created_at) IS NOT NULL
)
SELECT
  *,
  ROW_NUMBER() OVER (PARTITION BY customer_key ORDER BY purchase_at, transaction_id)
                                                                   AS customer_order_sequence,
  ROW_NUMBER() OVER (PARTITION BY customer_key ORDER BY purchase_at, transaction_id) = 1
                                                                   AS is_first_purchase,
  -- Days since this customer's first ACCEPTED order. Drives D7 / D30 / D90 /
  -- D365 cohort LTV and ROAS straight from Power BI (playbook ch.2-3).
  -- NULL for UNKNOWN_DELETED: many deleted customers share that key.
  IF(customer_key = 'UNKNOWN_DELETED', NULL,
     DATE_DIFF(purchase_date, MIN(purchase_date) OVER (PARTITION BY customer_key), DAY))
                                                                   AS days_since_acquisition,
  -- Bundle band for AOV / bundle-velocity analysis (playbook ch.2).
  CASE WHEN revenue_gbp = 0   THEN '£0 (credits/free)'
       WHEN revenue_gbp < 10  THEN 'Under £10'
       WHEN revenue_gbp < 25  THEN '£10-£24'
       WHEN revenue_gbp < 50  THEN '£25-£49'
       WHEN revenue_gbp < 100 THEN '£50-£99'
       ELSE '£100+' END                                            AS order_value_band,
  CURRENT_TIMESTAMP()                                              AS loaded_at
FROM base;
-- NOTE: raffle_id may be NULL on some ACCEPTED rows (e.g. competitions).
-- Those land on raffle_key = 'UNKNOWN'. Gate 9.6 reports how many.


-- =====================================================================
-- SECTION 8 — fact_subscriptions  (baskets excluded)
-- Status column is `status` (NOT payment_status).
-- Partitioned monthly on created_date (purchase_date is ~66% NULL).
-- monthly_value_gbp is unit-safe, same rule as dim_subscription_model.
-- No cancellation date exists in source — churn/tenure is Phase 3 and must
-- reuse subscriber_tenure_detail's definition.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`
PARTITION BY DATE_TRUNC(created_date, MONTH)
CLUSTER BY customer_key, subscription_model_key
OPTIONS (description = 'One row per real subscription (baskets excluded). No partition expiry by design. Full rebuild nightly.')
AS
WITH unit AS (
  SELECT IF(MAX(total_cost) > 100, 100, 1) AS divisor
  FROM `rh-ga4-491819.rh_warehouse.dim_subscription_models`
)
SELECT
  s.subscription_id                                                AS subscription_key,
  IF(c.customer_id IS NULL, 'UNKNOWN_DELETED', s.customer_id)      AS customer_key,
  IF(m.subscription_model_id IS NULL, 'UNKNOWN', s.subscription_model_id) AS subscription_model_key,
  IF(r.raffle_id IS NULL, 'UNKNOWN', s.raffle_id)                  AS raffle_key,
  DATE(s.created_at)                                               AS created_date,
  s.created_at,
  s.purchase_date,
  s.next_purchase_date,
  s.status,
  s.status = 'ACTIVE'                                              AS is_status_active,
  CAST(s.total_cost AS NUMERIC) / u.divisor                        AS monthly_value_gbp,
  s.num_of_tickets,
  s.months,
  s.two_for_one,
  s.card_type,
  s.platform,
  s.created_by,
  s.created_by = 'special-offer'                                   AS is_special_offer_trial,
  CURRENT_TIMESTAMP()                                              AS loaded_at
FROM `rh-ga4-491819.rh_warehouse.fact_subscriptions` s
CROSS JOIN unit u
LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_customers`           c ON s.customer_id = c.customer_id
LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_subscription_models` m ON s.subscription_model_id = m.subscription_model_id
LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_raffles`             r ON s.raffle_id = r.raffle_id
WHERE s.status NOT IN ('PENDING_BASKET','IN_PAYMENT_PROCESS')
  AND s.customer_id IS NOT NULL
  AND s.created_at IS NOT NULL;
-- is_status_active reads the WooCommerce flag, which is known to mark a
-- subscription CANCELLED when auto-renew is switched off. Named
-- is_status_active (not is_active) so nobody mistakes it for "still paying".


-- =====================================================================
-- SECTION 8c — AD SPEND  (Google + Meta in scope; others kept, flagged)
--
-- Decisions 28 Sept: Jordan's scope is Google and Meta only. Other paid
-- channels (Affiliates, Nextdoor, Native) are KEPT but flagged
-- is_google_meta = FALSE so Power BI can filter them (default filter ON).
-- "Paid Social" in the manual upload = Meta (per Jordan).
-- Zero-cost organic rows (Direct, Email, Organic, SMS, Social...) dropped.
--
-- One source per platform per date, never double counted:
--   google : google_ads_transfer from its first date (29 Jul 2026);
--            manual upload only for dates before that
--   meta   : stg_meta_ads_daily (EMPTY today, the slot the Meta transfer or
--            weekly CSV will fill); manual "Paid Social" only for dates
--            before the first Meta row. The manual upload STOPPED 6 Sept.
--   other  : manual upload, all dates
-- Full rebuild nightly (~3k rows, effectively free).
-- =====================================================================

-- 8c.1 The Meta slot. Connecting Meta later = loading rows here. Nothing
--      downstream changes. Columns cover CPM, CPC, link CPC, link CTR and
--      frequency (reach) from the playbook.
CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
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
OPTIONS (description = 'Landing slot for Meta Ads daily data (DTS Facebook Ads transfer or weekly Ads Manager CSV). Empty until Meta is connected.');

-- 8c.2 The spend fact.
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
PARTITION BY spend_date
CLUSTER BY ad_platform, channel_group
OPTIONS (description = 'Daily paid media spend. Filter is_google_meta = TRUE for the reporting scope. Platform conversions are for comparison with backend only, never revenue. Full rebuild nightly.')
AS
WITH
google_names AS (   -- campaign name AS AT each date (campaigns are renamed mid-flight)
  SELECT campaign_id, _DATA_DATE AS d,
         ANY_VALUE(campaign_name) AS campaign_name,
         ANY_VALUE(campaign_advertising_channel_type) AS channel_type
  FROM `rh-ga4-491819.google_ads_transfer.ads_Campaign_6102368044`
  GROUP BY 1, 2
),
google_first_name AS (   -- fallback where the name snapshot does not reach back
  SELECT campaign_id,
         ARRAY_AGG(campaign_name ORDER BY _DATA_DATE LIMIT 1)[OFFSET(0)] AS campaign_name,
         ARRAY_AGG(campaign_advertising_channel_type ORDER BY _DATA_DATE LIMIT 1)[OFFSET(0)] AS channel_type
  FROM `rh-ga4-491819.google_ads_transfer.ads_Campaign_6102368044`
  GROUP BY 1
),
google_api AS (
  SELECT
    s.segments_date                                        AS spend_date,
    'google'                                               AS ad_platform,
    'google_ads_transfer'                                  AS source_system,
    CAST(s.customer_id AS STRING)                          AS account_id,
    CAST(s.campaign_id AS STRING)                          AS campaign_id,
    COALESCE(n.campaign_name, f.campaign_name)             AS campaign_name,
    COALESCE(n.channel_type,  f.channel_type)              AS campaign_type,
    s.segments_device                                      AS device,
    SUM(CAST(s.metrics_cost_micros AS NUMERIC)) / 1000000  AS spend_gbp,   -- micros
    SUM(s.metrics_impressions)                             AS impressions,
    CAST(NULL AS INT64)                                    AS reach,
    SUM(s.metrics_clicks)                                  AS clicks,
    CAST(NULL AS INT64)                                    AS link_clicks,
    CAST(SUM(s.metrics_conversions) AS NUMERIC)            AS platform_conversions,
    CAST(SUM(s.metrics_conversions_value) AS NUMERIC)      AS platform_conversion_value_gbp  -- already pounds
  FROM `rh-ga4-491819.google_ads_transfer.ads_CampaignBasicStats_6102368044` s
  LEFT JOIN google_names n      ON n.campaign_id = s.campaign_id AND n.d = s.segments_date
  LEFT JOIN google_first_name f ON f.campaign_id = s.campaign_id
  GROUP BY 1,2,3,4,5,6,7,8
),
google_start AS (SELECT MIN(spend_date) AS d FROM google_api),
meta_feed AS (
  SELECT spend_date, 'meta' AS ad_platform, source_system, account_id, campaign_id,
         campaign_name, campaign_objective AS campaign_type, device, spend_gbp,
         impressions, reach, clicks, link_clicks, platform_conversions,
         platform_conversion_value_gbp
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
  WHERE source_system = 'meta_api'
    AND spend_date >= '2026-09-01'
),
meta_start AS (SELECT MIN(spend_date) AS d FROM meta_feed),
api_rows AS (
  SELECT * FROM google_api
  UNION ALL
  SELECT * FROM meta_feed
),
manual AS (
  SELECT
    m.Period                                               AS spend_date,
    CASE
      WHEN m.`Channel Group` IN ('Paid Search - Generic','Paid Search - Brand','Demand Gen',
                                 'Display','Performance Max','Video') THEN 'google'
      WHEN m.`Channel Group` = 'Paid Social'                           THEN 'meta'
      ELSE 'other'
    END                                                    AS ad_platform,
    m.`Channel Group`                                      AS manual_channel_group,
    CAST(m.Cost AS NUMERIC)                                AS spend_gbp
  FROM `rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload` m
  WHERE m.Cost <> 0
)
SELECT
  spend_date, ad_platform, ad_platform IN ('google','meta') AS is_google_meta,
  source_system, account_id, campaign_id, campaign_name, campaign_type,
  CASE
    WHEN ad_platform = 'google' AND campaign_type = 'PERFORMANCE_MAX'   THEN 'Performance Max'
    WHEN ad_platform = 'google' AND campaign_type = 'DEMAND_GEN'        THEN 'Demand Gen'
    WHEN ad_platform = 'google' AND campaign_type = 'DISPLAY'           THEN 'Display'
    WHEN ad_platform = 'google' AND campaign_type = 'VIDEO'             THEN 'Video'
    WHEN ad_platform = 'google' AND LOWER(campaign_name) LIKE '%brand%' THEN 'Paid Search - Brand'
    WHEN ad_platform = 'google'                                         THEN 'Paid Search - Generic'
    WHEN ad_platform = 'meta'                                           THEN 'Paid Social'
    ELSE 'Unclassified'
  END                                                      AS channel_group,
  -- Funnel stage from the naming convention (_Acquisition_ = prospecting,
  -- _Revenue_ = existing-customer / retargeting). Older "RH | ..." names
  -- carry no token. Drives new-buyer CAC vs repeat-buyer CPA (playbook ch.2).
  CASE WHEN REGEXP_CONTAINS(campaign_name, r'(?i)_acquisition_') THEN 'Acquisition'
       WHEN REGEXP_CONTAINS(campaign_name, r'(?i)_revenue_')     THEN 'Revenue'
       ELSE 'Unspecified' END                              AS funnel_stage,
  -- Which draw the campaign was promoting, from the name. Feeds draw-level
  -- spend for break-even. Manual rows and untagged names = 'Unspecified'.
  CASE WHEN REGEXP_CONTAINS(campaign_name, r'(?i)dorset') THEN 'Dorset'
       WHEN REGEXP_CONTAINS(campaign_name, r'(?i)london') THEN 'London'
       WHEN REGEXP_CONTAINS(campaign_name, r'(?i)\bcar\b|car-') THEN 'Car'
       ELSE 'Unspecified' END                              AS draw_hint,
  REGEXP_CONTAINS(IFNULL(campaign_name,''), r'(?i)competitor') AS is_competitor_campaign,
  device,
  spend_gbp,
  spend_gbp                                                AS spend_ex_vat_gbp,
  ROUND(spend_gbp * NUMERIC '1.2', 2)                      AS spend_inc_vat_gbp,
  NUMERIC '0.20'                                           AS vat_rate,
  CASE
    WHEN ad_platform != 'meta' THEN CAST(NULL AS STRING)
    WHEN campaign_name IS NULL THEN 'Unknown (daily total)'
    WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'_rmg|remarketing|m/bof') THEN 'Remarketing'
    WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'_rtg|retarget|retention') THEN 'Retargeting'
    ELSE 'Prospecting'
  END                                                      AS meta_funnel,
  impressions, reach, clicks, link_clicks,
  platform_conversions, platform_conversion_value_gbp,
  CURRENT_TIMESTAMP() AS loaded_at
FROM api_rows
UNION ALL
SELECT
  mn.spend_date, mn.ad_platform, mn.ad_platform IN ('google','meta'),
  'manual_upload_frozen', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
  mn.manual_channel_group, 'Unspecified', 'Unspecified', FALSE,
  CAST(NULL AS STRING)                                     AS device,
  mn.spend_gbp,
  mn.spend_gbp                                             AS spend_ex_vat_gbp,
  ROUND(mn.spend_gbp * NUMERIC '1.2', 2)                   AS spend_inc_vat_gbp,
  NUMERIC '0.20'                                           AS vat_rate,
  IF(mn.ad_platform = 'meta', 'Unknown (daily total)', CAST(NULL AS STRING)) AS meta_funnel,
  CAST(NULL AS INT64)                                      AS impressions,
  CAST(NULL AS INT64)                                      AS reach,
  CAST(NULL AS INT64)                                      AS clicks,
  CAST(NULL AS INT64)                                      AS link_clicks,
  CAST(NULL AS NUMERIC)                                    AS platform_conversions,
  CAST(NULL AS NUMERIC)                                    AS platform_conversion_value_gbp,
  CURRENT_TIMESTAMP()                                      AS loaded_at
FROM manual mn
CROSS JOIN google_start gs
CROSS JOIN meta_start ms
WHERE (mn.ad_platform = 'google' AND (gs.d IS NULL OR mn.spend_date < gs.d))
   OR (mn.ad_platform = 'meta'   AND (ms.d IS NULL OR mn.spend_date < ms.d))
   OR  mn.ad_platform = 'other';


-- =====================================================================
-- SECTION 8d — ref_raffle_economics  (EMPTY slot, filled by Finance)
-- The cost side the playbook's break-even, fulfilment ratio and net
-- profit per draw need. We hold none of these numbers and will not invent
-- them. One row per raffle, maintained by hand; draw revenue already exists
-- (fact_transactions x dim_raffle), so break-even day becomes a Power BI
-- measure the moment this table has rows.
-- =====================================================================

CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.ref_raffle_economics`
(
  raffle_key                   STRING,     -- joins dim_raffle
  draw_hint                    STRING,     -- joins fact_marketing_spend_daily.draw_hint (Dorset / London / Car)
  prize_cost_gbp               NUMERIC,
  prize_delivery_legal_gbp     NUMERIC,
  charity_commitment_gbp       NUMERIC,
  other_fixed_cost_gbp         NUMERIC,
  payment_fee_pct              NUMERIC,    -- e.g. 0.019 for 1.9%
  notes                        STRING,
  provided_by                  STRING,
  valid_from                   DATE,
  updated_at                   TIMESTAMP
)
OPTIONS (description = 'Per-raffle cost inputs from Finance for break-even, fulfilment ratio and net profit per draw. Maintained manually. Empty until Finance supplies figures.');


-- =====================================================================
-- SECTION 8e — fact_order_status_daily  (all payment statuses, aggregated)
-- The playbook's gross vs net revenue, refund/adjustment rate and checkout
-- friction need the orders we deliberately EXCLUDE from fact_transactions.
-- Kept as a small daily summary so the ACCEPTED fact and its parity gate
-- stay untouched. Live check 28 Sept: REFUNDED 6,814 rows / £107.7k (0.46%
-- of gross), REJECTED 2,841 / £41k, PENDING_BASKET 349,850 / £6.92m.
-- ACCEPTED is already NET of refunds (a refunded order changes status).
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily`
PARTITION BY order_date
CLUSTER BY payment_status
OPTIONS (description = 'Daily count and value of orders by payment_status (all statuses). Gross = ACCEPTED + REFUNDED. Full rebuild nightly.')
AS
SELECT
  DATE(COALESCE(purchase_date, created_at))                AS order_date,
  payment_status,
  CASE
    WHEN payment_status = 'ACCEPTED'                          THEN 'Paid'
    WHEN payment_status = 'REFUNDED'                          THEN 'Refunded'
    WHEN payment_status = 'REJECTED'                          THEN 'Payment failed'
    WHEN payment_status IN ('PENDING_BASKET')                 THEN 'Abandoned basket'
    WHEN payment_status IN ('PENDING','IN_PAYMENT_PROCESS','LOCKED_FOR_PAYMENT') THEN 'In progress / stalled'
    WHEN STARTS_WITH(payment_status, 'FREE')                  THEN 'Free entry'
    ELSE 'Other'
  END                                                      AS status_group,
  payment_status IN ('ACCEPTED','REFUNDED')                AS counts_in_gross,
  COUNT(*)                                                 AS orders,
  COUNT(DISTINCT customer_id)                              AS customers,
  CAST(SUM(total_cost) AS NUMERIC) / 100                   AS value_gbp,
  SUM(ticket_count)                                        AS tickets,
  CURRENT_TIMESTAMP()                                      AS loaded_at
FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
WHERE COALESCE(purchase_date, created_at) IS NOT NULL
GROUP BY 1, 2, 3, 4;
-- COUNT(DISTINCT customer) here is per day x status: never sum it across days.


-- =====================================================================
-- SECTION 8f — fact_ga4_purchases  (GA4 BoughtTickets, trailing 3 days)
-- One row per user_pseudo_id per event_date where event_name = 'BoughtTickets'.
-- Incremental reload of last 3 days (CURRENT_DATE - 3 to CURRENT_DATE - 1)
-- reading only needed date shards via _TABLE_SUFFIX.
-- =====================================================================

CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
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
)
PARTITION BY event_date
CLUSTER BY user_id
OPTIONS (description = 'GA4 BoughtTickets events de-duplicated to one row per user_pseudo_id per event_date. Reloads trailing 3 days nightly.');

DELETE FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
WHERE event_date BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY)
                     AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY);

INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
  (event_date, user_pseudo_id, user_id, first_purchase_ts, ga_session_id,
   session_source, session_medium, session_campaign, device_category,
   bt_events_that_day, loaded_at)
WITH raw AS (
  SELECT
    PARSE_DATE('%Y%m%d', event_date) AS event_date,
    user_pseudo_id,
    user_id,
    event_timestamp,
    TIMESTAMP_MICROS(event_timestamp) AS event_ts,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS ga_session_id,
    COALESCE(session_traffic_source_last_click.cross_channel_campaign.source, traffic_source.source) AS session_source,
    COALESCE(session_traffic_source_last_click.cross_channel_campaign.medium, traffic_source.medium) AS session_medium,
    COALESCE(session_traffic_source_last_click.cross_channel_campaign.campaign_name, traffic_source.name) AS session_campaign,
    device.category AS device_category,
    ROW_NUMBER() OVER(PARTITION BY event_date, user_pseudo_id ORDER BY event_timestamp ASC) AS rn,
    COUNT(*) OVER(PARTITION BY event_date, user_pseudo_id) AS bt_events_that_day,
    MAX(user_id) OVER(PARTITION BY event_date, user_pseudo_id) AS max_user_id
  FROM `rh-ga4-491819.analytics_354198910.events_*`
  WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY))
                          AND FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY))
    AND event_name = 'BoughtTickets'
)
SELECT
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
WHERE rn = 1;


-- =====================================================================
-- SECTION 9 — POWER BI VIEWS  (Power BI reads these, never the tables)
-- Free to store; let us change a table without breaking a report.
-- =====================================================================

CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_date`              AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_date`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_customer`          AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_customer`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_raffle`            AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_subscription_model` AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_channel_first_touch` AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_first_touch`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_channel_last_touch`  AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_transactions`     AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_subscriptions`    AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_marketing_spend_daily` AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_order_status_daily`  AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_ga4_purchases`      AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_ref_raffle_economics`     AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.ref_raffle_economics`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_stg_meta_ads_daily`        AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`;



-- =====================================================================
-- DAILY TESTS
-- =====================================================================

SET v_tx_ts    = (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`);
SET v_sub_ts   = (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`);
SET v_spend_ts = (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`);
SET v_stat_ts  = (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily`);
SET v_ga4_ts   = (SELECT MAX(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`);

CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
(
  run_ts          TIMESTAMP,
  run_date        DATE,
  test            STRING,
  severity        STRING,    -- HARD = fails the job and emails; WARN = recorded only
  result          STRING,    -- PASS / FAIL / WARN
  source_value    NUMERIC,
  curated_value   NUMERIC,
  detail          STRING
)
PARTITION BY run_date
OPTIONS (description = 'One row per test per nightly run. The daily test record for the curated star. HARD failures fail the scheduled query and trigger the failure email.');

INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
  (run_ts, run_date, test, severity, result, source_value, curated_value, detail)
WITH
src_tx AS (
  SELECT COUNT(*) AS n, ROUND(SUM(total_cost)/100, 2) AS gbp, SUM(ticket_count) AS tickets
  FROM `rh-ga4-491819.rh_warehouse.fact_transactions` FOR SYSTEM_TIME AS OF v_tx_ts
  WHERE payment_status = 'ACCEPTED' AND COALESCE(purchase_date, created_at) IS NOT NULL
),
cur_tx AS (
  SELECT COUNT(*) AS n, ROUND(SUM(revenue_gbp), 2) AS gbp, SUM(tickets) AS tickets
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
),
monthly AS (
  SELECT COUNTIF(ABS(IFNULL(s.gbp,0) - IFNULL(c.gbp,0)) >= 0.005) AS months_off
  FROM (SELECT DATE_TRUNC(DATE(COALESCE(purchase_date, created_at)), MONTH) AS m, ROUND(SUM(total_cost)/100, 2) AS gbp
        FROM `rh-ga4-491819.rh_warehouse.fact_transactions` FOR SYSTEM_TIME AS OF v_tx_ts
        WHERE payment_status = 'ACCEPTED' AND COALESCE(purchase_date, created_at) IS NOT NULL GROUP BY 1) s
  FULL OUTER JOIN
       (SELECT DATE_TRUNC(purchase_date, MONTH) AS m, ROUND(SUM(revenue_gbp), 2) AS gbp
        FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` GROUP BY 1) c
  USING (m)
),
dups AS (
  SELECT
    (SELECT COUNT(*) - COUNT(DISTINCT transaction_id)         FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`)
  + (SELECT COUNT(*) - COUNT(DISTINCT subscription_key)       FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`)
  + (SELECT COUNT(*) - COUNT(DISTINCT customer_key)           FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_customer`)
  + (SELECT COUNT(*) - COUNT(DISTINCT raffle_key)             FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle`)
  + (SELECT COUNT(*) - COUNT(DISTINCT subscription_model_key) FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model`)
  + (SELECT COUNT(*) - COUNT(DISTINCT channel_key)            FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_first_touch`)
  + (SELECT COUNT(*) - COUNT(DISTINCT channel_key)            FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch`)
  + (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(event_date AS STRING), '|', user_pseudo_id)) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`) AS n
),
orphans AS (
  SELECT
    (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` d USING (customer_key) WHERE d.customer_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle` d USING (raffle_key) WHERE d.raffle_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_date` d ON f.purchase_date = d.date_key WHERE d.date_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_first_touch` d ON f.channel_first_touch_key = d.channel_key WHERE d.channel_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch` d ON f.channel_last_touch_key = d.channel_key WHERE d.channel_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` d USING (customer_key) WHERE d.customer_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model` d USING (subscription_model_key) WHERE d.subscription_model_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle` d USING (raffle_key) WHERE d.raffle_key IS NULL)
  + (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` f LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_date` d ON f.created_date = d.date_key WHERE d.date_key IS NULL) AS n
),
subs AS (
  SELECT
    (SELECT COUNT(*) FROM `rh-ga4-491819.rh_warehouse.fact_subscriptions` FOR SYSTEM_TIME AS OF v_sub_ts
       WHERE status NOT IN ('PENDING_BASKET','IN_PAYMENT_PROCESS') AND customer_id IS NOT NULL AND created_at IS NOT NULL) AS src,
    (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`) AS cur
),
gspend AS (   -- the Google Ads source is a view, so no time travel; the transfer runs at 01:00, well before this job
  SELECT
    (SELECT ROUND(SUM(metrics_cost_micros)/1e6, 2) FROM `rh-ga4-491819.google_ads_transfer.ads_CampaignBasicStats_6102368044`) AS src,
    (SELECT ROUND(SUM(spend_gbp), 2) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE source_system = 'google_ads_transfer') AS cur
),
mspend_overlap AS (
  SELECT
    COUNT(*) AS days_diff_gt_1,
    COALESCE(MAX(ROUND(ABS(COALESCE(man.spend, 0) - COALESCE(api.spend, 0)), 2)), 0) AS max_diff_gbp
  FROM (
    SELECT Period AS spend_date, SUM(Cost) AS spend
    FROM `rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload` FOR SYSTEM_TIME AS OF v_spend_ts
    WHERE `Channel Group` = 'Paid Social' AND Cost <> 0
      AND Period BETWEEN '2026-09-01' AND '2026-09-26'
    GROUP BY 1
  ) man
  FULL OUTER JOIN (
    SELECT spend_date, SUM(spend_gbp) AS spend
    FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
    WHERE source_system = 'meta_api'
      AND spend_date BETWEEN '2026-09-01' AND '2026-09-26'
    GROUP BY 1
  ) api ON man.spend_date = api.spend_date
  WHERE ABS(COALESCE(man.spend, 0) - COALESCE(api.spend, 0)) > 1.0
),
spend_quality AS (
  SELECT
    (SELECT COUNT(*) FROM (SELECT spend_date, ad_platform FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
       WHERE ad_platform IN ('google','meta') GROUP BY 1,2 HAVING COUNT(DISTINCT source_system) > 1)) AS overlap_days,
    (SELECT COUNT(*) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE channel_group = 'Unclassified') AS unclassified
),
acq AS (
  SELECT COUNT(*) AS mismatches
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` f
  JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` d USING (customer_key)
  WHERE f.is_first_purchase AND NOT d.is_unknown_member AND f.purchase_at <> d.acq_first_purchase_at
),
fresh AS (
  SELECT
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`), DAY) AS tx_days,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(created_date)  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`), DAY) AS sub_days,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'google'), DAY) AS g_days,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'meta'),   DAY) AS m_days
),
spend_vat AS (
  SELECT
    COUNTIF(spend_inc_vat_gbp != ROUND(spend_ex_vat_gbp * NUMERIC '1.2', 2)) AS inconsistent_vat_rows,
    ROUND(SUM(spend_ex_vat_gbp), 2) AS sum_ex_vat,
    ROUND(SUM(spend_gbp), 2) AS sum_orig_gbp,
    ROUND(SAFE_DIVIDE(
      SUM(IF(ad_platform = 'meta' AND spend_date >= '2026-09-01' AND meta_funnel = 'Unknown (daily total)', spend_gbp, 0)),
      SUM(IF(ad_platform = 'meta' AND spend_date >= '2026-09-01', spend_gbp, 0))
    ) * 100, 2) AS meta_unknown_pct
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
),
meta_api_diffs AS (
  SELECT COUNT(*) AS dates_with_diff
  FROM (
    SELECT COALESCE(s.spend_date, f.spend_date) AS spend_date,
           ROUND(SUM(s.spend_gbp), 2) AS stg_spend,
           ROUND(SUM(f.spend_gbp), 2) AS fact_spend
    FROM (
      SELECT spend_date, spend_gbp
      FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
      WHERE source_system = 'meta_api'
        AND spend_date BETWEEN '2026-09-01' AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
    ) s
    FULL OUTER JOIN (
      SELECT spend_date, spend_gbp
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
      WHERE ad_platform = 'meta'
        AND spend_date BETWEEN '2026-09-01' AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
    ) f ON s.spend_date = f.spend_date
    GROUP BY 1
    HAVING ABS(COALESCE(stg_spend, 0) - COALESCE(fact_spend, 0)) >= 0.01
  )
),
meta_double_source AS (
  SELECT COUNT(*) AS overlap_dates
  FROM (
    SELECT spend_date
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
    WHERE ad_platform = 'meta'
    GROUP BY spend_date
    HAVING COUNT(DISTINCT source_system) > 1
  )
),
ga4_parity AS (
  SELECT
    ga4.n AS ga4_rows,
    web.n AS web_checkouts,
    ROUND(SAFE_DIVIDE(ABS(ga4.n - web.n), web.n) * 100, 2) AS diff_pct
  FROM (
    SELECT COUNT(*) AS n
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
    WHERE event_date BETWEEN DATE_TRUNC(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 MONTH), MONTH)
                         AND LAST_DAY(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 MONTH), MONTH)
  ) ga4
  CROSS JOIN (
    SELECT COUNT(*) AS n
    FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_transactions`
    WHERE purchase_date BETWEEN DATE_TRUNC(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 MONTH), MONTH)
                            AND LAST_DAY(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 MONTH), MONTH)
      AND (
        (NOT is_subscription_payment AND (platform = 'WEB' OR platform IS NULL))
        OR (is_subscription_payment AND is_first_purchase AND platform = 'WEB')
      )
  ) web
),
meta_ad_parity AS (
  SELECT
    COUNTIF(is_mismatch = 1) AS mismatch_days,
    COALESCE(STRING_AGG(IF(is_mismatch = 1, FORMAT_DATE('%Y-%m-%d', d), NULL), ', ' ORDER BY d), 'None') AS bad_dates
  FROM (
    SELECT
      days.d,
      CASE
        WHEN camp.spend IS NOT NULL AND ad.spend IS NULL THEN 1
        WHEN camp.spend IS NULL AND ad.spend IS NOT NULL THEN 1
        WHEN camp.spend IS NOT NULL AND ad.spend IS NOT NULL AND ABS(camp.spend - ad.spend) / NULLIF(camp.spend, 0) > 0.01 THEN 1
        ELSE 0
      END AS is_mismatch
    FROM (
      SELECT d
      FROM UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY), DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY))) d
    ) days
    LEFT JOIN (
      SELECT spend_date AS d, SUM(spend_gbp) AS spend
      FROM 
`rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
      WHERE spend_date BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY) AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
      GROUP BY spend_date
    ) camp ON days.d = camp.d
    LEFT JOIN (
      SELECT date AS d, SUM(spend) AS spend
      FROM 
`rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily_ad`
      WHERE date BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY) AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
      GROUP BY date
    ) ad ON days.d = ad.d
  )
),
tagging_exc AS (
  SELECT
    COALESCE(SUM(purchasing_visitors), 0) AS purchasing_visitors,
    COALESCE(STRING_AGG(CONCAT(campaign_name, ' (', CAST(purchasing_visitors AS STRING), ')'), ', ' ORDER BY purchasing_visitors DESC LIMIT 5), 'None') AS top_5_campaigns
  FROM (
    SELECT
      campaign_name,
      SUM(purchasing_visitors) AS purchasing_visitors
    FROM `rh-ga4-491819.rh_curated_Bright_Test.ga4_channel_daily_rh`
    WHERE date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)
      AND (
        (rule_id IN ('meta_named', 'paid_medium') AND ga4_channel_group LIKE 'Organic%')
        OR
        (source IN ('klaviyo', 'attentive') AND ga4_channel_group NOT IN ('Email', 'SMS'))
      )
    GROUP BY campaign_name
  )
)
SELECT v_run_ts, DATE(v_run_ts), 'revenue_parity',   'HARD', IF(ABS(s.gbp - c.gbp) < 0.005, 'PASS', 'FAIL'), ROUND(CAST(s.gbp AS NUMERIC), 2), c.gbp, 'All ACCEPTED orders, £, penny-exact' FROM src_tx s, cur_tx c
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'row_parity',     'HARD', IF(s.n = c.n, 'PASS', 'FAIL'), s.n, c.n, 'ACCEPTED order count' FROM src_tx s, cur_tx c
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ticket_parity',  'HARD', IF(s.tickets = c.tickets, 'PASS', 'FAIL'), s.tickets, c.tickets, 'Tickets on ACCEPTED orders' FROM src_tx s, cur_tx c
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'monthly_parity', 'HARD', IF(months_off = 0, 'PASS', 'FAIL'), 0, months_off, 'Months where revenue differs' FROM monthly
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'duplicate_keys', 'HARD', IF(n = 0, 'PASS', 'FAIL'), 0, n, 'Duplicate primary keys across all tables' FROM dups
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'orphan_rows',    'HARD', IF(n = 0, 'PASS', 'FAIL'), 0, n, 'Fact rows with no matching dimension row' FROM orphans
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'subscription_parity', 'HARD', IF(src = cur, 'PASS', 'FAIL'), src, cur, 'Real subscriptions (baskets excluded)' FROM subs
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'google_spend_parity', 'HARD', IF(ABS(src - cur) < 0.01, 'PASS', 'FAIL'), ROUND(CAST(src AS NUMERIC), 2), cur, 'Google Ads transfer, £' FROM gspend
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_spend_parity',   'WARN',
               IF(days_diff_gt_1 = 0, 'PASS', 'WARN'), 0, days_diff_gt_1,
               'Days where manual vs meta_api differs by > £1 on overlapping dates (2026-09-01 to 2026-09-26)' FROM mspend_overlap
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'spend_no_double_count', 'HARD', IF(overlap_days = 0, 'PASS', 'FAIL'), 0, overlap_days, 'Days where one platform has two sources' FROM spend_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'spend_unclassified',    'WARN', IF(unclassified = 0, 'PASS', 'WARN'), 0, unclassified, 'Spend rows the classifier could not place' FROM spend_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'acquisition_consistency','HARD', IF(mismatches = 0, 'PASS', 'FAIL'), 0, mismatches, 'First order on customer = first order in fact' FROM acq
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_transactions', 'HARD', IF(tx_days  <= 2, 'PASS', 'FAIL'), 2, tx_days,  'Days since latest order (limit 2)' FROM fresh
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_subscriptions','WARN', IF(sub_days <= 2, 'PASS', 'WARN'), 2, sub_days, 'Days since latest new subscription' FROM fresh
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_google_spend', 'HARD', IF(g_days   <= 2, 'PASS', 'FAIL'), 2, g_days,   'Days since latest Google spend day' FROM fresh
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_meta_spend',   'WARN', IF(m_days   <= 3, 'PASS', 'WARN'), 3, m_days,   'Days since latest Meta spend day (manual upload)' FROM fresh
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'vat_columns_consistent', 'HARD', IF(inconsistent_vat_rows = 0, 'PASS', 'FAIL'), 0, inconsistent_vat_rows, 'Every row has spend_inc_vat_gbp = ROUND(spend_ex_vat_gbp * 1.2, 2)' FROM spend_vat
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'spend_parity_unchanged', 'HARD', IF(sum_ex_vat = sum_orig_gbp, 'PASS', 'FAIL'), sum_orig_gbp, sum_ex_vat, 'SUM(spend_ex_vat_gbp) = SUM(spend_gbp)' FROM spend_vat
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_funnel_classified', 'WARN', IF(meta_unknown_pct = 0, 'PASS', 'WARN'), 0, meta_unknown_pct, 'Share of Meta spend in Unknown from 2026-09-01 onwards, %' FROM spend_vat
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_api_parity',        'HARD', IF(dates_with_diff = 0, 'PASS', 'FAIL'), 0, dates_with_diff, 'Meta spend equals stg_meta_ads_daily from 2026-09-01 to yesterday' FROM meta_api_diffs
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_no_double_source',   'HARD', IF(overlap_dates = 0, 'PASS', 'FAIL'), 0, overlap_dates, 'No Meta date has rows from both manual and API sources' FROM meta_double_source
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_purchase_parity',   'WARN', IF(diff_pct <= 10.0, 'PASS', 'WARN'), web_checkouts, ga4_rows, 'GA4 BoughtTickets rows within ±10% of web checkouts for previous month' FROM ga4_parity
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tagging_exceptions',    'WARN', IF(purchasing_visitors = 0, 'PASS', 'WARN'), 0, purchasing_visitors, CONCAT('Top mis-tagged campaigns: ', top_5_campaigns) FROM tagging_exc
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_ad_level_parity', 'WARN', IF(mismatch_days = 0, 'PASS', 'WARN'), 0, mismatch_days, IF(mismatch_days = 0, 'Ad-level spend matches campaign spend within 1% for all days in last 7', CONCAT('Days in last 7 with >1% diff or missing ad-level: ', bad_dates)) FROM meta_ad_parity;

-- Order-status parity is its own statement: BigQuery allows only ONE
-- snapshot time per source table per statement, and this table was built
-- a few minutes after fact_transactions.
INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
  (run_ts, run_date, test, severity, result, source_value, curated_value, detail)
SELECT v_run_ts, DATE(v_run_ts), 'order_status_parity', 'HARD', IF(src = cur, 'PASS', 'FAIL'), src, cur, 'All orders, every status'
FROM (
  SELECT
    (SELECT COUNT(*) FROM `rh-ga4-491819.rh_warehouse.fact_transactions` FOR SYSTEM_TIME AS OF v_stat_ts WHERE COALESCE(purchase_date, created_at) IS NOT NULL) AS src,
    (SELECT SUM(orders) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily`) AS cur
);

-- Power BI data-health page reads this.
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dq_gate_results` AS
SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`;

-- RE-APPLY DESCRIPTIONS (CREATE OR REPLACE wipes them). Free: metadata only.
BEGIN
  MERGE `rh-ga4-491819.rh_curated_Bright_Test.meta_metric_definitions` T
  USING (
    SELECT 'spend_inc_vat_gbp' AS metric_name,
           'ROAS and ROI use inc-VAT spend because Raffle House cannot recover VAT on ad spend.' AS plain_english
  ) S
  ON T.metric_name = S.metric_name
  WHEN MATCHED THEN UPDATE SET plain_english = S.plain_english, updated_at = CURRENT_TIMESTAMP()
  WHEN NOT MATCHED THEN INSERT (metric_name, plain_english, updated_at) VALUES (S.metric_name, S.plain_english, CURRENT_TIMESTAMP());
EXCEPTION WHEN ERROR THEN
  SELECT 'metadata merge skipped' AS note, @@error.message AS reason;
END;

BEGIN
  MERGE `rh-ga4-491819.rh_curated_Bright_Test.meta_column_dictionary` T
  USING (SELECT * FROM UNNEST([
    STRUCT<table_name STRING, column_name STRING, description STRING, pii_level STRING, status STRING, updated_at TIMESTAMP>
    ('fact_marketing_spend_daily', 'spend_ex_vat_gbp', 'Daily paid media spend excluding VAT in pounds. Matches platform-reported spend.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_marketing_spend_daily', 'spend_inc_vat_gbp', 'Daily paid media spend including 20% VAT in pounds. ROUND(spend_ex_vat_gbp * 1.2, 2). Used for ROAS and ROI.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_marketing_spend_daily', 'vat_rate', 'VAT rate applied to ad spend (0.20 for 20% UK VAT).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_marketing_spend_daily', 'meta_funnel', 'Meta campaign funnel stage classified from campaign name (Prospecting, Retargeting, Remarketing, or Unknown (daily total) for manual upload). NULL for Google.', 'none', 'approved', CURRENT_TIMESTAMP())
  ])) S
  ON T.table_name = S.table_name AND T.column_name = S.column_name
  WHEN MATCHED THEN UPDATE SET description = S.description, pii_level = S.pii_level, status = S.status, updated_at = S.updated_at
  WHEN NOT MATCHED THEN INSERT (table_name, column_name, description, pii_level, status, updated_at) VALUES (S.table_name, S.column_name, S.description, S.pii_level, S.status, S.updated_at);
EXCEPTION WHEN ERROR THEN
  SELECT 'metadata merge skipped' AS note, @@error.message AS reason;
END;

-- Wrapped so a missing procedure or a bad description never fails the build.
BEGIN
  CALL `rh-ga4-491819.rh_curated_Bright_Test.sp_apply_descriptions`();
EXCEPTION WHEN ERROR THEN
  SELECT 'sp_apply_descriptions skipped' AS note, @@error.message AS reason;
END;


-- =====================================================================
-- SECTION 12 — rpt_channel_perf_daily  (last 10 touch dates reload)
-- Touch window requires 7 days to mature; reloading last 10 days ensures
-- all maturing touch cohorts update cleanly every night.
-- =====================================================================
DELETE FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_channel_perf_daily`
WHERE touch_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 10 DAY);

INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.rpt_channel_perf_daily`
SELECT * 
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_channel_perf_daily_calc`
WHERE touch_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 10 DAY);

-- FAIL LOUDLY. Any HARD failure fails the job and sends the failure email.
ASSERT NOT EXISTS (
  SELECT 1 FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
  WHERE run_ts = v_run_ts AND severity = 'HARD' AND result = 'FAIL'
) AS 'Raffle House nightly build: a HARD test FAILED. Check rh_curated_Bright_Test.dq_gate_results for tonight.';
