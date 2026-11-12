import os, sys

def main():
    with open('sql/nightly_build_and_test.sql', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Section 7b: after fact_transactions
    target1 = """FROM base;
-- NOTE: raffle_id may be NULL on some ACCEPTED rows (e.g. competitions).
-- Those land on raffle_key = 'UNKNOWN'. Gate 9.6 reports how many."""

    repl1 = """FROM base;
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
LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_channel_last_touch` c ON c.channel_key = t.channel_last_touch_key;"""

    assert target1 in content, 'Target 1 not found'
    content = content.replace(target1, repl1, 1)

    # 2. Section 8h: after fact_ga4_purchases
    target2 = """  CURRENT_TIMESTAMP() AS loaded_at
FROM raw
WHERE rn = 1;"""

    repl2 = """  CURRENT_TIMESTAMP() AS loaded_at
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
GROUP BY 1, 2;"""

    assert target2 in content, 'Target 2 not found'
    content = content.replace(target2, repl2, 1)

    # 3. Views in Section 9
    target3 = "CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_entity_status` AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`;"

    repl3 = """CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_rpt_paid_media_entity_status` AS SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`;
CREATE OR REPLACE VIEW `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_order_audience_label`     AS SELECT * EXCEPT(loaded_at) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`;
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

    assert target3 in content, 'Target 3 not found'
    content = content.replace(target3, repl3, 1)

    # 4. Column dictionary merge
    target4 = """    ('vw_trial_status', 'days_left', 'Days remaining until next_purchase_date: DATE_DIFF(DATE(next_purchase_date), CURRENT_DATE(), DAY).', 'none', 'approved', CURRENT_TIMESTAMP())
  ])) S"""

    repl4 = """    ('vw_trial_status', 'days_left', 'Days remaining until next_purchase_date: DATE_DIFF(DATE(next_purchase_date), CURRENT_DATE(), DAY).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'audience_label', 'Warehouse audience label: existing (prior paid order), engaged (prior £0 order or account older than order day), new (first order on account creation day), or free (£0 order).', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'label_reason', 'Specific reason for audience_label assignment.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'prior_paid_orders', 'Count of prior accepted orders with revenue_gbp > 0 for this customer before this order.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'prior_free_orders', 'Count of prior accepted orders with revenue_gbp = 0 for this customer before this order.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('fact_order_audience_label', 'is_renewal', 'TRUE if order is a recurring subscription renewal (sub_payment_no > 1); FALSE for first payment or non-sub.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_ga4_prior_visit_daily', 'with_prior_visit_30d', 'Count of GA4 purchase events where the user_pseudo_id had a session_start on an earlier day in the trailing 30 days. Estimate only.', 'none', 'approved', CURRENT_TIMESTAMP()),
    ('rpt_ga4_prior_visit_daily', 'prior_visit_share', 'Share of GA4 purchases with an earlier session_start in the trailing 30 days. Estimate only.', 'none', 'approved', CURRENT_TIMESTAMP())
  ])) S"""

    assert target4 in content, 'Target 4 not found'
    content = content.replace(target4, repl4, 1)

    with open('sql/nightly_build_and_test.sql', 'w', encoding='utf-8') as f:
        f.write(content)

    print('Updated sql/nightly_build_and_test.sql successfully.')

if __name__ == '__main__':
    main()

