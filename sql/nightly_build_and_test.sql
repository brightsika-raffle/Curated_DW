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
  CASE
    WHEN price_gbp IN (10, 15) THEN 'Bronze'
    WHEN price_gbp IN (20, 25, 30) THEN 'Silver'
    WHEN price_gbp IN (35, 50) THEN 'Gold'
    WHEN price_gbp IN (55, 75) THEN 'Platinum'
    ELSE 'Other'
  END                                                AS tier_name,
  FALSE                                              AS is_unknown_member,
  CURRENT_TIMESTAMP()                                AS loaded_at
FROM m
UNION ALL
SELECT 'UNKNOWN', 'Unknown tier', CAST(NULL AS NUMERIC), CAST(NULL AS STRING),
       CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS BOOL), CAST(NULL AS BOOL),
       CAST(NULL AS TIMESTAMP), 'Unknown', 'Other', TRUE, CURRENT_TIMESTAMP();
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
-- SECTION 7b — fact_order_audience_label  (full rebuild nightly)
-- One row per accepted order with warehouse audience label (existing / engaged / new / free).
-- Built from curated fact_transactions. Compare with Meta user_segment_key.
-- Chained directly after fact_transactions: if fact_transactions fails, this does not run.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`
PARTITION BY purchase_date
CLUSTER BY audience_label, channel_group
OPTIONS (description = 'One row per accepted order with the warehouse audience label (existing / engaged / new / free). Built from curated fact_transactions. Compare with Meta user_segment_key in stg_meta_segment_daily.', labels = [('layer', 'fact'), ('owner', 'bright_sika')])
AS
WITH t AS (
  SELECT
    transaction_id, purchase_date, purchase_at, customer_key, revenue_gbp, channel_last_touch_key,
    customer_age_days_at_order, subscription_key, is_subscription_payment,
    COUNTIF(revenue_gbp > 0) OVER w AS prior_paid_orders,
    COUNTIF(revenue_gbp = 0) OVER w AS prior_free_orders,
    ROW_NUMBER() OVER (PARTITION BY COALESCE(subscription_key, transaction_id) ORDER BY purchase_at) AS sub_payment_no
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE customer_key != 'UNKNOWN_DELETED'
  WINDOW w AS (PARTITION BY customer_key ORDER BY purchase_at ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
)
SELECT
  t.transaction_id,
  t.purchase_date,
  t.purchase_at,
  t.customer_key,
  t.revenue_gbp,
  t.channel_last_touch_key,
  COALESCE(c.channel_group, 'Unattributed') AS channel_group,
  c.utm_campaign,
  CASE
    WHEN t.revenue_gbp = 0 THEN 'free'
    WHEN t.prior_paid_orders > 0 THEN 'existing'
    WHEN t.prior_free_orders > 0 OR t.customer_age_days_at_order > 0 THEN 'engaged'
    ELSE 'new'
  END AS audience_label,
  CASE
    WHEN t.revenue_gbp = 0 THEN 'order is £0'
    WHEN t.prior_paid_orders > 0 THEN 'prior paid order'
    WHEN t.prior_free_orders > 0 OR t.customer_age_days_at_order > 0 THEN 'account older than order day'
    ELSE 'first order, account created same day'
  END AS label_reason,
  t.prior_paid_orders,
  t.prior_free_orders,
  (t.is_subscription_payment AND t.sub_payment_no > 1) AS is_renewal,
  CURRENT_TIMESTAMP() AS loaded_at
FROM t
LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch` c ON c.channel_key = t.channel_last_touch_key;


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
),
free_accepted_tx AS (
  SELECT
    subscription_key AS subscription_id,
    MIN(purchase_at) AS first_free_at
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE subscription_key IS NOT NULL
    AND is_free_entry
  GROUP BY subscription_key
),
-- Genuine special-offer trial definition (Trello card + business owner decisions):
-- 1. created_by = 'special-offer', not in ('PENDING_BASKET','IN_PAYMENT_PROCESS'), and has a £0 ACCEPTED tx (first_free_at).
-- 2. Exclude exploit accounts with >= 10 trial subscriptions.
--    Documented backstop (5 accounts today with 1,234 / 121 / 27 / 21 / 12 trial subscriptions):
--    '66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0',
--    '6911fdcc76ae9a44158d1022', '64b5d23f79c5fe003eefd800'.
-- 3. One trial per customer: take each customer's first special-offer trial subscription, ordered by first_free_at.
special_offer_ranked AS (
  SELECT
    s.subscription_id,
    s.customer_id,
    COUNT(*) OVER (PARTITION BY s.customer_id) AS cust_trial_subs,
    ROW_NUMBER() OVER (PARTITION BY s.customer_id ORDER BY ft.first_free_at, s.created_at, s.subscription_id) AS cust_trial_rn
  FROM `rh-ga4-491819.rh_warehouse.fact_subscriptions` s
  JOIN free_accepted_tx ft ON s.subscription_id = ft.subscription_id
  WHERE s.created_by = 'special-offer'
    AND s.status NOT IN ('PENDING_BASKET','IN_PAYMENT_PROCESS')
    AND s.customer_id IS NOT NULL
    AND s.created_at IS NOT NULL
),
genuine_trial_subs AS (
  SELECT subscription_id
  FROM special_offer_ranked
  WHERE cust_trial_subs < 10
    AND cust_trial_rn = 1
    AND customer_id NOT IN (
      '66d231300ff155003e747fcc',
      '69dfaa9740447116ffe02052',
      '67e8bfdb8b1def003ebee6e0',
      '6911fdcc76ae9a44158d1022',
      '64b5d23f79c5fe003eefd800'
    )
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
  gt.subscription_id IS NOT NULL                                   AS is_special_offer_trial,
  CURRENT_TIMESTAMP()                                              AS loaded_at
FROM `rh-ga4-491819.rh_warehouse.fact_subscriptions` s
CROSS JOIN unit u
LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_customers`           c ON s.customer_id = c.customer_id
LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_subscription_models` m ON s.subscription_model_id = m.subscription_model_id
LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_raffles`             r ON s.raffle_id = r.raffle_id
LEFT JOIN genuine_trial_subs                                   gt ON s.subscription_id = gt.subscription_id
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
OPTIONS (description = 'Meta Ads daily ad set level audience segments (prospecting, engaged, existing, unknown, null).');

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
  ga4_reliable        BOOLEAN,
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
   bt_events_that_day, ga4_reliable, loaded_at)
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
  event_date < DATE '2026-10-19' AS ga4_reliable,
  CURRENT_TIMESTAMP() AS loaded_at
FROM raw
WHERE rn = 1;


-- =====================================================================
-- SECTION 8h — rpt_ga4_prior_visit_daily  (incremental reload of last 3 days)
-- GA4 purchases by day and GA4 customer_type, with the share that had a session
-- on an earlier day in the previous 30 days. Estimate only; cannot be joined to orders.
-- =====================================================================

CREATE TABLE IF NOT EXISTS `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`
(
  purchase_date        DATE,
  ga4_customer_type    STRING,
  ga4_purchases        INT64,
  with_prior_visit_30d INT64,
  prior_visit_share    FLOAT64,
  loaded_at            TIMESTAMP
)
PARTITION BY purchase_date
OPTIONS (description = 'GA4 purchases by day and GA4 customer_type, with the share that had a session on an earlier day in the previous 30 days. Estimate only; cannot be joined to orders.', labels = [('layer', 'report'), ('owner', 'bright_sika')]);

DELETE FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`
WHERE purchase_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY);

INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`
  (purchase_date, ga4_customer_type, ga4_purchases, with_prior_visit_30d, prior_visit_share, loaded_at)
WITH ev AS (
  SELECT user_pseudo_id AS u, event_name AS e, TIMESTAMP_MICROS(event_timestamp) AS ts, ecommerce.transaction_id AS tx,
    IF(event_name = 'purchase', (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'customer_type'), NULL) AS ct
  FROM `rh-ga4-491819.analytics_354198910.events_*`
  WHERE _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 33 DAY))
    AND event_name IN ('purchase', 'session_start') AND user_pseudo_id IS NOT NULL
),
pur AS (SELECT tx, ANY_VALUE(u) AS u, MIN(ts) AS ts, ANY_VALUE(ct) AS ct FROM ev WHERE e = 'purchase' AND tx IS NOT NULL GROUP BY tx),
vis AS (SELECT u, DATE(ts, 'Europe/London') AS d FROM ev WHERE e = 'session_start' GROUP BY 1, 2),
pv AS (
  SELECT p.tx, DATE(p.ts, 'Europe/London') AS purchase_date, p.ct,
    LOGICAL_OR(v.d < DATE(p.ts, 'Europe/London') AND v.d >= DATE_SUB(DATE(p.ts, 'Europe/London'), INTERVAL 30 DAY)) AS prior_visit_30d
  FROM pur p LEFT JOIN vis v USING (u) GROUP BY 1, 2, 3
)
SELECT purchase_date, COALESCE(ct, 'unknown') AS ga4_customer_type, COUNT(*) AS ga4_purchases,
  COUNTIF(prior_visit_30d) AS with_prior_visit_30d, SAFE_DIVIDE(COUNTIF(prior_visit_30d), COUNT(*)) AS prior_visit_share,
  CURRENT_TIMESTAMP() AS loaded_at
FROM pv
WHERE purchase_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY)
GROUP BY 1, 2;


-- =====================================================================
-- =====================================================================
-- SECTION 8g — fact_transaction_touch  (replaces fact_transaction_attribution)
-- First-touch and last-touch attribution URLs and timestamps per transaction.
-- =====================================================================
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch`
PARTITION BY purchase_date
CLUSTER BY customer_key
OPTIONS (description = "First-touch and last-touch attribution URLs and timestamps per transaction, built directly from raw source tables. Replaces rh_warehouse.fact_transaction_attribution.")
AS
SELECT
  t.transaction_id,
  IF(c.customer_id IS NULL, 'UNKNOWN_DELETED', t.customer_id) AS customer_key,
  DATE(COALESCE(t.purchase_date, t.created_at)) AS purchase_date,
  COALESCE(t.purchase_date, t.created_at) AS purchase_at,
  t.subscription_id AS subscription_key,
  t.order_type,
  t.payment_status,
  CASE
    WHEN t.subscription_id IS NOT NULL AND s.subscription_id IS NOT NULL
      THEN 'inherited_from_subscription_signup'
    WHEN t.subscription_id IS NOT NULL
      THEN 'orphan_subscription_own_touch'
    ELSE 'transaction_own_touch'
  END AS attribution_basis,
  COALESCE(s.attribution_first_touch,           t.attribution_first_touch)           AS first_touch_url,
  COALESCE(s.attribution_first_touch_timestamp, t.attribution_first_touch_timestamp) AS first_touch_timestamp,
  COALESCE(s.attribution_last_touch,            t.attribution_last_touch)            AS last_touch_url,
  COALESCE(s.attribution_last_touch_timestamp,  t.attribution_last_touch_timestamp)  AS last_touch_timestamp,
  COALESCE(s.attribution_ga_client_id,          t.attribution_ga_client_id)          AS ga_client_id,
  t.attribution_first_touch           AS payment_touch_first_url,
  t.attribution_first_touch_timestamp AS payment_touch_first_timestamp,
  t.attribution_last_touch            AS payment_touch_last_url,
  t.attribution_last_touch_timestamp  AS payment_touch_last_timestamp,
  t.attribution_ga_client_id          AS payment_touch_ga_client_id,
  CURRENT_TIMESTAMP()                 AS loaded_at
FROM `rh-ga4-491819.rh_warehouse.fact_transactions` t
LEFT JOIN `rh-ga4-491819.rh_warehouse.fact_subscriptions` s
  ON t.subscription_id = s.subscription_id
