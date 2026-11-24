-- Staging: Customer-keyed Meta audience labels (2 buckets: existing vs new)
-- Project: rh-ga4-491819, Dataset: rh_curated_Bright_Test, Location: europe-west2
-- Rules:
--   existing = customer has at least one paid purchase (revenue_gbp > 0, not free entry)
--              OR is a free-trial-only customer (only £0 / free-entry orders).
--   new      = customer has no orders in fact_transactions yet.
-- No PII stored or output.

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.customer_meta_audience_label_stg`
CLUSTER BY meta_audience_bucket, customer_key
OPTIONS (
  description = 'Staging: Customer-level Meta audience bucket (existing vs new; free-trial-only counts as existing). Built nightly from dim_customer and fact_transactions.',
  labels = [('layer', 'mart'), ('owner', 'bright_sika')]
)
AS
WITH tx AS (
  SELECT
    customer_key,
    COUNTIF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE)) AS paid_orders,
    COUNTIF(revenue_gbp = 0 OR IFNULL(is_free_entry, FALSE)) AS free_orders,
    MIN(IF(revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE), purchase_date, NULL)) AS first_paid_date,
    MAX(purchase_date) AS last_tx_date
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE purchase_date >= DATE '2018-01-01'
    AND customer_key != 'UNKNOWN_DELETED'
  GROUP BY 1
)
SELECT
  d.customer_key,
  IF(COALESCE(t.paid_orders, 0) > 0 OR COALESCE(t.free_orders, 0) > 0, 'existing', 'new') AS meta_audience_bucket,
  CASE
    WHEN COALESCE(t.paid_orders, 0) > 0 THEN 'paid_customer'
    WHEN COALESCE(t.free_orders, 0) > 0 THEN 'free_trial_only'
    ELSE 'no_orders'
  END AS label_reason,
  COALESCE(t.paid_orders, 0) > 0 AS has_paid_order,
  (COALESCE(t.paid_orders, 0) = 0 AND COALESCE(t.free_orders, 0) > 0) AS is_free_trial_only,
  COALESCE(t.paid_orders, 0) AS paid_orders,
  COALESCE(t.free_orders, 0) AS free_orders,
  t.first_paid_date,
  t.last_tx_date,
  CURRENT_TIMESTAMP() AS built_at
FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` d
LEFT JOIN tx t USING (customer_key);
