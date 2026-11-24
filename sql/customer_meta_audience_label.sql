-- Customer-keyed Meta audience labels (2 buckets: existing vs new)
-- Project: rh-ga4-491819, Dataset: rh_curated_Bright_Test, Location: europe-west2
-- Parameter: flip free_trial_only_bucket between 'existing' and 'new' without other edits.
-- No PII stored or output; no Meta export or hashed email.

DECLARE free_trial_only_bucket STRING DEFAULT 'existing';

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.customer_meta_audience_label`
CLUSTER BY meta_audience_bucket, customer_key
OPTIONS (
  description = 'Customer-level Meta audience bucket (existing vs new; free_trial_only parameterized at top) with recency columns last_paid_date and paid_orders_last_365d. Rebuilt nightly.',
  labels = [('layer', 'mart'), ('owner', 'bright_sika')]
)
AS
WITH tx AS (
  SELECT
    customer_key,
    COUNTIF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE)) AS paid_orders,
    COUNTIF(
      revenue_gbp > 0
      AND NOT IFNULL(is_free_entry, FALSE)
      AND purchase_date > DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 365 DAY)
    ) AS paid_orders_last_365d,
    COUNTIF(revenue_gbp = 0 OR IFNULL(is_free_entry, FALSE)) AS free_orders,
    MIN(IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), purchase_date, NULL)) AS first_paid_date,
    MAX(IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), purchase_date, NULL)) AS last_paid_date,
    MAX(purchase_date) AS last_tx_date
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE purchase_date >= DATE '2018-01-01'
    AND customer_key != 'UNKNOWN_DELETED'
  GROUP BY 1
)
SELECT
  d.customer_key,
  CASE
    WHEN COALESCE(t.paid_orders, 0) > 0 THEN 'existing'
    WHEN COALESCE(t.free_orders, 0) > 0 THEN free_trial_only_bucket
    ELSE 'new'
  END AS meta_audience_bucket,
  CASE
    WHEN COALESCE(t.paid_orders, 0) > 0 THEN 'paid_customer'
    WHEN COALESCE(t.free_orders, 0) > 0 THEN 'free_trial_only'
    ELSE 'no_orders'
  END AS label_reason,
  COALESCE(t.paid_orders, 0) > 0 AS has_paid_order,
  (COALESCE(t.paid_orders, 0) = 0 AND COALESCE(t.free_orders, 0) > 0) AS is_free_trial_only,
  COALESCE(t.paid_orders, 0) AS paid_orders,
  COALESCE(t.paid_orders_last_365d, 0) AS paid_orders_last_365d,
  COALESCE(t.free_orders, 0) AS free_orders,
  t.first_paid_date,
  t.last_paid_date,
  t.last_tx_date,
  CURRENT_TIMESTAMP() AS built_at
FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` d
LEFT JOIN tx t USING (customer_key);
