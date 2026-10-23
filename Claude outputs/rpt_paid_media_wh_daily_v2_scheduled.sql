-- Scheduled query: rpt_paid_media_wh_daily nightly (v2 - Curated Sources Rebuild)
-- Rebuilds the warehouse side of the paid media report every night, for first AND last
-- touch together, so the two views always cover the same dates.
-- Reads ONLY curated/source tables: rh_curated_Bright_Test.fact_transaction_touch
-- Runs as the Raffle House pipeline service account. ~0.3 GB per run (pennies a month).
-- Location: europe-west2. Schedule: daily 05:00 Europe/London (after LTV at 04:00).

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
        CASE WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'competitor') THEN 'Competitor'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'generic') THEN 'Generic'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'brand') THEN 'Brand'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'pmax|performance') THEN 'PMAX'
             WHEN REGEXP_CONTAINS(LOWER(ch.utm_campaign), r'demand|dgen|\bdg\b') THEN 'Demand Gen'
             ELSE 'Unknown' END
      ELSE NULL
    END AS subchannel,
    ch.utm_campaign AS campaign,
    CONCAT(IF(t.is_first_purchase, 'N', 'E'), IF(t.is_sub, 'S', 'O')) AS ct,  -- NO / NS / EO / ES
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
