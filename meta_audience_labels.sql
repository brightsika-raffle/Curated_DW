-- Meta audience labels, warehouse version (existing / engaged / new)
-- Project rh-ga4-491819, dataset rh_curated_Bright_Test. Source: curated fact_transactions + dim_channel_last_touch only.
-- Grain: one row per accepted order (paid and £0). Revenue in pounds (fact_transactions already converts from pence).
--
-- Label rules (Jordan, 5 Nov):
--   existing = the customer had at least one PAID order (revenue_gbp > 0) before this order
--   engaged  = no prior paid order, but a prior £0 / free-trial order, OR the account existed before the order day
--   new      = none of the above
--   free     = the order itself is £0 (kept out of paid totals; never counted as prospecting)
-- Not in v1: "engaged because they visited the site earlier". GA4 purchase events cannot be joined to orders
-- (GA4 transaction_id is a 36-char UUID, warehouse order id is a 24-char id; 0 matches checked 12 Nov). See table 2.

-- 1) Order-level labels. Full rebuild is about 250 MB per run (well under 1p).
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
    WHEN t.prior_free_orders > 0 THEN 'prior £0 order only'
    WHEN t.customer_age_days_at_order > 0 THEN 'account older than order day'
    ELSE 'first order, account created same day'
  END AS label_reason,
  t.prior_paid_orders,
  t.prior_free_orders,
  (t.is_subscription_payment AND t.sub_payment_no > 1) AS is_renewal,
  CURRENT_TIMESTAMP() AS loaded_at
FROM t
LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch` c ON c.channel_key = t.channel_last_touch_key;

-- 2) Daily estimate of "new by our data, but visited before" from GA4 (site-wide, not per order).
-- Lower bound: GA4 pseudo ids reset with cookies, and GA4 sees about 40% of orders.
-- About 10 GB per full run (about 5p); run incrementally (last 3 days) in the schedule.
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`
PARTITION BY purchase_date
OPTIONS (description = 'GA4 purchases by day and GA4 customer_type, with the share that had a session on an earlier day in the previous 30 days. Estimate only; cannot be joined to orders.', labels = [('layer', 'report'), ('owner', 'bright_sika')])
AS
WITH ev AS (
  SELECT user_pseudo_id AS u, event_name AS e, TIMESTAMP_MICROS(event_timestamp) AS ts, ecommerce.transaction_id AS tx,
    IF(event_name = 'purchase', (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'customer_type'), NULL) AS ct
  FROM `rh-ga4-491819.analytics_354198910.events_*`
  WHERE _TABLE_SUFFIX >= '20260801' AND event_name IN ('purchase', 'session_start') AND user_pseudo_id IS NOT NULL
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
WHERE purchase_date >= '2026-08-31'  -- needs 30 days of GA4 history before it
GROUP BY 1, 2;

-- 3) Reporting view for the team: two buckets, like for like with Meta.
--    existing = existing; not_yet_customer = engaged + new (Meta: engaged + prospecting).
--    Paid orders only, renewals excluded (first payment only, same rule as the daily dashboard).
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
FROM w FULL JOIN m USING (d, bucket);

-- 4) Check against Meta's own split (read-only). Meta counts view-through and 7-day click, so totals differ.
-- WITH w AS (SELECT audience_label AS seg, COUNT(*) AS orders, SUM(revenue_gbp) AS rev
--            FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`
--            WHERE channel_group = 'Meta' AND audience_label != 'free' AND NOT is_renewal AND purchase_date BETWEEN '2026-09-01' AND '2026-11-03' GROUP BY 1),
--      m AS (SELECT CASE user_segment_key WHEN 'prospecting' THEN 'new' ELSE user_segment_key END AS seg, SUM(purchases) AS meta_purchases
--            FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
--            WHERE spend_date BETWEEN '2026-09-01' AND '2026-11-03' GROUP BY 1)
-- SELECT * FROM w FULL JOIN m USING (seg);