LEFT JOIN `rh-ga4-491819.rh_warehouse.dim_customers` c
  ON t.customer_id = c.customer_id;


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
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_transaction_touch`   AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_wh_daily`    AS SELECT * EXCEPT(built_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_wh_daily`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_page_daily`  AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_page_status` AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dim_paid_media_entity`       AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_entity_daily` AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_entity_status` AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_ga4_session_funnel`
OPTIONS (description = "Power BI view over fact_ga4_session_funnel without built_at.")
AS SELECT * EXCEPT(built_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_session_funnel`;
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_meta_audience_compare_daily`
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
FROM w FULL JOIN m USING (d, bucket);




-- =====================================================================
-- SECTION 10 — rpt_charity_sales_daily  (daily sales by charity)
-- One row per date x selected_charity x charity_type.
-- Reads ONLY fact_transactions (source). Full rebuild nightly.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily`
PARTITION BY date
CLUSTER BY charity_type, selected_charity
OPTIONS (description = "Daily sales by charity and category from fact_transactions. Full rebuild nightly.")
AS
WITH valid_tx AS (
  SELECT
    DATE(COALESCE(purchase_date, created_at), 'Europe/London') AS date,
    customer_id,
    transaction_id,
    CAST(total_cost AS NUMERIC) / 100 AS total_cost_gbp,
    COALESCE(NULLIF(TRIM(selected_charity), ''), 'No charity selected') AS raw_charity,
    ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY COALESCE(purchase_date, created_at) ASC, transaction_id ASC) AS cust_tx_rank
  FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
  WHERE 
    payment_status != 'PENDING_BASKET'
    AND COALESCE(payment_initiated_by, '') != 'recurring'
    AND NOT (COALESCE(order_type, '') = 'wowcher' AND total_cost = 0)
    AND (total_cost > 0 OR (total_cost = 0 AND subscription_id IS NOT NULL))
),
norm_charity AS (
  SELECT
    date,
    customer_id,
    transaction_id,
    total_cost_gbp,
    cust_tx_rank,
    CASE 
      WHEN LOWER(raw_charity) LIKE '%brain tumour%' THEN 'Brain Tumour Research'
      WHEN LOWER(raw_charity) LIKE '%war paws%' THEN 'War Paws'
      WHEN LOWER(raw_charity) LIKE '%centrepoint%' OR LOWER(raw_charity) LIKE '%centre point%' THEN 'CentrePoint'
      WHEN LOWER(raw_charity) LIKE '%all dogs matter%' THEN 'All Dogs Matter'
      WHEN LOWER(raw_charity) LIKE '%dragonfly cancer%' THEN 'Dragonfly Cancer Trust'
      WHEN LOWER(raw_charity) LIKE '%housingforwomen%' OR LOWER(raw_charity) LIKE '%housing for women%' THEN 'HousingForWomen'
      WHEN LOWER(raw_charity) LIKE '%when you wish upon a star%' THEN 'When You Wish Upon a Star'
      WHEN LOWER(raw_charity) LIKE '%dogs for autism%' THEN 'Dogs for Autism'
      WHEN LOWER(raw_charity) LIKE '%cat watch%' THEN 'Cat Watch Rescue Shelter'
      WHEN LOWER(raw_charity) LIKE '%sense%' THEN 'Sense'
      WHEN LOWER(raw_charity) LIKE '%mental health innovations%' OR LOWER(raw_charity) LIKE '%shout%' THEN 'Mental Health Innovations (formerly Shout)'
      WHEN LOWER(raw_charity) LIKE '%british heart%' THEN 'British Heart Foundation'
      WHEN LOWER(raw_charity) LIKE '%phoenix rehoming%' THEN 'Phoenix Rehoming'
      WHEN LOWER(raw_charity) LIKE '%helpful hounds%' THEN 'Helpful Hounds'
      WHEN LOWER(raw_charity) LIKE '%cfenc%' THEN "CFENC (children's education)"
      WHEN LOWER(raw_charity) LIKE '%it gets better%' THEN 'It Gets Better'
      WHEN LOWER(raw_charity) LIKE '%head up%' THEN 'Head Up'
      WHEN LOWER(raw_charity) LIKE '%momentum%' THEN 'Momentum'
      WHEN LOWER(raw_charity) LIKE '%beyond conflict%' THEN 'Beyond Conflict'
      WHEN LOWER(raw_charity) LIKE '%save the children%' THEN 'Save the Children'
      WHEN LOWER(raw_charity) LIKE '%prostate cancer%' THEN 'Prostate Cancer UK'
      WHEN LOWER(raw_charity) LIKE '%miracle%mission%' THEN "Miracle's Mission"
      WHEN LOWER(raw_charity) LIKE '%alzheimer%research%' THEN "Alzheimer's Research UK"
      WHEN LOWER(raw_charity) LIKE '%marie curie%' THEN 'Marie Curie'
      WHEN LOWER(raw_charity) LIKE '%children in need%' THEN 'Children in Need'
      WHEN LOWER(raw_charity) LIKE '%penny%ark%' THEN "Penny's Ark"
      WHEN LOWER(raw_charity) LIKE '%pipal tree%' THEN 'Pipal Tree'
      WHEN LOWER(raw_charity) LIKE '%nspcc%' THEN 'NSPCC'
      WHEN LOWER(raw_charity) LIKE '%emmaus%' THEN 'Emmaus'
      WHEN LOWER(raw_charity) LIKE '%wwf%' THEN 'WWF'
      WHEN LOWER(raw_charity) LIKE '%dementia uk%' THEN 'Dementia UK'
      WHEN LOWER(raw_charity) LIKE '%alzheimer%society%' THEN "Alzheimer's Society"
      WHEN LOWER(raw_charity) LIKE '%rain rescue%' THEN 'Rain Rescue'
      WHEN LOWER(raw_charity) LIKE '%cancer research uk%' THEN 'Cancer Research UK'
      WHEN LOWER(raw_charity) LIKE '%parkinson%' THEN "Parkinson's UK"
      WHEN LOWER(raw_charity) LIKE '%samaritans%' THEN 'Samaritans'
      WHEN LOWER(raw_charity) LIKE '%british red cross%' THEN 'British Red Cross'
      WHEN LOWER(raw_charity) LIKE '%leukaemia uk%' THEN 'Leukaemia UK'
      WHEN LOWER(raw_charity) LIKE '%millimetres 2 mountains%' THEN 'Millimetres 2 Mountains'
      WHEN LOWER(raw_charity) LIKE '%support dogs%' THEN 'Support Dogs'
      WHEN LOWER(raw_charity) LIKE '%battersea%' THEN 'Battersea Dogs and Cats Home'
      WHEN LOWER(raw_charity) LIKE '%st. john ambulance%' OR LOWER(raw_charity) LIKE '%st john ambulance%' THEN 'St. John Ambulance'
      WHEN LOWER(raw_charity) LIKE '%national trust%' THEN 'National Trust'
      WHEN LOWER(raw_charity) LIKE '%barnardo%' THEN "Barnardo's"
      WHEN LOWER(raw_charity) LIKE '%felix fund%' THEN 'Felix Fund'
      WHEN LOWER(raw_charity) LIKE '%macmillan%' THEN 'Macmillan Cancer Support'
      WHEN LOWER(raw_charity) LIKE '%ips%' THEN 'IPS'
      WHEN LOWER(raw_charity) LIKE '%royal british legion%' THEN 'Royal British Legion'
      WHEN LOWER(raw_charity) LIKE '%challenging mnd%' THEN 'Challenging MND'
      WHEN LOWER(raw_charity) LIKE '%nomad opening doors%' THEN 'Nomad Opening Doors'
      WHEN LOWER(raw_charity) LIKE '%keep britain tidy%' THEN 'Keep Britain Tidy'
      WHEN LOWER(raw_charity) LIKE '%diabetes uk%' THEN 'Diabetes UK'
      WHEN LOWER(raw_charity) LIKE '%moodswings%' THEN 'Moodswings'
      WHEN LOWER(raw_charity) LIKE '%duke of edinburgh%' THEN 'Duke of Edinburgh Awards'
      WHEN LOWER(raw_charity) LIKE '%guide dogs%' THEN 'Guide Dogs'
      WHEN LOWER(raw_charity) LIKE '%rspca%' THEN 'RSPCA'
      WHEN LOWER(raw_charity) LIKE '%rnli%' THEN 'RNLI Lifeboats'
      WHEN LOWER(raw_charity) LIKE '%war child%' THEN 'War Child'
      WHEN LOWER(raw_charity) LIKE '%science and technology%' THEN 'The Foundation for Science and Technology'
      WHEN LOWER(raw_charity) LIKE '%help for heroes%' THEN 'Help for Heroes'
      WHEN LOWER(raw_charity) LIKE '%scar free%' THEN 'The Scar Free Foundation'
      WHEN LOWER(raw_charity) LIKE '%great ormond%' OR LOWER(raw_charity) LIKE '%greatormond%' THEN 'Great Ormond Street Hospital'
      WHEN LOWER(raw_charity) LIKE '%flying seagull%' THEN 'The Flying Seagull Project'
      WHEN LOWER(raw_charity) LIKE '%serve on%' THEN 'Serve On'
      WHEN raw_charity IN ('none', 'No charity selected') THEN 'No charity selected'
      ELSE raw_charity
    END AS selected_charity
  FROM valid_tx
),
categorized AS (
  SELECT
    date,
    customer_id,
    transaction_id,
    total_cost_gbp,
    cust_tx_rank,
    selected_charity,
    CASE 
      WHEN selected_charity IN (
        'Brain Tumour Research', 'Dragonfly Cancer Trust', 'British Heart Foundation',
        'Prostate Cancer UK', "Alzheimer's Research UK", 'Marie Curie', 'Dementia UK',
        "Alzheimer's Society", 'Cancer Research UK', "Parkinson's UK", 'Leukaemia UK',
        'Macmillan Cancer Support', 'Challenging MND', 'Diabetes UK',
        'Mental Health Innovations (formerly Shout)', 'Head Up', 'St. John Ambulance',
        'The Scar Free Foundation', 'Moodswings'
      ) THEN 'health'
      WHEN selected_charity IN (
        'War Paws', 'All Dogs Matter', 'Dogs for Autism', 'Cat Watch Rescue Shelter',
        'Phoenix Rehoming', 'Helpful Hounds', "Miracle's Mission", "Penny's Ark",
        'Rain Rescue', 'Support Dogs', 'Battersea Dogs and Cats Home', 'Guide Dogs', 'RSPCA'
      ) THEN 'animals'
      WHEN selected_charity IN (
        'When You Wish Upon a Star', "CFENC (children's education)", 'Momentum',
        'Save the Children', 'Children in Need', 'NSPCC', "Barnardo's",
        'Duke of Edinburgh Awards', 'War Child', 'Great Ormond Street Hospital',
        'The Flying Seagull Project'
      ) THEN 'children'
      WHEN selected_charity IN (
        'CentrePoint', 'HousingForWomen', 'Sense', 'It Gets Better', 'Beyond Conflict',
        'Pipal Tree', 'Emmaus', 'Samaritans', 'British Red Cross', 'Millimetres 2 Mountains',
        'Felix Fund', 'IPS', 'Royal British Legion', 'Nomad Opening Doors',
        'RNLI Lifeboats', 'Help for Heroes', 'Serve On'
      ) THEN 'community'
      WHEN selected_charity IN (
        'WWF', 'National Trust', 'Keep Britain Tidy'
      ) THEN 'environment'
      ELSE 'other'
    END AS charity_type
  FROM norm_charity
)
SELECT
  date,
  selected_charity,
  charity_type,
  COUNT(transaction_id) AS purchases,
  ROUND(SUM(total_cost_gbp), 2) AS revenue,
  COUNTIF(cust_tx_rank = 1) AS new_customers,
  ROUND(SAFE_DIVIDE(SUM(total_cost_gbp), COUNT(transaction_id)), 2) AS average_basket,
  CURRENT_TIMESTAMP() AS loaded_at
FROM categorized
GROUP BY 1, 2, 3;

CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_charity_sales_daily` AS
SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily`;


-- =====================================================================
-- =====================================================================
-- SECTION 13 — rpt_paid_media_wh_daily  (warehouse side of paid media)
-- =====================================================================
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_wh_daily`
PARTITION BY click_date
CLUSTER BY basis, channel
OPTIONS (description = "Paid media report, warehouse side. One row per click date x attribution view (F = first touch, L = last touch) x channel x subchannel x campaign x customer type. Purchases paid within 7 days of the click; free entries and subscription renewals excluded. Rebuilt nightly.")
AS
WITH a AS (
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
        CASE WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'rtg') THEN 'RTG'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'rmg|remarket') THEN 'RMG'
             WHEN ch.utm_campaign IS NULL THEN 'Unknown'
             ELSE 'PR' END
      WHEN ch.channel_group IN ('Google Ads', 'Microsoft Ads') THEN
        CASE WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'competitor|\|\s*competitor') THEN 'Competitor'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'generic|\|\s*generic') THEN 'Generic'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'brand|\|\s*brand') THEN 'Brand'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'pmax|performance|\|\s*pmax') THEN 'PMAX'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'demand|dgen|(|_|-)dg(|_|-|$)|\|\s*demand') THEN 'Demand Gen'
             ELSE 'Unknown' END
      ELSE NULL
    END AS subchannel,
    ch.utm_campaign AS campaign,
    CONCAT(IF(t.is_first_purchase, 'N', 'E'), IF(t.is_sub, 'S', 'O')) AS ct,
    t.customer_key, t.revenue_gbp, t.is_first_purchase
  FROM touches t
  LEFT JOIN ch ON ch.landing_url = t.url
  WHERE t.touch_ts IS NOT NULL
    AND DATE_DIFF(t.purchase_date, DATE(t.touch_ts, 'Europe/London'), DAY) BETWEEN 0 AND 7
)
SELECT
  click_date, basis, channel, subchannel, campaign, ct,
  COUNT(*) AS purchases,
  ROUND(SUM(revenue_gbp), 2) AS revenue_gbp,
  COUNT(DISTINCT IF(is_first_purchase, customer_key, NULL)) AS new_customers,
  DATE_DIFF(CURRENT_DATE('Europe/London'), click_date, DAY) < 7 AS maturing,
  CURRENT_TIMESTAMP() AS built_at
FROM t
WHERE click_date >= DATE '2026-09-01'
GROUP BY 1, 2, 3, 4, 5, 6;


-- =====================================================================
-- SECTION 14 — rpt_paid_media_page_daily & rpt_paid_media_page_status
-- Pre-calculated table for paid media reporting page. One row per day x basis x channel x sub.
-- =====================================================================
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
          WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'pr|prospecting|(_|^)pr(_|$)') THEN 'PR'
          ELSE 'Other'
        END
      WHEN ad_platform = 'google' OR channel_group IN ('Microsoft Ads', 'Bing') THEN
        CASE
          WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'competitor|\|\s*competitor') THEN 'Competitor'
          WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'generic|\|\s*generic') THEN 'Generic'
          WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'brand|\|\s*brand') THEN 'Brand'
          WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'pmax|performance|\|\s*pmax') THEN 'PMax'
          WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'demand|dgen|(|_|-)dg(|_|-|$)|\|\s*demand') THEN 'Demand Gen'
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
          WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'pr|prospecting|(_|^)pr(_|$)') THEN 'PR'
          ELSE 'Other' 
        END
      WHEN ch.channel_group IN ('Google Ads', 'Microsoft Ads') THEN
        CASE 
          WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'competitor|\|\s*competitor') THEN 'Competitor'
          WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'generic|\|\s*generic') THEN 'Generic'
          WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'brand|\|\s*brand') THEN 'Brand'
          WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'pmax|performance|\|\s*pmax') THEN 'PMax'
          WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'demand|dgen|(|_|-)dg(|_|-|$)|\|\s*demand') THEN 'Demand Gen'
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
  COALESCE(tc.ga4_sessions_reliable, COALESCE(s.d, w.d) < DATE '2026-10-19') AS ga4_reliable
FROM spend_both_bases s
FULL OUTER JOIN wh_agg w
  ON s.d = w.d
 AND s.basis = w.basis
 AND s.channel = w.channel
 AND s.sub = w.sub
LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
  ON COALESCE(s.d, w.d) = tc.d;

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

-- =====================================================================
-- SECTION 14 — rpt_visit_source_daily  (reloads last 3 days from finalized events_YYYYMMDD tables)
-- =====================================================================
DELETE FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
WHERE d BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY) AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY);

INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
  (d, consent, channel, source, medium, landings, page_views, ga4_complete, ga4_reliable, built_at)
WITH raw_events AS (
  SELECT
    PARSE_DATE('%Y%m%d', event_date) AS d,
    event_name,
    event_timestamp,
    user_pseudo_id,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS ga_session_id,
    CASE
      WHEN privacy_info.analytics_storage = 'Yes' THEN 'granted'
      WHEN privacy_info.analytics_storage = 'No' THEN 'denied'
      ELSE 'unknown'
    END AS consent,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location') AS page_location,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_referrer') AS page_referrer
  FROM `rh-ga4-491819.analytics_354198910.events_*`
  WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY))
                          AND FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY))
    AND _TABLE_SUFFIX NOT LIKE 'intraday_%'
    AND event_name IN ('session_start', 'page_view')
),
classified_events AS (
  SELECT
    *,
    NULLIF(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')), '') AS utm_source,
    NULLIF(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_medium=([^&#]+)')), '') AS utm_medium,
    CASE
      WHEN REGEXP_CONTAINS(LOWER(page_location), r'[?&](gclid|gbraid|wbraid|gad_source|gad_campaignid)=') THEN 'Google Ads'
      WHEN REGEXP_CONTAINS(LOWER(page_location), r'[?&]fbclid=')
           AND REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)') IS NULL
           AND REGEXP_EXTRACT(page_location, r'[?&]utm_medium=([^&#]+)') IS NULL THEN 'Meta (unclear paid/organic)'
      WHEN REGEXP_CONTAINS(IFNULL(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')),''), r'mentionme') THEN 'Referral (Mention Me)'
      WHEN LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')) = 'affiliate'
           OR REGEXP_CONTAINS(IFNULL(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_medium=([^&#]+)')),''), r'affiliate')
           OR REGEXP_CONTAINS(LOWER(page_location), r'[?&]clickref=') THEN 'Affiliate'
      WHEN LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')) = 'klaviyo'
           OR LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_medium=([^&#]+)')) = 'email' THEN 'Email'
      WHEN LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_medium=([^&#]+)')) = 'sms'
           OR REGEXP_CONTAINS(IFNULL(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')),''), r'attentive|sms') THEN 'SMS'
      WHEN (LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')) = 'google'
            AND REGEXP_CONTAINS(IFNULL(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_medium=([^&#]+)')),''), r'cpc|paid|ppc')) THEN 'Google Ads'
      WHEN REGEXP_CONTAINS(LOWER(page_location), r'[?&]fbclid=')
           OR REGEXP_CONTAINS(IFNULL(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')),''), r'^(facebook|fb|ig|instagram|meta)') THEN 'Meta'
      WHEN REGEXP_CONTAINS(LOWER(page_location), r'[?&]msclkid=')
           OR REGEXP_CONTAINS(IFNULL(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')),''), r'bing|microsoft') THEN 'Microsoft Ads'
      WHEN REGEXP_CONTAINS(LOWER(page_location), r'[?&]ttclid=')
           OR REGEXP_CONTAINS(IFNULL(LOWER(REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)')),''), r'tiktok') THEN 'TikTok'
      WHEN REGEXP_CONTAINS(LOWER(page_location), r'^https?://wowcher\.') THEN 'Wowcher'
      WHEN REGEXP_EXTRACT(page_location, r'[?&]utm_source=([^&#]+)') IS NOT NULL THEN 'Other tagged'
      WHEN page_referrer IS NOT NULL AND NOT REGEXP_CONTAINS(LOWER(page_referrer), r'rafflehouse\.com') THEN
        CASE
          WHEN REGEXP_CONTAINS(LOWER(page_referrer), r'google\.|bing\.|yahoo\.|duckduckgo\.|ecosia\.|baidu\.|yandex\.') THEN 'Organic Search'
          ELSE 'Referral'
        END
      ELSE 'Direct / untagged'
    END AS channel
  FROM raw_events
),
session_landing AS (
  SELECT
    user_pseudo_id,
    ga_session_id,
    consent,
    channel,
    utm_source,
    utm_medium
  FROM classified_events
  WHERE user_pseudo_id IS NOT NULL AND ga_session_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY user_pseudo_id, ga_session_id
    ORDER BY IF(event_name = 'session_start', 0, 1), event_timestamp ASC
  ) = 1
),
landings_agg AS (
  SELECT
    d,
    consent,
    channel,
    utm_source AS source,
    utm_medium AS medium,
    COUNT(*) AS landings
  FROM classified_events
  WHERE event_name = 'session_start'
  GROUP BY 1, 2, 3, 4, 5
),
page_views_attributed AS (
  SELECT
    ce.d,
    COALESCE(sl.consent, ce.consent) AS consent,
    COALESCE(sl.channel, ce.channel) AS channel,
    COALESCE(sl.utm_source, ce.utm_source) AS source,
    COALESCE(sl.utm_medium, ce.utm_medium) AS medium
  FROM classified_events ce
  LEFT JOIN session_landing sl
    ON ce.user_pseudo_id = sl.user_pseudo_id
   AND ce.ga_session_id = sl.ga_session_id
  WHERE ce.event_name = 'page_view'
),
page_views_agg AS (
  SELECT
    d,
    consent,
    channel,
    source,
    medium,
    COUNT(*) AS page_views
  FROM page_views_attributed
  GROUP BY 1, 2, 3, 4, 5
)
SELECT
  COALESCE(l.d, p.d) AS d,
  COALESCE(l.consent, p.consent) AS consent,
  COALESCE(l.channel, p.channel) AS channel,
  COALESCE(l.source, p.source) AS source,
  COALESCE(l.medium, p.medium) AS medium,
  COALESCE(l.landings, 0) AS landings,
  COALESCE(p.page_views, 0) AS page_views,
  TRUE AS ga4_complete,
  COALESCE(l.d, p.d) < DATE '2026-10-19' AS ga4_reliable,
  CURRENT_TIMESTAMP() AS built_at
FROM landings_agg l
FULL OUTER JOIN page_views_agg p
  ON l.d = p.d
 AND l.consent = p.consent
 AND l.channel = p.channel
 AND (l.source = p.source OR (l.source IS NULL AND p.source IS NULL))
 AND (l.medium = p.medium OR (l.medium IS NULL AND p.medium IS NULL));

-- =====================================================================
-- SECTION 14B — ORGANIC SOCIAL REPORTING TABLES
-- rpt_organic_social_weekly, rpt_organic_social_top_posts, rpt_organic_social_status
-- Rebuilt nightly after GA4 visit source steps
-- =====================================================================
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

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_top_posts`
(
  week_commencing DATE,
  platform STRING,
  post_rank INT64,
  post_id STRING,
  permalink STRING,
  post_type STRING,
  reach INT64,
  engagement INT64,
  engagement_rate NUMERIC
)
OPTIONS (description = "Top 10 posts per week ranked by engagement rate from organic social platforms. Empty pending Meta Graph API connection.");

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_status`
OPTIONS (description = "One-row status table for organic social reporting page showing sync and break dates.")
AS
SELECT
  CURRENT_TIMESTAMP() AS built_at,
  DATE '2026-10-19' AS tracking_changed_on,
  DATE '2026-10-19' AS cookie_banner_changed_on,
  DATE '2026-10-24' AS consent_setting_changed_on,
  DATE '2026-10-26' AS pricing_display_changed_on,
  [DATE '2026-10-19', DATE '2026-10-24', DATE '2026-10-26'] AS change_dates,
  FALSE AS meta_connected,
  CAST(NULL AS DATE) AS ig_data_to,
  CAST(NULL AS DATE) AS fb_data_to,
  (SELECT MAX(date) FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_organic_social_sessions_daily`) AS site_data_to,
  FALSE AS top_posts_available,
  'Meta Graph API feed is not connected (meta_connected = false); top posts requires Meta Page and Instagram permissions.' AS top_posts_unavailable_reason,
  CURRENT_TIMESTAMP() AS weekly_table_built_at,
  (SELECT MAX(week_commencing) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`) AS latest_week;

-- =====================================================================
-- SECTION 14b: fact_ga4_session_funnel (reloads last 3 days from finalized events_YYYYMMDD tables)
-- =====================================================================
DELETE FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_session_funnel`
WHERE session_date BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY)
                       AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY);

INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_session_funnel`
(
  session_date, user_pseudo_id, ga_session_id, device_category, channel,
  source_medium_raw, has_user_id, viewed_draw, added_basket, reached_basket,
  started_checkout, paid, entry_stage, furthest_stage, cycle_day, built_at
)
WITH events_extracted AS (
  SELECT
    event_timestamp,
    event_name,
    user_pseudo_id,
    user_id,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS ga_session_id,
    COALESCE(
      device.category,
      (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'device_category'),
      'unknown'
    ) AS device_category,
    collected_traffic_source.manual_source AS manual_source,
    collected_traffic_source.manual_medium AS manual_medium,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'source') AS param_source,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'medium') AS param_medium
  FROM `rh-ga4-491819.analytics_354198910.events_*`
  WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY))
                          AND FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY))
    AND _TABLE_SUFFIX NOT LIKE 'intraday_%'
),
raw_sessions AS (
  SELECT
    user_pseudo_id,
    ga_session_id,
    DATE(TIMESTAMP_MICROS(MIN(event_timestamp)), 'Europe/London') AS session_date,
    ANY_VALUE(device_category) AS device_category,
    LOWER(CONCAT(
      IFNULL(ANY_VALUE(manual_source), IFNULL(ANY_VALUE(param_source), '')),
      '|',
      IFNULL(ANY_VALUE(manual_medium), IFNULL(ANY_VALUE(param_medium), ''))
    )) AS src,
    LOGICAL_OR(user_id IS NOT NULL) AS has_user_id,
    LOGICAL_OR(event_name = 'product_view') AS viewed_draw,
    LOGICAL_OR(event_name = 'add_to_basket') AS added_basket,
    LOGICAL_OR(event_name IN ('add_to_basket', 'basket_view')) AS reached_basket,
    LOGICAL_OR(event_name = 'form_start') AS started_checkout,
    LOGICAL_OR(event_name = 'BoughtTickets') AS paid,
    ARRAY_AGG(
      CASE 
        WHEN event_name = 'product_view' THEN 'viewed_draw'
        WHEN event_name IN ('add_to_basket', 'basket_view') THEN 'reached_basket'
        WHEN event_name = 'form_start' THEN 'started_checkout'
        WHEN event_name = 'BoughtTickets' THEN 'paid'
        ELSE NULL
      END IGNORE NULLS
      ORDER BY event_timestamp ASC
      LIMIT 1
    )[SAFE_OFFSET(0)] AS entry_stage,
    CASE
      WHEN LOGICAL_OR(event_name = 'BoughtTickets') THEN 'paid'
      WHEN LOGICAL_OR(event_name = 'form_start') THEN 'started_checkout'
      WHEN LOGICAL_OR(event_name IN ('add_to_basket', 'basket_view')) THEN 'reached_basket'
      WHEN LOGICAL_OR(event_name = 'product_view') THEN 'viewed_draw'
      ELSE NULL
    END AS furthest_stage
  FROM events_extracted
  WHERE ga_session_id IS NOT NULL
  GROUP BY user_pseudo_id, ga_session_id
),
classified_sessions AS (
  SELECT
    s.session_date,
    s.user_pseudo_id,
    s.ga_session_id,
    s.device_category,
    CASE
      WHEN REGEXP_CONTAINS(s.src, r'(cpc|ppc|paidsearch)') THEN 'Paid search'
      WHEN REGEXP_CONTAINS(s.src, r'(paidsocial|paid_social)') THEN 'Paid social'
      WHEN REGEXP_CONTAINS(s.src, r'(klaviyo|email|newsletter)') THEN 'Email'
      WHEN REGEXP_CONTAINS(s.src, r'(facebook|instagram|tiktok|fb|ig|t\.co|twitter|youtube)') THEN 'Organic social'
      WHEN REGEXP_CONTAINS(s.src, r'\|organic\|') THEN 'Organic search'
      WHEN s.src IS NULL OR s.src = '||' OR REGEXP_CONTAINS(s.src, r'^\(direct\)') THEN 'Direct or unknown'
      ELSE 'Other' 
    END AS channel,
    s.src AS source_medium_raw,
    s.has_user_id,
    s.viewed_draw,
    s.added_basket,
    s.reached_basket,
    s.started_checkout,
    s.paid,
    s.entry_stage,
    s.furthest_stage
  FROM raw_sessions s
),
cycles AS (
  SELECT 
    end_date AS cs, 
    LEAD(end_date) OVER(ORDER BY end_date) AS next_cs
  FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle`
  WHERE is_closed = TRUE
)
SELECT 
  c.session_date,
  c.user_pseudo_id,
  c.ga_session_id,
  c.device_category,
  c.channel,
  c.source_medium_raw,
  c.has_user_id,
  c.viewed_draw,
  c.added_basket,
  c.reached_basket,
  c.started_checkout,
  c.paid,
  c.entry_stage,
  c.furthest_stage,
  DATE_DIFF(c.session_date, cyc.cs, DAY) + 1 AS cycle_day,
  CURRENT_TIMESTAMP() AS built_at
FROM classified_sessions c
LEFT JOIN cycles cyc 
  ON c.session_date > cyc.cs 
 AND (c.session_date <= cyc.next_cs OR cyc.next_cs IS NULL);

-- =====================================================================
-- SECTION 15: rpt_tracking_coverage_daily (rebuilt nightly from 2026-09-01)
-- =====================================================================
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
CLUSTER BY d
OPTIONS (description = 'Daily tracking coverage tracking orders with last-touch URL vs unassigned GA4 sessions and consent. Rebuilt nightly from 2026-09-01.')
AS
WITH dates AS (
  SELECT d
  FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY))) AS d
),
orders_daily AS (
  SELECT
    t.purchase_date AS d,
    COUNT(*) AS orders,
    COUNTIF(touch.last_touch_url IS NOT NULL AND TRIM(touch.last_touch_url) != '') AS orders_tracked
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
    ON t.transaction_id = touch.transaction_id
  WHERE t.purchase_date >= DATE '2026-09-01'
    AND NOT IFNULL(t.is_free_entry, FALSE)
    AND touch.attribution_basis != 'inherited_from_subscription_signup'
  GROUP BY 1
),
ga4_daily AS (
  SELECT
    date AS d,
    SUM(sessions) AS ga4_sessions,
    SUM(IF(ga4_channel_group = 'Unassigned', sessions, 0)) AS ga4_unassigned
  FROM `rh-ga4-491819.rh_curated_Bright_Test.ga4_source_daily`
  WHERE date >= DATE '2026-09-01'
  GROUP BY 1
),
funnel_daily AS (
  SELECT
    session_date AS d,
    COUNT(*) AS ga4_sessions
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_session_funnel`
  WHERE session_date >= DATE '2026-09-01'
  GROUP BY 1
),
visit_sources AS (
  SELECT
    d,
    SUM(landings) AS visit_landings,
    SUM(page_views) AS total_pv,
    SUM(IF(consent = 'granted', landings, 0)) AS granted_landings,
    SUM(IF(consent = 'granted', page_views, 0)) AS granted_pv
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
  WHERE d >= DATE '2026-09-01'
  GROUP BY 1
)
SELECT
  dt.d,
  COALESCE(o.orders, 0) AS orders,
  COALESCE(o.orders_tracked, 0) AS orders_tracked,
  ROUND(SAFE_DIVIDE(COALESCE(o.orders_tracked, 0), o.orders), 4) AS pct_tracked,
  COALESCE(fn.ga4_sessions, g.ga4_sessions, 0) AS ga4_sessions,
  COALESCE(g.ga4_unassigned, 0) AS ga4_unassigned,
  ROUND(SAFE_DIVIDE(COALESCE(g.ga4_unassigned, 0), COALESCE(fn.ga4_sessions, g.ga4_sessions, 0)), 4) AS pct_unassigned,
  ROUND(SAFE_DIVIDE(v.granted_landings, v.visit_landings), 4) AS consent_granted_share,
  IF(dt.d < DATE '2026-10-19', CAST(ROUND(SAFE_DIVIDE(
    v.total_pv,
    SAFE_DIVIDE(v.granted_pv, v.granted_landings)
  )) AS INT64), NULL) AS est_visits,
  (
    COALESCE(fn.ga4_sessions, g.ga4_sessions, 0) > 0
    AND SAFE_DIVIDE(COALESCE(g.ga4_unassigned, 0), COALESCE(fn.ga4_sessions, g.ga4_sessions, 0)) <= 0.20
    AND (SAFE_DIVIDE(v.granted_landings, v.visit_landings) IS NULL OR SAFE_DIVIDE(v.granted_landings, v.visit_landings) >= 0.50)
  ) AS ga4_sessions_reliable,
  COALESCE(fn.ga4_sessions, g.ga4_sessions, 0) > 0 AS ga4_complete,
  CURRENT_TIMESTAMP() AS built_at
FROM dates dt
LEFT JOIN orders_daily o ON dt.d = o.d
LEFT JOIN ga4_daily g ON dt.d = g.d
LEFT JOIN funnel_daily fn ON dt.d = fn.d
LEFT JOIN visit_sources v ON dt.d = v.d;

ALTER TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
  ALTER COLUMN ga4_sessions SET OPTIONS (description = 'distinct GA4 sessions (user_pseudo_id + ga_session_id)');

CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_tracking_coverage_daily` AS
SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`;


-- =====================================================================
-- SECTION 16 — dim_paid_media_entity, rpt_paid_media_entity_daily & rpt_paid_media_entity_status
-- Nightly rebuild of paid media report at entity grain (Jordan's report)
-- from 2026-09-01 to yesterday across first (F) and last (L) touch bases.
-- =====================================================================

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity`
OPTIONS (description = "Paid media entity dimension. One row per reporting entity (totals, groups, campaigns). Rebuilt nightly.")
AS
WITH
meta_names AS (
  SELECT campaign_id, ANY_VALUE(campaign_name) AS campaign_name
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
  GROUP BY 1
),
meta_summary AS (
  SELECT 
    campaign_id,
    SUM(spend_gbp) AS tot_spend,
    COUNT(DISTINCT IF(spend_gbp > 0 AND spend_date >= DATE '2026-09-01', spend_date, NULL)) AS n_spend_days,
    MIN(IF(spend_gbp > 0 AND spend_date >= DATE '2026-09-01', spend_date, NULL)) AS min_spend_date,
    MIN(spend_date) AS active_from,
    MAX(spend_date) AS active_to
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
  WHERE spend_date >= DATE '2026-09-01'
  GROUP BY 1
),
meta_qualified AS (
  SELECT
    s.campaign_id,
    n.campaign_name,
    CASE 
      WHEN REGEXP_CONTAINS(LOWER(n.campaign_name), r'rtg') THEN 'RTG'
      WHEN REGEXP_CONTAINS(LOWER(n.campaign_name), r'rmg|remarket') THEN 'RMG'
      ELSE 'PR'
    END AS grp,
    s.active_from,
    s.active_to,
    (s.n_spend_days = 1 AND s.min_spend_date = DATE '2026-09-10') AS is_legacy
  FROM meta_summary s
  JOIN meta_names n ON s.campaign_id = n.campaign_id
  WHERE s.tot_spend > 100
),
static_entities AS (
  SELECT 'ps' AS entity_id, 'Paid Social' AS name, 'total' AS kind, CAST(NULL AS STRING) AS parent_id, 'PR' AS grp, FALSE AS wordseed, TRUE AS in_parent_total, DATE '2026-09-01' AS active_from, DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY) AS active_to
  UNION ALL SELECT 'ps-pr', 'Paid Social PR', 'group', 'ps', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'ps-rmg', 'Paid Social RMG', 'group', 'ps', 'RMG', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'ps-rtg', 'Paid Social RTG', 'group', 'ps', 'RTG', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'ps-unk', 'Paid Social Unknown', 'group', 'ps', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'gb', 'Google and Bing', 'total', CAST(NULL AS STRING), 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'g-comp', 'Google Competitor', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'g-gen', 'Google Generic', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'g-brand', 'Google Brand', 'group', 'gb', 'PR', FALSE, FALSE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'g-dg', 'Google Demand Gen', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'g-pmax', 'Google PMAX', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'g-unk', 'Google Unknown', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'b-comp', 'Bing Competitor', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'b-gen', 'Bing Generic', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'b-unk', 'Bing Unknown', 'group', 'gb', 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
  UNION ALL SELECT 'aff', 'Affiliates', 'total', CAST(NULL AS STRING), 'PR', FALSE, TRUE, DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
),
campaign_entities AS (
  SELECT
    CONCAT('m', campaign_id) AS entity_id,
    campaign_name AS name,
    'campaign' AS kind,
    CONCAT('ps-', LOWER(grp)) AS parent_id,
    grp,
    REGEXP_CONTAINS(LOWER(campaign_name), r'wordseed') AS wordseed,
    TRUE AS in_parent_total,
    active_from,
    active_to
  FROM meta_qualified
  WHERE NOT is_legacy
)
SELECT * FROM static_entities
UNION ALL
SELECT * FROM campaign_entities;

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`
OPTIONS (description = "Status table for paid media reporting showing data currency and parameters. Rebuilt nightly.")
AS
WITH
raw_touches AS (
  SELECT
    t.transaction_id,
    DATE(t.purchase_at, 'Europe/London') AS pay_date_lon,
    t.is_first_purchase,
    COALESCE(h.ltv_3m, h.realised_value, IF(IFNULL(t.is_free_entry, FALSE), NUMERIC '0', t.revenue_gbp)) AS v3_raw,
    COALESCE(touch.first_touch_timestamp, touch.last_touch_timestamp) AS f_ts,
    touch.last_touch_timestamp AS l_ts
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
  JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
    ON t.transaction_id = touch.transaction_id
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
    ON t.subscription_key = s.subscription_key
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons` h
    ON t.customer_key = h.customer_key
  WHERE DATE(t.purchase_at, 'Europe/London') >= DATE '2026-06-01'
    AND COALESCE(t.payment_initiated_by, '') != 'recurring'
    AND t.customer_key != 'UNKNOWN_DELETED'
    AND (
      NOT IFNULL(t.is_free_entry, FALSE)
      OR (IFNULL(t.is_free_entry, FALSE) AND t.order_type = 'SUBSCRIPTION' AND s.created_by = 'special-offer')
    )
),
unioned_touches AS (
  SELECT pay_date_lon, is_first_purchase, v3_raw, f_ts AS touch_ts FROM raw_touches
  UNION ALL
  SELECT pay_date_lon, is_first_purchase, v3_raw, l_ts AS touch_ts FROM raw_touches
),
windowed_touches AS (
  SELECT is_first_purchase, v3_raw
  FROM unioned_touches
  WHERE touch_ts IS NOT NULL
    AND DATE_DIFF(pay_date_lon, DATE(touch_ts, 'Europe/London'), DAY) BETWEEN 0 AND 7
    AND DATE(touch_ts, 'Europe/London') >= DATE '2026-06-01'
),
p99_calc AS (
  SELECT
    ROUND(CAST(PERCENTILE_CONT(IF(is_first_purchase, CAST(v3_raw AS FLOAT64), NULL), 0.99) OVER () AS NUMERIC), 2) AS p99_v3
  FROM windowed_touches
  LIMIT 1
),
dates_calc AS (
  SELECT
    (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`) AS wh_to,
    (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`) AS meta_to,
    (SELECT MAX(segments_date) FROM `rh-ga4-491819.google_ads_transfer.ads_CampaignBasicStats_6102368044`) AS google_to,
    CAST(NULL AS DATE) AS bing_to,
    (SELECT MAX(`Period`) FROM `rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload` WHERE `Channel Group` = 'Affiliates') AS affiliates_to
)
SELECT
  CURRENT_TIMESTAMP() AS built_at,
  d.wh_to,
  d.meta_to,
  d.google_to,
  d.bing_to,
  d.affiliates_to,
  ARRAY(
    SELECT feed FROM UNNEST([
      STRUCT('Meta' AS feed, d.meta_to AS feed_date),
      STRUCT('Google' AS feed, d.google_to AS feed_date)
    ])
    WHERE feed_date IS NOT NULL AND DATE_DIFF(CURRENT_DATE('Europe/London'), feed_date, DAY) > 2
  ) AS stale_feeds,
  p99.p99_v3,
  DATE '2026-09-01' AS report_start,
  DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 6 DAY) AS mat_from,
  [DATE '2026-10-19', DATE '2026-10-24', DATE '2026-10-26'] AS change_dates
FROM p99_calc p99
CROSS JOIN dates_calc d;

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
PARTITION BY d
CLUSTER BY basis, entity_id
OPTIONS (description = "Paid media report at entity grain. Daily performance from 1 Sep 2026 to yesterday across first (F) and last (L) touch bases. Rolled up to groups and totals. Rebuilt nightly.")
AS
WITH
-- 1. Entities
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
),
meta_legacy AS (
  SELECT campaign_id
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
  WHERE spend_date >= DATE '2026-09-01'
  GROUP BY 1
  HAVING COUNT(DISTINCT IF(spend_gbp > 0 AND spend_date >= DATE '2026-09-01', spend_date, NULL)) = 1
     AND MIN(IF(spend_gbp > 0 AND spend_date >= DATE '2026-09-01', spend_date, NULL)) = DATE '2026-09-10'
),
-- 2. Full Grid: d x basis x entity_id
calendar AS (
  SELECT d
  FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY))) AS d
),
bases AS (
  SELECT 'F' AS basis UNION ALL SELECT 'L' AS basis
),
grid AS (
  SELECT c.d, b.basis, e.entity_id
  FROM calendar c
  CROSS JOIN bases b
  CROSS JOIN entities e
),
-- 3. Platform Data
-- 3a. Meta
meta_base AS (
  SELECT 
    spend_date AS d,
    campaign_id,
    ROUND(SUM(spend_gbp), 2) AS spend,
    ROUND(SUM(platform_conversions), 2) AS plat_purchases,
    ROUND(SUM(platform_conversion_value_gbp), 2) AS plat_rev,
    SUM(impressions) AS impr,
    SUM(clicks) AS clicks,
    SUM(reach) AS reach
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
  WHERE spend_date >= DATE '2026-09-01'
  GROUP BY 1, 2
),
meta_names AS (
  SELECT campaign_id, ANY_VALUE(campaign_name) AS campaign_name
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
  GROUP BY 1
),
meta_classified AS (
  SELECT 
    m.d,
    m.campaign_id,
    m.spend,
    m.plat_purchases,
    m.plat_rev,
    m.impr,
    m.clicks,
    m.reach,
    mc.entity_id AS qualified_ent_id,
    CASE 
      WHEN leg.campaign_id IS NOT NULL THEN 'ps-unk'
      WHEN REGEXP_CONTAINS(LOWER(n.campaign_name), r'rtg') THEN 'ps-rtg'
      WHEN REGEXP_CONTAINS(LOWER(n.campaign_name), r'rmg|remarket') THEN 'ps-rmg'
      ELSE 'ps-pr'
    END AS group_ent_id
  FROM meta_base m
  JOIN meta_names n ON m.campaign_id = n.campaign_id
  LEFT JOIN meta_camps mc ON m.campaign_id = mc.cid
  LEFT JOIN meta_legacy leg ON m.campaign_id = leg.campaign_id
),
meta_groups_and_camps AS (
  -- Group (ps-pr, ps-rmg, ps-rtg, ps-unk)
  SELECT d, group_ent_id AS entity_id, SUM(spend) AS spend, SUM(plat_purchases) AS plat_purchases, SUM(plat_rev) AS plat_rev, SUM(impr) AS impr, SUM(clicks) AS clicks, CAST(NULL AS INT64) AS reach FROM meta_classified GROUP BY 1, 2
  UNION ALL
  -- Campaign m<cid> (reach populated only at campaign level)
  SELECT d, qualified_ent_id AS entity_id, spend, plat_purchases, plat_rev, impr, clicks, reach FROM meta_classified WHERE qualified_ent_id IS NOT NULL
),
meta_total AS (
  -- Total ps strictly defined as sum of ps-* groups
  SELECT d, 'ps' AS entity_id, ROUND(SUM(spend), 2) AS spend, ROUND(SUM(plat_purchases), 2) AS plat_purchases, ROUND(SUM(plat_rev), 2) AS plat_rev, SUM(impr) AS impr, SUM(clicks) AS clicks, CAST(NULL AS INT64) AS reach
  FROM meta_groups_and_camps
  WHERE entity_id IN ('ps-pr', 'ps-rmg', 'ps-rtg', 'ps-unk')
  GROUP BY 1
),
-- 3b. Google
google_names AS (
  SELECT campaign_id, _DATA_DATE AS d,
         ANY_VALUE(campaign_name) AS campaign_name
  FROM `rh-ga4-491819.google_ads_transfer.ads_Campaign_6102368044`
  GROUP BY 1, 2
),
google_first_name AS (
  SELECT campaign_id,
         ARRAY_AGG(campaign_name ORDER BY _DATA_DATE LIMIT 1)[OFFSET(0)] AS campaign_name
  FROM `rh-ga4-491819.google_ads_transfer.ads_Campaign_6102368044`
  GROUP BY 1
),
google_base AS (
  SELECT
    s.segments_date AS d,
    COALESCE(n.campaign_name, f.campaign_name) AS campaign_name,
    ROUND(SUM(CAST(s.metrics_cost_micros AS NUMERIC) / 1000000), 2) AS spend,
    ROUND(SUM(s.metrics_conversions), 2) AS plat_purchases,
    ROUND(SUM(CAST(s.metrics_conversions_value AS NUMERIC)), 2) AS plat_rev,
    SUM(s.metrics_impressions) AS impr,
    SUM(s.metrics_clicks) AS clicks
  FROM `rh-ga4-491819.google_ads_transfer.ads_CampaignBasicStats_6102368044` s
  LEFT JOIN google_names n      ON n.campaign_id = s.campaign_id AND n.d = s.segments_date
  LEFT JOIN google_first_name f ON f.campaign_id = s.campaign_id
  WHERE s.segments_date >= DATE '2026-09-01'
  GROUP BY 1, 2
),
google_groups AS (
  SELECT
    d,
    CASE 
      WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'competitor') THEN 'g-comp'
      WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'generic') THEN 'g-gen'
      WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'brand') THEN 'g-brand'
      WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'pmax|performance') THEN 'g-pmax'
      WHEN REGEXP_CONTAINS(LOWER(campaign_name), r'demand|dgen|( |_|-)dg( |_|-|$)') THEN 'g-dg'
      ELSE 'g-unk'
    END AS entity_id,
    ROUND(SUM(spend), 2) AS spend, 
    ROUND(SUM(plat_purchases), 2) AS plat_purchases, 
    ROUND(SUM(plat_rev), 2) AS plat_rev, 
    SUM(impr) AS impr, 
    SUM(clicks) AS clicks,
    CAST(NULL AS INT64) AS reach
  FROM google_base
  GROUP BY 1, 2
),
gb_total AS (
  -- Total gb strictly defined as sum of children in_parent_total (g-* except brand, plus b-*)
  SELECT d, 'gb' AS entity_id, ROUND(SUM(spend), 2) AS spend, ROUND(SUM(plat_purchases), 2) AS plat_purchases, ROUND(SUM(plat_rev), 2) AS plat_rev, SUM(impr) AS impr, SUM(clicks) AS clicks, CAST(NULL AS INT64) AS reach
  FROM google_groups
  WHERE entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-unk') -- b-* has 0 spend
  GROUP BY 1
),
-- 3c. Affiliates
aff_base AS (
  SELECT
    `Period` AS d,
    'aff' AS entity_id,
    ROUND(SUM(CAST(`Cost` AS NUMERIC)), 2) AS spend,
    0.0 AS plat_purchases,
    0.0 AS plat_rev,
    0 AS impr,
    0 AS clicks,
    CAST(NULL AS INT64) AS reach
  FROM `rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload`
  WHERE `Period` >= DATE '2026-09-01'
    AND LOWER(`Channel Group`) LIKE '%affiliate%'
  GROUP BY 1
),
platform_all AS (
  SELECT * FROM meta_groups_and_camps
  UNION ALL SELECT * FROM meta_total
  UNION ALL SELECT * FROM google_groups
  UNION ALL SELECT * FROM gb_total
  UNION ALL SELECT * FROM aff_base
),
platform_daily AS (
  SELECT
    d,
    entity_id,
    ROUND(SUM(spend), 2) AS spend,
    ROUND(SUM(plat_purchases), 2) AS plat_purchases,
    ROUND(SUM(plat_rev), 2) AS plat_rev,
    SUM(impr) AS impr,
    SUM(clicks) AS clicks,
    MAX(reach) AS reach
  FROM platform_all
  GROUP BY 1, 2
),

-- 4. Warehouse Data
raw_touches AS (
  SELECT
    t.transaction_id,
    DATE(t.purchase_at, 'Europe/London') AS pay_date_lon,
    t.customer_key,
    IF(IFNULL(t.is_free_entry, FALSE), NUMERIC '0', t.revenue_gbp) AS revenue_gbp,
    t.is_first_purchase,
    (t.is_subscription_payment OR t.order_type = 'SUBSCRIPTION') AS is_sub,
    IFNULL(t.is_free_entry, FALSE) AS is_free_entry,
    t.subscription_key,
    COALESCE(
      m.tier_band,
      CASE
        WHEN s.monthly_value_gbp IN (10, 15) THEN 'Low'
        WHEN s.monthly_value_gbp IN (20, 25, 30) THEN 'Mid'
        WHEN s.monthly_value_gbp IN (35, 50) THEN 'High'
        WHEN s.monthly_value_gbp IN (55, 75) THEN 'Very High'
        ELSE NULL
      END
    ) AS tier_band,
    COALESCE(h.ltv_3m, h.realised_value, IF(IFNULL(t.is_free_entry, FALSE), NUMERIC '0', t.revenue_gbp)) AS v3_raw,
    -- Basis F: first touch with fallback to last touch
    COALESCE(touch.first_touch_url, touch.last_touch_url) AS f_url,
    COALESCE(touch.first_touch_timestamp, touch.last_touch_timestamp) AS f_ts,
    -- Basis L: last touch
    touch.last_touch_url AS l_url,
    touch.last_touch_timestamp AS l_ts
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
  JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
    ON t.transaction_id = touch.transaction_id
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
    ON t.subscription_key = s.subscription_key
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model` m
    ON s.subscription_model_key = m.subscription_model_key
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons` h
    ON t.customer_key = h.customer_key
  WHERE DATE(t.purchase_at, 'Europe/London') >= DATE '2026-06-01'
    AND COALESCE(t.payment_initiated_by, '') != 'recurring'
    AND t.customer_key != 'UNKNOWN_DELETED'
    AND (
      NOT IFNULL(t.is_free_entry, FALSE)
      OR (IFNULL(t.is_free_entry, FALSE) AND t.order_type = 'SUBSCRIPTION' AND s.created_by = 'special-offer')
    )
),
unioned_touches AS (
  SELECT 'F' AS basis, r.* EXCEPT(f_url, f_ts, l_url, l_ts), f_url AS url, f_ts AS touch_ts FROM raw_touches r
  UNION ALL
  SELECT 'L' AS basis, r.* EXCEPT(f_url, f_ts, l_url, l_ts), l_url AS url, l_ts AS touch_ts FROM raw_touches r
),
windowed_touches AS (
  SELECT 
    t.*,
    DATE(t.touch_ts, 'Europe/London') AS click_date,
    LOWER(REPLACE(t.url, '%3F', '?')) AS clean_url,
    NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(t.url, '%3F', '?')), r'[?&]utm_source=([^&#]*)'), '') AS utm_source,
    NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(t.url, '%3F', '?')), r'[?&]utm_medium=([^&#]*)'), '') AS utm_medium,
    NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(t.url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '') AS utm_camp,
    NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(t.url, '%3F', '?')), r'[?&]gad_campaignid=([^&#]*)'), '') AS gad_cid,
    NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(t.url, '%3F', '?')), r'[?&]tw_campaign=([^&#]*)'), '') AS tw_cid
  FROM unioned_touches t
  WHERE t.touch_ts IS NOT NULL
    AND DATE_DIFF(t.pay_date_lon, DATE(t.touch_ts, 'Europe/London'), DAY) BETWEEN 0 AND 7
    AND DATE(t.touch_ts, 'Europe/London') >= DATE '2026-06-01'
),
p99_calc AS (
  SELECT
    ROUND(CAST(PERCENTILE_CONT(IF(is_first_purchase, CAST(v3_raw AS FLOAT64), NULL), 0.99) OVER () AS NUMERIC), 2) AS p99_v3
  FROM windowed_touches
  LIMIT 1
),
filtered_touches AS (
  SELECT *
  FROM windowed_touches
  WHERE click_date >= DATE '2026-09-01'
    AND click_date <= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
),
latest_gads_camps AS (
  SELECT DISTINCT CAST(campaign_id AS STRING) AS campaign_id, campaign_name
  FROM `rh-ga4-491819.google_ads_transfer.ads_Campaign_6102368044`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY campaign_id ORDER BY _DATA_DATE DESC) = 1
),
latest_gads_by_name AS (
  SELECT LOWER(campaign_name) AS cname_low, ANY_VALUE(campaign_name) AS campaign_name
  FROM latest_gads_camps
  GROUP BY 1
),
classified_touches AS (
  SELECT
    t.*,
    -- Channel classification matching assemble2.py / vw_channel_perf_daily_calc
    CASE
      WHEN REGEXP_CONTAINS(t.clean_url, r'clickref|utm_medium=[^&#]*affiliate|utm_source=(affiliate|impact|partnerize)') THEN 'Affiliates'
      WHEN REGEXP_CONTAINS(t.clean_url, r'utm_source=klaviyo|utm_klaviyo_id|_kx=') THEN 'Email'
      WHEN REGEXP_CONTAINS(t.clean_url, r'utm_source=attentive|utm_medium=sms') THEN 'SMS'
      WHEN REGEXP_CONTAINS(t.clean_url, r'utm_source=mentionme') THEN 'Referral'
      WHEN REGEXP_CONTAINS(t.clean_url, r'msclkid|utm_source=bing') THEN 'Bing'
      WHEN REGEXP_CONTAINS(t.clean_url, r'gclid|gbraid|wbraid|gad_campaignid|utm_source=google') THEN 'Google'
      WHEN (
        REGEXP_CONTAINS(t.clean_url, r'utm_source=(meta|fb|ig|facebook|instagram|meta_fb|meta_ig|an|th)|tw_source=(fb|ig)|fbclid')
        AND (
          t.utm_camp IS NOT NULL
          OR REGEXP_CONTAINS(t.clean_url, r'utm_medium=(cpc|paid|paidsocial|cpm|video|feed|story|reels)')
          OR REGEXP_CONTAINS(t.clean_url, r'tw_')
          OR REGEXP_CONTAINS(t.clean_url, r'utm_id=')
          OR (REGEXP_CONTAINS(t.clean_url, r'fbclid=') AND t.utm_source IS NULL)
        )
      ) THEN 'Paid Social'
      ELSE 'Other'
    END AS channel,
    -- Customer Type: NO, NS, ER, ES
    IF(t.is_sub, IF(t.is_first_purchase, 'NS', 'ES'), IF(t.is_first_purchase, 'NO', 'ER')) AS ct,
    COALESCE(
      IF(REGEXP_CONTAINS(t.tw_cid, r'^1202[0-9]{10,20}$'), t.tw_cid, NULL),
      IF(REGEXP_CONTAINS(t.utm_camp, r'^1202[0-9]{10,20}$'), t.utm_camp, NULL)
    ) AS extracted_meta_cid,
    gm.subchannel AS gads_subchannel_override,
    COALESCE(gc1.campaign_name, gc2.campaign_name, gc2n.campaign_name, gc3.campaign_name, gc3n.campaign_name, t.utm_camp) AS extracted_google_camp_name
  FROM filtered_touches t
  LEFT JOIN latest_gads_camps gc1 ON t.gad_cid = gc1.campaign_id
  LEFT JOIN latest_gads_camps gc2 ON t.tw_cid = gc2.campaign_id
  LEFT JOIN latest_gads_by_name gc2n ON t.tw_cid = gc2n.cname_low
  LEFT JOIN latest_gads_camps gc3 ON t.utm_camp = gc3.campaign_id
  LEFT JOIN latest_gads_by_name gc3n ON t.utm_camp = gc3n.cname_low
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.ref_google_campaign_id_map` gm ON t.gad_cid = gm.campaign_id
),
with_sub AS (
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
),
unnested AS (
  SELECT
    w.basis,
    w.click_date AS d,
    w.channel,
    ent_id,
    w.transaction_id,
    w.customer_key,
    w.revenue_gbp,
    w.is_first_purchase,
    w.is_free_entry,
    w.ct,
    w.tier_band,
    w.v3_raw
  FROM with_entity_ids w,
  UNNEST(w.entity_ids) AS ent_id
),
wh_daily AS (
  SELECT
    u.d,
    u.basis,
    u.ent_id AS entity_id,
    COUNT(*) AS wh_n,
    COUNTIF(u.is_free_entry) AS wh_nt,
    ROUND(SUM(u.revenue_gbp), 2) AS wh_rev,
    COUNTIF(u.revenue_gbp >= 200) AS n200,
    ROUND(SUM(IF(u.revenue_gbp >= 200, u.revenue_gbp, 0)), 2) AS r200,
    ROUND(MAX(u.revenue_gbp), 2) AS max_order,
    COUNTIF(u.is_first_purchase) AS new_cust,
    ROUND(SUM(IF(u.is_first_purchase, LEAST(u.v3_raw, p.p99_v3), 0)), 2) AS v3,
    COUNTIF(u.ct = 'NO') AS no_n,
    ROUND(SUM(IF(u.ct = 'NO', u.revenue_gbp, 0)), 2) AS no_rev,
    COUNTIF(u.ct = 'NS') AS ns_n,
    ROUND(SUM(IF(u.ct = 'NS', u.revenue_gbp, 0)), 2) AS ns_rev,
    COUNTIF(u.ct = 'ER') AS er_n,
    ROUND(SUM(IF(u.ct = 'ER', u.revenue_gbp, 0)), 2) AS er_rev,
    COUNTIF(u.ct = 'ES') AS es_n,
    ROUND(SUM(IF(u.ct = 'ES', u.revenue_gbp, 0)), 2) AS es_rev,
    COUNTIF(u.ct IN ('NS', 'ES') AND u.tier_band = 'Low') AS tier_low,
    COUNTIF(u.ct IN ('NS', 'ES') AND u.tier_band = 'Mid') AS tier_mid,
    COUNTIF(u.ct IN ('NS', 'ES') AND u.tier_band = 'High') AS tier_high,
    COUNTIF(u.ct IN ('NS', 'ES') AND u.tier_band = 'Very High') AS tier_vhigh,
    -- Google adjusted revenue: subscription starts revalued at £50/£80/£105/£110 by tier band plus actual non-sub revenue for Google rows
    ROUND(SUM(
      CASE 
        WHEN u.channel = 'Google' AND u.ent_id IN ('gb', 'g-comp', 'g-gen', 'g-brand', 'g-dg', 'g-pmax', 'g-unk') THEN
          CASE 
            WHEN u.ct IN ('NS', 'ES') AND u.tier_band = 'Low' THEN NUMERIC '50.0'
            WHEN u.ct IN ('NS', 'ES') AND u.tier_band = 'Mid' THEN NUMERIC '80.0'
            WHEN u.ct IN ('NS', 'ES') AND u.tier_band = 'High' THEN NUMERIC '105.0'
            WHEN u.ct IN ('NS', 'ES') AND u.tier_band = 'Very High' THEN NUMERIC '110.0'
            ELSE u.revenue_gbp
          END
        ELSE NUMERIC '0.0'
      END
    ), 2) AS adj_rev
  FROM unnested u
  CROSS JOIN p99_calc p
  GROUP BY 1, 2, 3
)
SELECT
  g.d,
  g.basis,
  g.entity_id,
  -- Platform
  COALESCE(p.spend, 0.0) AS spend,
  ROUND(COALESCE(p.spend, 0.0) * NUMERIC '1.2', 2) AS spend_inc_vat,
  COALESCE(p.plat_purchases, 0.0) AS plat_purchases,
  COALESCE(p.plat_rev, 0.0) AS plat_rev,
  COALESCE(p.impr, 0) AS impr,
  COALESCE(p.clicks, 0) AS clicks,
  p.reach,
  -- Warehouse
  COALESCE(w.wh_n, 0) AS wh_n,
  COALESCE(w.wh_nt, 0) AS wh_nt,
  COALESCE(w.wh_rev, 0.0) AS wh_rev,
  COALESCE(w.n200, 0) AS n200,
  COALESCE(w.r200, 0.0) AS r200,
  COALESCE(w.max_order, 0.0) AS max_order,
  COALESCE(w.new_cust, 0) AS new_cust,
  COALESCE(w.v3, 0.0) AS v3,
  -- Customer type
  COALESCE(w.no_n, 0) AS no_n,
  COALESCE(w.no_rev, 0.0) AS no_rev,
  COALESCE(w.ns_n, 0) AS ns_n,
  COALESCE(w.ns_rev, 0.0) AS ns_rev,
  COALESCE(w.er_n, 0) AS er_n,
  COALESCE(w.er_rev, 0.0) AS er_rev,
  COALESCE(w.es_n, 0) AS es_n,
  COALESCE(w.es_rev, 0.0) AS es_rev,
  -- Plan tiers
  COALESCE(w.tier_low, 0) AS tier_low,
  COALESCE(w.tier_mid, 0) AS tier_mid,
  COALESCE(w.tier_high, 0) AS tier_high,
  COALESCE(w.tier_vhigh, 0) AS tier_vhigh,
  -- Adjusted
  COALESCE(w.adj_rev, 0.0) AS adj_rev,
  -- Flags
  DATE_DIFF(CURRENT_DATE('Europe/London'), g.d, DAY) < 7 AS maturing,
  CURRENT_TIMESTAMP() AS built_at
FROM grid g
LEFT JOIN platform_daily p ON g.d = p.d AND g.entity_id = p.entity_id
LEFT JOIN wh_daily w ON g.d = w.d AND g.basis = w.basis AND g.entity_id = w.entity_id;


-- =====================================================================
-- SECTION 17 — rpt_trial_conversion & vw_trial_status
--
-- Free-trial definition & business owner decisions:
--  1. Genuine trial = customer's first special-offer subscription (rn = 1)
--     with a £0 ACCEPTED transaction (first_free_at), excluding PENDING_BASKET /
--     IN_PAYMENT_PROCESS and excluding exploit accounts with >= 10 trial subscriptions
--     (backstop: '66d231300ff155003e747fcc', '69dfaa9740447116ffe02052',
--      '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022',
--      '64b5d23f79c5fe003eefd800'). Identified by s.is_special_offer_trial.
--  2. Date-based status (never uses WooCommerce fs.status):
--     - 'converted' after 1st paid renewal (paid_tx_count >= 1)
--     - 'active' while inside the first trial cycle:
--       CURRENT_DATE('Europe/London') < DATE_ADD(DATE_TRUNC(DATE(first_free_at), MONTH), INTERVAL 1 MONTH)
--       and no paid transaction yet
--     - 'lapsed' otherwise (failed-payment or paused rows with no paid tx are lapsed).
--  3. Both conversion measures per cohort x tier:
--     - 1st paid payment (LTV / EFV multiplier uses trailing mature 1st-payment rate by tier,
--       with < 30 trials fallback to overall mature 1st-payment rate)
--     - 3rd paid payment (headline KPI in rpt_trial_conversion, with mature_3rd requiring
--       cohort window + 2 payment cycles = 3 months).
-- =====================================================================
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`
CLUSTER BY cohort, tier
OPTIONS (description = "Free-trial conversion by trial start month (cohort of first_free_at) and plan tier: 1st-payment and 3rd-payment conversion rates, maturity flags, and trailing mature conversion rates by tier (fallback to overall rate when < 30 trials). Rebuilt nightly.")
AS
WITH sub_tx AS (
  SELECT
    subscription_key,
    MIN(IF(is_free_entry, purchase_at, NULL)) AS first_free_at,
    COUNTIF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE)) AS paid_tx_count,
    ARRAY_AGG(
      IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), purchase_date, NULL)
      IGNORE NULLS
      ORDER BY purchase_at, transaction_id
    ) AS paid_dates
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE subscription_key IS NOT NULL
  GROUP BY subscription_key
),
trials AS (
  SELECT
    s.subscription_key,
    s.customer_key,
    DATE(tx.first_free_at) AS start_date,
    DATE_TRUNC(DATE(tx.first_free_at), MONTH) AS cohort,
    DATE(s.next_purchase_date) AS next_purchase_date,
    s.monthly_value_gbp,
    COALESCE(
      m.tier_name,
      CASE
        WHEN s.monthly_value_gbp IN (10, 15) THEN 'Bronze'
        WHEN s.monthly_value_gbp IN (20, 25, 30) THEN 'Silver'
        WHEN s.monthly_value_gbp IN (35, 50) THEN 'Gold'
        WHEN s.monthly_value_gbp IN (55, 75) THEN 'Platinum'
        ELSE 'Other'
      END
    ) AS tier,
    COALESCE(
      m.tier_band,
      CASE
        WHEN s.monthly_value_gbp < 20 THEN 'Low'
        WHEN s.monthly_value_gbp < 40 THEN 'Mid'
        WHEN s.monthly_value_gbp < 60 THEN 'High'
        ELSE 'Very High'
      END
    ) AS tier_band,
    COALESCE(tx.paid_tx_count, 0) AS paid_tx_count,
    COALESCE(tx.paid_tx_count, 0) >= 1 AS converted_1st,
    COALESCE(tx.paid_tx_count, 0) >= 3 AS converted_3rd,
    tx.paid_dates[SAFE_OFFSET(0)] AS first_paid_date,
    tx.paid_dates[SAFE_OFFSET(2)] AS third_paid_date,
    CASE
      WHEN COALESCE(tx.paid_tx_count, 0) >= 1 THEN 'converted'
      WHEN CURRENT_DATE('Europe/London') < DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at), MONTH), INTERVAL 1 MONTH) THEN 'active'
      ELSE 'lapsed'
    END AS trial_status,
    IF(
      COALESCE(tx.paid_tx_count, 0) >= 1,
      GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(0)], DATE(tx.first_free_at), DAY), 0),
      NULL
    ) AS days_to_1st,
    IF(
      COALESCE(tx.paid_tx_count, 0) >= 3,
      GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(2)], DATE(tx.first_free_at), DAY), 0),
      NULL
    ) AS days_to_3rd
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
  JOIN sub_tx tx
    ON s.subscription_key = tx.subscription_key
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model` m
    ON s.subscription_model_key = m.subscription_model_key
  WHERE s.is_special_offer_trial
    AND tx.first_free_at IS NOT NULL
),
cohort_tier_med AS (
  SELECT DISTINCT
    cohort,
    tier,
    PERCENTILE_CONT(CAST(days_to_1st AS FLOAT64), 0.5) OVER (PARTITION BY cohort, tier) AS median_days_1st_raw,
    PERCENTILE_CONT(CAST(days_to_3rd AS FLOAT64), 0.5) OVER (PARTITION BY cohort, tier) AS median_days_3rd_raw
  FROM trials
),
cohort_tier_agg AS (
  SELECT
    t.cohort,
    t.tier,
    ANY_VALUE(t.tier_band) AS tier_band,
    COUNT(*) AS trials,
    COUNTIF(t.trial_status = 'active') AS active_trials,
    COUNTIF(t.trial_status = 'lapsed') AS lapsed_trials,
    COUNTIF(t.converted_1st) AS converted_1st,
    ROUND(CAST(SAFE_DIVIDE(COUNTIF(t.converted_1st), COUNT(*)) AS NUMERIC), 4) AS conversion_rate_1st,
    ROUND(CAST(AVG(t.days_to_1st) AS NUMERIC), 2) AS mean_days_1st,
    COUNTIF(t.converted_3rd) AS converted_3rd,
    ROUND(CAST(SAFE_DIVIDE(COUNTIF(t.converted_3rd), COUNT(*)) AS NUMERIC), 4) AS conversion_rate_3rd,
    ROUND(CAST(AVG(t.days_to_3rd) AS NUMERIC), 2) AS mean_days_3rd,
    CURRENT_DATE('Europe/London') >= DATE_ADD(DATE_ADD(t.cohort, INTERVAL 1 MONTH), INTERVAL 15 DAY) AS mature_1st,
    CURRENT_DATE('Europe/London') >= DATE_ADD(DATE_ADD(t.cohort, INTERVAL 3 MONTH), INTERVAL 15 DAY) AS mature_3rd
  FROM trials t
  GROUP BY t.cohort, t.tier
),
trailing_tier AS (
  SELECT
    tier,
    SUM(IF(mature_1st, trials, 0)) AS trailing_mature_trials_1st,
    SUM(IF(mature_1st, converted_1st, 0)) AS trailing_mature_converted_1st,
    SAFE_DIVIDE(SUM(IF(mature_1st, converted_1st, 0)), SUM(IF(mature_1st, trials, 0))) AS trailing_tier_cvr_1st,
    SUM(IF(mature_3rd, trials, 0)) AS trailing_mature_trials_3rd,
    SUM(IF(mature_3rd, converted_3rd, 0)) AS trailing_mature_converted_3rd,
    SAFE_DIVIDE(SUM(IF(mature_3rd, converted_3rd, 0)), SUM(IF(mature_3rd, trials, 0))) AS trailing_tier_cvr_3rd
  FROM cohort_tier_agg
  GROUP BY tier
),
trailing_overall AS (
  SELECT
    SUM(IF(mature_1st, trials, 0)) AS overall_mature_trials_1st,
    SUM(IF(mature_1st, converted_1st, 0)) AS overall_mature_converted_1st,
    SAFE_DIVIDE(SUM(IF(mature_1st, converted_1st, 0)), SUM(IF(mature_1st, trials, 0))) AS trailing_overall_cvr_1st,
    SUM(IF(mature_3rd, trials, 0)) AS overall_mature_trials_3rd,
    SUM(IF(mature_3rd, converted_3rd, 0)) AS overall_mature_converted_3rd,
    SAFE_DIVIDE(SUM(IF(mature_3rd, converted_3rd, 0)), SUM(IF(mature_3rd, trials, 0))) AS trailing_overall_cvr_3rd
  FROM cohort_tier_agg
)
SELECT
  a.cohort,
  a.tier,
  a.tier_band,
  a.trials,
  a.active_trials,
  a.lapsed_trials,
  -- 1st paid payment conversion (LTV / EFV multiplier basis)
  a.converted_1st,
  a.conversion_rate_1st,
  ROUND(CAST(m.median_days_1st_raw AS NUMERIC), 2) AS median_days_1st,
  a.mean_days_1st,
  a.mature_1st,
  -- 3rd paid payment conversion (Headline KPI basis)
  a.converted_3rd,
  a.conversion_rate_3rd,
  ROUND(CAST(m.median_days_3rd_raw AS NUMERIC), 2) AS median_days_3rd,
  a.mean_days_3rd,
  a.mature_3rd,
  a.mature_3rd AS mature,
  -- Trailing mature 1st-payment conversion rates (used by customer_ltv_horizons)
  COALESCE(tt.trailing_mature_trials_1st, 0) AS trailing_mature_trials_1st,
  COALESCE(tt.trailing_mature_converted_1st, 0) AS trailing_mature_converted_1st,
  ROUND(CAST(tt.trailing_tier_cvr_1st AS NUMERIC), 4) AS trailing_tier_cvr_1st,
  ROUND(CAST(to_all.trailing_overall_cvr_1st AS NUMERIC), 4) AS trailing_overall_cvr_1st,
  ROUND(
    CAST(
      IF(COALESCE(tt.trailing_mature_trials_1st, 0) >= 30, tt.trailing_tier_cvr_1st, to_all.trailing_overall_cvr_1st)
      AS NUMERIC
    ),
    4
  ) AS trailing_conversion_rate_1st,
  -- Trailing mature 3rd-payment conversion rates (headline KPI)
  COALESCE(tt.trailing_mature_trials_3rd, 0) AS trailing_mature_trials_3rd,
  COALESCE(tt.trailing_mature_converted_3rd, 0) AS trailing_mature_converted_3rd,
  ROUND(CAST(tt.trailing_tier_cvr_3rd AS NUMERIC), 4) AS trailing_tier_cvr_3rd,
  ROUND(CAST(to_all.trailing_overall_cvr_3rd AS NUMERIC), 4) AS trailing_overall_cvr_3rd,
  ROUND(
    CAST(
      IF(COALESCE(tt.trailing_mature_trials_3rd, 0) >= 30, tt.trailing_tier_cvr_3rd, to_all.trailing_overall_cvr_3rd)
      AS NUMERIC
    ),
    4
  ) AS trailing_conversion_rate_3rd,
  -- Backwards-compatible aliases
  a.converted_1st AS converted,
  a.conversion_rate_1st AS conversion_rate,
  ROUND(CAST(m.median_days_1st_raw AS NUMERIC), 2) AS median_days,
  a.mean_days_1st AS mean_days,
  ROUND(
    CAST(
      IF(COALESCE(tt.trailing_mature_trials_1st, 0) >= 30, tt.trailing_tier_cvr_1st, to_all.trailing_overall_cvr_1st)
      AS NUMERIC
    ),
    4
  ) AS trailing_90d_conversion_rate,
  CURRENT_TIMESTAMP() AS built_at
FROM cohort_tier_agg a
LEFT JOIN cohort_tier_med m
  ON a.cohort = m.cohort AND a.tier = m.tier
LEFT JOIN trailing_tier tt
  ON a.tier = tt.tier
CROSS JOIN trailing_overall to_all;

CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_trial_conversion`
OPTIONS (description = "Power BI view over rpt_trial_conversion.")
AS
SELECT * EXCEPT(built_at)
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`;

CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
OPTIONS (description = "One row per genuine special-offer free trial (1st trial per customer with £0 ACCEPTED transaction, < 10 trials): date-based status (active / converted / lapsed), days_left, and 1st/3rd paid conversion details.")
AS
WITH sub_tx AS (
  SELECT
    subscription_key,
    MIN(IF(is_free_entry, purchase_at, NULL)) AS first_free_at,
    COUNTIF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE)) AS paid_payments,
    ROUND(SUM(IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), revenue_gbp, 0)), 2) AS paid_revenue_gbp,
    ARRAY_AGG(
      IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), purchase_date, NULL)
      IGNORE NULLS
      ORDER BY purchase_at, transaction_id
    ) AS paid_dates
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE subscription_key IS NOT NULL
  GROUP BY subscription_key
)
SELECT
  s.subscription_key,
  s.customer_key,
  tx.first_free_at,
  DATE(tx.first_free_at, 'Europe/London') AS start_date,
  DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH) AS cohort,
  DATE(s.next_purchase_date) AS next_purchase_date,
  DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH) AS first_renewal_date,
  COALESCE(
    m.tier_name,
    CASE
      WHEN s.monthly_value_gbp IN (10, 15) THEN 'Bronze'
      WHEN s.monthly_value_gbp IN (20, 25, 30) THEN 'Silver'
      WHEN s.monthly_value_gbp IN (35, 50) THEN 'Gold'
      WHEN s.monthly_value_gbp IN (55, 75) THEN 'Platinum'
      ELSE 'Other'
    END
  ) AS tier,
  COALESCE(
    m.tier_band,
    CASE
      WHEN s.monthly_value_gbp < 20 THEN 'Low'
      WHEN s.monthly_value_gbp < 40 THEN 'Mid'
      WHEN s.monthly_value_gbp < 60 THEN 'High'
      ELSE 'Very High'
    END
  ) AS tier_band,
  s.monthly_value_gbp,
  DATE_DIFF(CURRENT_DATE('Europe/London'), DATE(tx.first_free_at, 'Europe/London'), DAY) AS days_in,
  DATE_DIFF(DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH), CURRENT_DATE('Europe/London'), DAY) AS days_left,
  CASE
    WHEN COALESCE(tx.paid_payments, 0) >= 1 THEN 'converted'
    WHEN s.status IN ('MONTHLY_PAYMENT_ACTION_REQUIRED', 'PAUSED', 'CANCELLED', 'MONTHLY_CANCELLED', 'MONTHLY_CANCELLED_FINISHED', 'FINISHED') THEN 'lapsed'
    WHEN CURRENT_DATE('Europe/London') < DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH) THEN 'active'
    WHEN CURRENT_DATE('Europe/London') BETWEEN DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH)
                                           AND DATE_ADD(DATE_ADD(DATE_TRUNC(DATE(tx.first_free_at, 'Europe/London'), MONTH), INTERVAL 1 MONTH), INTERVAL 15 DAY)
         AND s.status IN ('ACTIVE', 'MONTHLY_PAYMENT_FAILED') THEN 'payment due'
    ELSE 'lapsed'
  END AS status,
  tx.paid_dates[SAFE_OFFSET(0)] AS converted_date,
  tx.paid_dates[SAFE_OFFSET(2)] AS third_paid_date,
  IF(
    COALESCE(tx.paid_payments, 0) >= 1,
    GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(0)], DATE(tx.first_free_at), DAY), 0),
    NULL
  ) AS days_to_convert,
  IF(
    COALESCE(tx.paid_payments, 0) >= 3,
    GREATEST(DATE_DIFF(tx.paid_dates[SAFE_OFFSET(2)], DATE(tx.first_free_at), DAY), 0),
    NULL
  ) AS days_to_3rd_payment,
  COALESCE(tx.paid_payments, 0) >= 1 AS converted_1st,
  COALESCE(tx.paid_payments, 0) >= 3 AS converted_3rd,
  COALESCE(tx.paid_payments, 0) AS paid_payments,
  COALESCE(tx.paid_revenue_gbp, NUMERIC '0.00') AS paid_revenue_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
JOIN sub_tx tx
  ON s.subscription_key = tx.subscription_key
LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model` m
  ON s.subscription_model_key = m.subscription_model_key
WHERE s.is_special_offer_trial
  AND tx.first_free_at IS NOT NULL;

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
  + (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(event_date AS STRING), '|', COALESCE(user_pseudo_id, '(null)'))) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`) AS n
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

charity_quality AS (
  SELECT
    (SELECT COUNT(*) FROM (
      SELECT date, selected_charity 
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily` 
      GROUP BY 1, 2 
      HAVING COUNT(*) > 1
    )) AS dup_charity_rows,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(date) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily`), DAY) AS charity_days_behind
),
paid_media_wh_quality AS (
  SELECT
    (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(click_date AS STRING), '|', basis, '|', channel, '|', IFNULL(subchannel, 'none'), '|', IFNULL(campaign, 'none'), '|', ct))
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_wh_daily`) AS dup_keys,
    (SELECT COUNTIF(diff_days > 1)
     FROM (
       SELECT basis, DATE_DIFF(CURRENT_DATE('Europe/London'), MAX(click_date), DAY) AS diff_days
       FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_wh_daily`
       WHERE basis IN ('F', 'L')
       GROUP BY basis
     )) AS freshness_f_l_days_behind,
    (SELECT IF(COUNT(*) > 0, 0, 1)
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_wh_daily`
     WHERE basis = 'L' AND click_date > DATE '2026-09-19' AND purchases > 0) AS l_purchases_missing_post_sep19
),
paid_media_page_quality AS (
  SELECT
    (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(d AS STRING), '|', basis, '|', channel, '|', sub))
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`) AS dup_page_rows,
    (SELECT COUNT(*)
     FROM (
       SELECT d, ROUND(SUM(IF(basis = 'F', spend, 0)), 2) AS spend_f, ROUND(SUM(IF(basis = 'L', spend, 0)), 2) AS spend_l
       FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`
       GROUP BY d
       HAVING ROUND(SUM(IF(basis = 'F', spend, 0)) - SUM(IF(basis = 'L', spend, 0)), 2) != 0
     )) AS spend_parity_diff_days
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
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` WHERE ad_platform = 'meta'),   DAY) AS m_days,
    DATE_DIFF(CURRENT_DATE('Europe/London'), (SELECT MAX(spend_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`), DAY) AS meta_segment_days
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
null_pseudo AS (
  SELECT COUNTIF(user_pseudo_id IS NULL) AS n
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`
),
tracking_coverage_quality AS (
  SELECT
    (SELECT COUNT(*) - COUNT(DISTINCT d) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`) AS dup_d,
    (SELECT pct_tracked FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
     WHERE d = DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)) AS yesterday_pct_tracked
),
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
ga4_break_quality AS (
  SELECT COUNTIF(d BETWEEN DATE '2026-10-19' AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY) AND ga4_sessions_reliable) AS true_in_break_window
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
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
),
paid_media_entity_quality AS (
  SELECT
    (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(d AS STRING), '|', basis, '|', entity_id))
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`) AS dup_rows,
    (SELECT COUNT(*)
     FROM (
       SELECT d, basis,
         ROUND(SUM(IF(entity_id = 'ps', spend, 0)), 2) AS parent_ps,
         ROUND(SUM(IF(entity_id IN ('ps-pr', 'ps-rmg', 'ps-rtg', 'ps-unk'), spend, 0)), 2) AS child_ps,
         ROUND(SUM(IF(entity_id = 'gb', spend, 0)), 2) AS parent_gb,
         ROUND(SUM(IF(entity_id IN ('g-comp', 'g-gen', 'g-dg', 'g-pmax', 'g-unk', 'b-comp', 'b-gen', 'b-unk'), spend, 0)), 2) AS child_gb
       FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
       GROUP BY d, basis
       HAVING ABS(parent_ps - child_ps) > 0.01 OR ABS(parent_gb - child_gb) > 0.01
     )) AS spend_parity_diff_days,
    (SELECT COUNTIF(wh_n != (no_n + ns_n + er_n + es_n))
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`) AS ct_parity_bad_rows,
    (SELECT COUNT(*)
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily` r
     LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity` d ON r.entity_id = d.entity_id
     WHERE d.entity_id IS NULL) AS missing_dim_entities,
    (SELECT GREATEST(
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
        REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), ''), r'(?i)\bpr\b|_pr|pr_|\brmg\b|_rmg|rmg_|\brtg\b|_rtg|rtg_')
        OR REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]tw_campaign=([^&#]*)'), ''), r'(?i)\bpr\b|_pr|pr_|\brmg\b|_rmg|rmg_|\brtg\b|_rtg|rtg_')
      )
    GROUP BY 1
  )
),
meta_segment_quality AS (
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
),
trial_conversion_quality AS (
  SELECT
    (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(cohort AS STRING), '|', tier))
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`) AS dup_rows,
    (SELECT COUNTIF((mature_1st AND trials > 20 AND converted_1st = 0) OR (mature_3rd AND trials > 20 AND converted_3rd = 0))
     FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`) AS mature_zero_conv_anomalies,
    wf.all_special_offer,
    wf.sum_buckets,
    wf.waterfall_diff,
    act_exp.exploit_in_active,
    ROUND(CAST(COALESCE(w.cvr_7d_ago, 0) * 100 AS NUMERIC), 2) AS cvr_7d_ago_pct,
    ROUND(CAST(COALESCE(w.cvr_now, 0) * 100 AS NUMERIC), 2) AS cvr_now_pct,
    ROUND(CAST(ABS(COALESCE(w.cvr_now, 0) - COALESCE(w.cvr_7d_ago, 0)) * 100 AS NUMERIC), 2) AS wow_shift_pp
  FROM (
    WITH sub_tx_all AS (
      SELECT
        subscription_id,
        COUNT(*) AS total_tx,
        COUNTIF(payment_status = 'ACCEPTED' AND total_cost = 0) AS accepted_free_tx,
        MIN(IF(payment_status = 'ACCEPTED' AND total_cost = 0, COALESCE(purchase_date, created_at), NULL)) AS first_free_at,
        STRING_AGG(DISTINCT payment_status, ',' ORDER BY payment_status) AS all_statuses
      FROM `rh-ga4-491819.rh_warehouse.fact_transactions` FOR SYSTEM_TIME AS OF v_tx_ts
      WHERE subscription_id IS NOT NULL
      GROUP BY subscription_id
    ),
    so_ranked AS (
      SELECT
        s.subscription_id,
        s.customer_id,
        s.status AS woo_status,
        s.created_at,
        COALESCE(t.total_tx, 0) AS total_tx,
        COALESCE(t.accepted_free_tx, 0) AS accepted_free_tx,
        t.all_statuses,
        COUNTIF(
          s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS')
          AND s.customer_id IS NOT NULL
          AND s.created_at IS NOT NULL
          AND COALESCE(t.accepted_free_tx, 0) > 0
        ) OVER (PARTITION BY s.customer_id) AS cust_free_trial_subs,
        ROW_NUMBER() OVER (
          PARTITION BY s.customer_id,
            (s.status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND s.customer_id IS NOT NULL AND s.created_at IS NOT NULL AND COALESCE(t.accepted_free_tx, 0) > 0)
          ORDER BY t.first_free_at, s.created_at, s.subscription_id
        ) AS rn_valid
      FROM `rh-ga4-491819.rh_warehouse.fact_subscriptions` s FOR SYSTEM_TIME AS OF v_sub_ts
      LEFT JOIN sub_tx_all t ON s.subscription_id = t.subscription_id
      WHERE s.created_by = 'special-offer'
    ),
    buckets AS (
      SELECT
        COUNT(*) AS all_special_offer,
        COUNTIF(woo_status IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') OR customer_id IS NULL OR created_at IS NULL) AS b_abandoned,
        COUNTIF(woo_status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND customer_id IS NOT NULL AND created_at IS NOT NULL AND total_tx = 0) AS b_empty,
        COUNTIF(woo_status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND customer_id IS NOT NULL AND created_at IS NOT NULL AND total_tx > 0 AND accepted_free_tx = 0 AND all_statuses = 'FREE') AS b_free_only,
        COUNTIF(woo_status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND customer_id IS NOT NULL AND created_at IS NOT NULL AND total_tx > 0 AND accepted_free_tx = 0 AND IFNULL(all_statuses, '') != 'FREE') AS b_other_no_free_acc,
        COUNTIF(
          woo_status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND customer_id IS NOT NULL AND created_at IS NOT NULL AND accepted_free_tx > 0
          AND (
            cust_free_trial_subs >= 10
            OR customer_id IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022', '64b5d23f79c5fe003eefd800')
          )
        ) AS b_exploit,
        COUNTIF(
          woo_status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND customer_id IS NOT NULL AND created_at IS NOT NULL AND accepted_free_tx > 0
          AND cust_free_trial_subs < 10
          AND customer_id NOT IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022', '64b5d23f79c5fe003eefd800')
          AND rn_valid > 1
        ) AS b_repeat,
        COUNTIF(
          woo_status NOT IN ('PENDING_BASKET', 'IN_PAYMENT_PROCESS') AND customer_id IS NOT NULL AND created_at IS NOT NULL AND accepted_free_tx > 0
          AND cust_free_trial_subs < 10
          AND customer_id NOT IN ('66d231300ff155003e747fcc', '69dfaa9740447116ffe02052', '67e8bfdb8b1def003ebee6e0', '6911fdcc76ae9a44158d1022', '64b5d23f79c5fe003eefd800')
          AND rn_valid = 1
        ) AS b_genuine
      FROM so_ranked
    )
    SELECT
      all_special_offer,
      (b_abandoned + b_empty + b_free_only + b_other_no_free_acc + b_exploit + b_repeat + b_genuine) AS sum_buckets,
      ABS(all_special_offer - (b_abandoned + b_empty + b_free_only + b_other_no_free_acc + b_exploit + b_repeat + b_genuine))
      + ABS(b_genuine - (SELECT COALESCE(SUM(trials), 0) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`)) AS waterfall_diff
    FROM buckets
  ) wf
  CROSS JOIN (
    WITH cust_so_counts AS (
      SELECT
        s.customer_key,
        COUNT(*) AS free_trial_subs
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
      WHERE s.created_by = 'special-offer'
        AND EXISTS (
          SELECT 1
          FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
          WHERE t.subscription_key = s.subscription_key AND t.is_free_entry
        )
      GROUP BY 1
    )
    SELECT
      COUNTIF(
        COALESCE(c.free_trial_subs, 0) >= 10
        OR v.customer_key IN (
          '66d231300ff155003e747fcc',
          '69dfaa9740447116ffe02052',
          '67e8bfdb8b1def003ebee6e0',
          '6911fdcc76ae9a44158d1022',
          '64b5d23f79c5fe003eefd800'
        )
      ) AS exploit_in_active
    FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status` v
    LEFT JOIN cust_so_counts c USING (customer_key)
    WHERE v.status = 'active'
  ) act_exp
  CROSS JOIN (
    SELECT
      SAFE_DIVIDE(
        COUNTIF(DATE_ADD(cohort, INTERVAL 1 MONTH) <= CURRENT_DATE('Europe/London') AND converted_date IS NOT NULL),
        COUNTIF(DATE_ADD(cohort, INTERVAL 1 MONTH) <= CURRENT_DATE('Europe/London'))
      ) AS cvr_now,
      SAFE_DIVIDE(
        COUNTIF(DATE_ADD(cohort, INTERVAL 1 MONTH) <= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY) AND converted_date <= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)),
        COUNTIF(DATE_ADD(cohort, INTERVAL 1 MONTH) <= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY))
      ) AS cvr_7d_ago
    FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`
  ) w
)
SELECT v_run_ts, DATE(v_run_ts), 'revenue_parity',   'HARD', IF(ABS(s.gbp - c.gbp) < 0.005, 'PASS', 'FAIL'), ROUND(CAST(s.gbp AS NUMERIC), 2), c.gbp, 'All ACCEPTED orders, £, penny-exact' FROM src_tx s, cur_tx c
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'row_parity',     'HARD', IF(s.n = c.n, 'PASS', 'FAIL'), s.n, c.n, 'ACCEPTED order count' FROM src_tx s, cur_tx c
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ticket_parity',  'HARD', IF(s.tickets = c.tickets, 'PASS', 'FAIL'), s.tickets, c.tickets, 'Tickets on ACCEPTED orders' FROM src_tx s, cur_tx c
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'monthly_parity', 'HARD', IF(months_off = 0, 'PASS', 'FAIL'), 0, months_off, 'Months where revenue differs' FROM monthly
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'duplicate_keys', 'HARD', IF(n = 0, 'PASS', 'FAIL'), 0, n, 'Duplicate primary keys across all tables' FROM dups
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'null user_pseudo_id count', 'WARN', IF(n = 0, 'PASS', 'WARN'), 0, n, 'GA4 purchases with null user_pseudo_id (tracked as warning only)' FROM null_pseudo
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
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'fresh_meta_segment', 'HARD', IF(meta_segment_days <= 2, 'PASS', 'FAIL'), 2, meta_segment_days, 'Days since latest Meta segment spend day in stg_meta_segment_daily (limit 2)' FROM fresh
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'vat_columns_consistent', 'HARD', IF(inconsistent_vat_rows = 0, 'PASS', 'FAIL'), 0, inconsistent_vat_rows, 'Every row has spend_inc_vat_gbp = ROUND(spend_ex_vat_gbp * 1.2, 2)' FROM spend_vat
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'spend_parity_unchanged', 'HARD', IF(sum_ex_vat = sum_orig_gbp, 'PASS', 'FAIL'), sum_orig_gbp, sum_ex_vat, 'SUM(spend_ex_vat_gbp) = SUM(spend_gbp)' FROM spend_vat
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_funnel_classified', 'WARN', IF(meta_unknown_pct = 0, 'PASS', 'WARN'), 0, meta_unknown_pct, 'Share of Meta spend in Unknown from 2026-09-01 onwards, %' FROM spend_vat
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_api_parity',        'HARD', IF(dates_with_diff = 0, 'PASS', 'FAIL'), 0, dates_with_diff, 'Meta spend equals stg_meta_ads_daily from 2026-09-01 to yesterday' FROM meta_api_diffs
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_no_double_source',   'HARD', IF(overlap_dates = 0, 'PASS', 'FAIL'), 0, overlap_dates, 'No Meta date has rows from both manual and API sources' FROM meta_double_source
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_purchase_parity',   'WARN', IF(diff_pct <= 10.0, 'PASS', 'WARN'), web_checkouts, ga4_rows, 'GA4 BoughtTickets rows within ±10% of web checkouts for previous month' FROM ga4_parity
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tagging_exceptions',    'WARN', IF(purchasing_visitors = 0, 'PASS', 'WARN'), 0, purchasing_visitors, CONCAT('Top mis-tagged campaigns: ', top_5_campaigns) FROM tagging_exc
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'charity_sales_no_duplicates', 'HARD',
               IF(dup_charity_rows = 0, 'PASS', 'FAIL'), 0, dup_charity_rows,
               'Zero duplicate (date, charity) rows in rpt_charity_sales_daily' FROM charity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'charity_sales_freshness',     'HARD',
               IF(charity_days_behind <= 1, 'PASS', 'FAIL'), 1, charity_days_behind,
               'Max date in rpt_charity_sales_daily reaches yesterday (limit 1 day)' FROM charity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_ad_level_parity', 'WARN', IF(mismatch_days = 0, 'PASS', 'WARN'), 0, mismatch_days, IF(mismatch_days = 0, 'Ad-level spend matches campaign spend within 1% for all days in last 7', CONCAT('Days in last 7 with >1% diff or missing ad-level: ', bad_dates)) FROM meta_ad_parity
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_wh_no_duplicates', 'HARD',
               IF(dup_keys = 0, 'PASS', 'FAIL'), 0, dup_keys,
               'Zero duplicate keys in rpt_paid_media_wh_daily' FROM paid_media_wh_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_wh_freshness',     'HARD',
               IF(freshness_f_l_days_behind = 0, 'PASS', 'FAIL'), 0, freshness_f_l_days_behind,
               'Max click_date in rpt_paid_media_wh_daily reaches yesterday for both F and L (limit 1 day)' FROM paid_media_wh_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_wh_recent_purchases', 'HARD',
               IF(l_purchases_missing_post_sep19 = 0, 'PASS', 'FAIL'), 0, l_purchases_missing_post_sep19,
               'Last touch (L) has purchases after 19 Sep' FROM paid_media_wh_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_page_no_duplicates', 'HARD',
               IF(dup_page_rows = 0, 'PASS', 'FAIL'), 0, dup_page_rows,
               'Zero duplicate (d, basis, channel, sub) rows in rpt_paid_media_page_daily' FROM paid_media_page_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_page_spend_parity', 'HARD',
               IF(spend_parity_diff_days = 0, 'PASS', 'FAIL'), 0, spend_parity_diff_days,
               'Spend on F = spend on L per day across all dates' FROM paid_media_page_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_entity_no_duplicates', 'HARD',
               IF(dup_rows = 0, 'PASS', 'FAIL'), 0, dup_rows,
               'Zero duplicate (d, basis, entity_id) rows in rpt_paid_media_entity_daily' FROM paid_media_entity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_entity_parent_spend_parity', 'HARD',
               IF(spend_parity_diff_days = 0, 'PASS', 'FAIL'), 0, spend_parity_diff_days,
               'Parent spend equals sum of children spend within £0.01 for ps and gb' FROM paid_media_entity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_entity_wh_customer_type_parity', 'HARD',
               IF(ct_parity_bad_rows = 0, 'PASS', 'FAIL'), 0, ct_parity_bad_rows,
               'wh_n = sum of customer-type n across all rows' FROM paid_media_entity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_entity_dim_fk', 'HARD',
               IF(missing_dim_entities = 0, 'PASS', 'FAIL'), 0, missing_dim_entities,
               'Every entity_id in rpt_paid_media_entity_daily exists in dim_paid_media_entity' FROM paid_media_entity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'paid_media_entity_freshness', 'WARN',
               IF(freshness_max_days_behind <= 2, 'PASS', 'WARN'), 2, freshness_max_days_behind,
               'meta_to and google_to are within 2 days of today' FROM paid_media_entity_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'meta_unknown_revenue_share', 'WARN',
               IF(days_over_10pct = 0, 'PASS', 'WARN'), 10.0, max_unk_share_pct,
               IF(days_over_10pct = 0,
                  CONCAT('ps-unk share <= 10% for all of last 7 days (max ', CAST(max_unk_share_pct AS STRING), '%). Top unmatched: ', top_unmatched),
                  CONCAT('Days in last 7 with ps-unk > 10%: ', bad_days, '. Top unmatched: ', top_unmatched)
               )
FROM meta_unk_quality CROSS JOIN meta_unk_top_unmatched
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
FROM meta_segment_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tracking_coverage_no_duplicates', 'HARD',
               IF(dup_d = 0, 'PASS', 'FAIL'), 0, dup_d,
               'Zero duplicate d in rpt_tracking_coverage_daily' FROM tracking_coverage_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tracking_coverage_pct_tracked', 'WARN',
               IF(yesterday_pct_tracked >= 0.90, 'PASS', 'WARN'), 0.90, ROUND(CAST(yesterday_pct_tracked AS NUMERIC), 4),
               'pct_tracked for yesterday >= 0.90 (warns if cookie change reduced tracking)' FROM tracking_coverage_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tracking_early_warning_spike', 'WARN',
               test_result, ROUND(CAST(med_sessions AS NUMERIC), 0), y_sessions,
               detail FROM early_warning_tracking
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_flag_populated', 'HARD',
               IF(null_flags_count = 0, 'PASS', 'FAIL'), 0, null_flags_count,
               'Every table reading GA4 has non-null ga4_reliable flag' FROM ga4_flags
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_from_break', 'HARD',
               IF(true_in_break_window = 0, 'PASS', 'FAIL'), 0, true_in_break_window,
               'Zero days in break window (2026-10-19 to yesterday) have ga4_sessions_reliable = true' FROM ga4_break_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'tracking_early_warning_incomplete_guard', 'HARD',
               IF(fixture_test_result = 'WARN', 'PASS', 'FAIL'), 0, IF(fixture_test_result = 'WARN', 0, 1),
               'Early-warning test returns WARN when ga4_complete is false (tested with incomplete fixture day)' FROM early_warning_guard
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'trial_mature_cohort_conversions', 'HARD',
               IF(mature_zero_conv_anomalies = 0, 'PASS', 'FAIL'), 0, mature_zero_conv_anomalies,
               'Zero mature trial cohorts with > 20 trials have 0 conversions' FROM trial_conversion_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'trial_conversion_no_duplicates', 'HARD',
               IF(dup_rows = 0, 'PASS', 'FAIL'), 0, dup_rows,
               'Zero duplicate (cohort, tier) rows in rpt_trial_conversion' FROM trial_conversion_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'trial_waterfall_exact_sum', 'HARD',
               IF(waterfall_diff = 0, 'PASS', 'FAIL'), all_special_offer, sum_buckets,
               'Special-offer waterfall buckets sum exactly to total special-offer subscriptions and genuine trials match rpt_trial_conversion' FROM trial_conversion_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'trial_no_exploit_in_active', 'HARD',
               IF(exploit_in_active = 0, 'PASS', 'FAIL'), 0, exploit_in_active,
               'Zero exploit-rule customers (>= 10 trial subscriptions or backstop accounts) in active trials' FROM trial_conversion_quality
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'trial_conversion_1st_pay_wow_shift', 'WARN',
               IF(wow_shift_pp <= 5.0, 'PASS', 'WARN'), cvr_7d_ago_pct, cvr_now_pct,
               CONCAT('Trailing mature 1st-payment trial CVR week-on-week shift = ', CAST(wow_shift_pp AS STRING), ' pp (limit 5.0 pp)') FROM trial_conversion_quality;

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

-- GA4 session funnel health gate (freshness <= 2 days, daily sessions within +/-40% of trailing 7d avg)
INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
  (run_ts, run_date, test, severity, result, source_value, curated_value, detail)
WITH stats AS (
  SELECT
    session_date,
    COUNT(*) as daily_sessions
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_session_funnel`
  WHERE session_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 14 DAY)
  GROUP BY 1
),
eval_day AS (
  SELECT
    MAX(session_date) AS max_date,
    DATE_DIFF(CURRENT_DATE('Europe/London'), MAX(session_date), DAY) AS days_old
  FROM stats
),
trailing_calc AS (
  SELECT
    e.max_date,
    e.days_old,
    s_eval.daily_sessions AS eval_sessions,
    ROUND(AVG(s_prior.daily_sessions), 2) AS trailing_7d_avg,
    ROUND(SAFE_DIVIDE(ABS(s_eval.daily_sessions - AVG(s_prior.daily_sessions)), AVG(s_prior.daily_sessions)) * 100, 2) AS pct_deviation
  FROM eval_day e
  JOIN stats s_eval ON s_eval.session_date = e.max_date
  LEFT JOIN stats s_prior ON s_prior.session_date BETWEEN DATE_SUB(e.max_date, INTERVAL 7 DAY) AND DATE_SUB(e.max_date, INTERVAL 1 DAY)
  GROUP BY 1, 2, 3
)
SELECT
  v_run_ts,
  DATE(v_run_ts),
  'ga4_session_funnel_health' AS test,
  'HARD' AS severity,
  IF(days_old <= 2 AND pct_deviation <= 40.0, 'PASS', 'FAIL') AS result,
  CAST(trailing_7d_avg AS NUMERIC) AS source_value,
  CAST(eval_sessions AS NUMERIC) AS curated_value,
  CONCAT('Latest date ', CAST(max_date AS STRING), ' has ', CAST(eval_sessions AS STRING), ' sessions vs 7d avg ', CAST(trailing_7d_avg AS STRING), ' (', CAST(pct_deviation AS STRING), '% dev, limit 40%), age = ', CAST(days_old AS STRING), ' days (limit 2)') AS detail
FROM trailing_calc;

-- Power BI data-health page reads this.
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_dq_gate_results` AS
SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`;

-- Cross-suite health monitoring view
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
    IF(
      ARRAY_LENGTH(stale_feeds) > 0,
      CONCAT('Feed warning: ', ARRAY_TO_STRING(stale_feeds, ', '), ' spend feed >2 days behind build date'),
      'Platform feeds up to date'
    ) AS caveat_reason
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

  -- 3. Free Trials (renewal-day grace window dynamic counts & mature cohort zero-conversion HARD test)
  SELECT
    'Free trials' AS report,
    'vw_trial_status, rpt_trial_conversion' AS tables_read,
    MAX(t.built_at) AS built_at,
    (SELECT MAX(purchase_date) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`) AS latest_data_date,
    MAX(m.anomaly_count) > 0 AS is_failed,
    FALSE AS has_caveat,
    IF(
      MAX(m.anomaly_count) > 0,
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
    ELSE 7
  END;

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
    ('fact_marketing_spend_daily', 'meta_funnel', 'Meta campaign funnel stage classified from campaign name (Prospecting, Retargeting, Remarketing, or Unknown (daily total) for manual upload). NULL for Google.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('dim_subscription_model', 'tier_band', 'internal curve grouping; use tier_name in reports', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('dim_subscription_model', 'tier_name', 'Website subscription tier: Bronze (£10/£15), Silver (£20/£25/£30), Gold (£35/£50), Platinum (£55/£75), Other.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('customer_ltv_horizons', 'current_tier_name', 'The website tier name of the customer active subscription (Bronze, Silver, Gold, Platinum, Other), NULL if none.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_tracking_coverage_daily', 'est_visits', 'Estimated visits before the 19 Oct banner based on page views per consented visit. Not reliable after the 19 Oct banner; do not use (NULL from 19 Oct).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_tracking_coverage_daily', 'consent_granted_share', 'Share of landings with privacy_info.analytics_storage = Yes. Before the 19 Oct banner, remaining 6-22% were NULL (unconfigured), with 0% denied.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_tracking_coverage_daily', 'ga4_sessions', 'distinct GA4 sessions (user_pseudo_id + ga_session_id)', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_tracking_coverage_daily', 'ga4_complete', 'Whether GA4 daily export table events_YYYYMMDD has landed and been loaded for this date (excludes intraday pings).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_visit_source_daily', 'ga4_complete', 'Whether this date was loaded from a finalized GA4 daily table events_YYYYMMDD (TRUE).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_paid_media_entity_daily', 'spend', 'Platform-reported ad spend excluding VAT in pounds.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_paid_media_entity_daily', 'spend_inc_vat', 'Platform-reported ad spend including 20% UK VAT in pounds: ROUND(spend * 1.2, 2).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_paid_media_entity_daily', 'adj_rev', 'Subscription starts revalued at £50/£80/£105/£110 by plan tier plus actual non-sub revenue for Google entities; 0.0 for non-Google.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_subscriptions', 'is_special_offer_trial', 'Persistent flag for a customer first genuine special-offer free trial (has £0 ACCEPTED transaction, customer has < 10 trial subscriptions and not in backstop exploit accounts).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_trial_conversion', 'conversion_rate_1st', 'Realised trial-to-1st-paid-payment conversion rate for cohort x tier: ROUND(converted_1st / trials, 4).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_trial_conversion', 'conversion_rate_3rd', 'Headline KPI: realised trial-to-3rd-paid-payment conversion rate for cohort x tier: ROUND(converted_3rd / trials, 4).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_trial_conversion', 'trailing_conversion_rate_1st', 'Trailing mature 1st-payment conversion rate by tier (fallback to overall mature 1st-payment rate when tier has < 30 trials). Used as LTV/EFV multiplier.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_trial_conversion', 'trailing_conversion_rate_3rd', 'Trailing mature 3rd-payment conversion rate by tier (fallback to overall mature 3rd-payment rate when tier has < 30 trials). Headline KPI.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('vw_trial_status', 'status', 'Date-based trial status (never uses WooCommerce fs.status): active while in first trial cycle with 0 paid tx, converted after 1st paid tx, lapsed otherwise.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('vw_trial_status', 'days_left', 'Days remaining until next_purchase_date: DATE_DIFF(DATE(next_purchase_date), CURRENT_DATE(), DAY).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'audience_label', 'Warehouse audience label: existing (prior paid order), engaged (prior £0 order or account older than order day), new (first order on account creation day), or free (£0 order).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'label_reason', 'Specific reason for audience_label assignment.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'prior_paid_orders', 'Count of prior accepted orders with revenue_gbp > 0 for this customer before this order.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'prior_free_orders', 'Count of prior accepted orders with revenue_gbp = 0 for this customer before this order.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'is_renewal', 'TRUE if order is a recurring subscription renewal (sub_payment_no > 1); FALSE for first payment or non-sub.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_ga4_prior_visit_daily', 'with_prior_visit_30d', 'Count of GA4 purchase events where the user_pseudo_id had a session_start on an earlier day in the trailing 30 days. Estimate only.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_ga4_prior_visit_daily', 'prior_visit_share', 'Share of GA4 purchases with an earlier session_start in the trailing 30 days. Estimate only.', 'none', 'approved', CURRENT_TIMESTAMP())
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
-- SECTION 20 — rpt_exec_summary (Executive Dashboard Snapshot Table)
-- Rebuilt nightly as the final pipeline step, one row per KPI.
-- =====================================================================
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
  SELECT CONCAT('org_clearly_sessions_', oa.period) AS kpi_id, 'organic_charity' AS section, 'Organic Social Site Sessions (Clearly Organic)' AS label, oa.period, CAST(oa.clearly_sessions AS NUMERIC) AS value, 'count' AS unit, oa.as_of_date, 'rpt_organic_social_weekly' AS source_tables, oa.built_at, oa.health_organic AS health_status, 'GA4 sessions unreliable since 2026-10-19; site CVR understated' AS note FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_clearly_purchases_', oa.period), 'organic_charity', 'Organic Social Purchases (Clearly Organic)', oa.period, CAST(oa.clearly_purchases AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Orders from clearly organic social sessions' FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_clearly_revenue_', oa.period), 'organic_charity', 'Organic Social Revenue (Clearly Organic)', oa.period, CAST(oa.clearly_revenue AS NUMERIC), 'gbp', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'Revenue from clearly organic social sessions' FROM organic_agg oa
  UNION ALL
  SELECT CONCAT('org_unclear_sessions_', oa.period), 'organic_charity', 'Organic Social Site Sessions (Unclear)', oa.period, CAST(oa.unclear_sessions AS NUMERIC), 'count', oa.as_of_date, 'rpt_organic_social_weekly', oa.built_at, oa.health_organic, 'GA4 sessions unreliable since 2026-10-19; site CVR understated' AS note FROM organic_agg oa
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
