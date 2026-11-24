-- Customer value tiers (realised, future 36m, full LTV 36m)
-- Target: rh-ga4-491819.rh_curated_Bright_Test.customer_value_tiers (europe-west2)
-- Tie-break: first_tx_date ASC (oldest first), then customer_key ASC.

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.customer_value_tiers`
CLUSTER BY customer_key
OPTIONS (
  description = 'Customer value tiers (1=VIP1 .. 8=Very Low, 9=Free Only) across realised, future 36m (with high-depth floor), and full LTV 36m. Tie-break: oldest first_tx_date first.',
  labels = [('layer', 'report'), ('owner', 'bright_sika')]
)
AS
WITH tx AS (
  SELECT customer_key, purchase_date, revenue_gbp,
         (is_subscription_payment OR order_type = 'SUBSCRIPTION') AS is_sub
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
  WHERE revenue_gbp > 0 AND NOT IFNULL(is_free_entry, FALSE) AND customer_key != 'UNKNOWN_DELETED'
),
as_of AS (SELECT MAX(purchase_date) AS as_of_date FROM tx),
paid AS (
  SELECT t.customer_key,
         MIN(t.purchase_date) AS first_tx_date,
         SUM(t.revenue_gbp) AS paid_gbp,
         COUNTIF(NOT t.is_sub) AS single_orders,
         SUM(IF(NOT t.is_sub, t.revenue_gbp, 0)) AS single_spend,
         COUNTIF(NOT t.is_sub AND t.purchase_date > DATE_SUB(a.as_of_date, INTERVAL 61 DAY)) AS single_recent,
         COUNTIF(t.is_sub AND t.purchase_date > DATE_SUB(a.as_of_date, INTERVAL 35 DAY)) AS sub_recent
  FROM tx t CROSS JOIN as_of a
  GROUP BY t.customer_key
),
fv AS (
  SELECT p.customer_key, p.first_tx_date,
         COALESCE(h.realised_value, p.paid_gbp) AS realised_gbp,
         IFNULL(h.efv_36m, 0) AS efv36_model,
         IF(p.single_orders >= 10 AND p.single_recent > 0 AND p.sub_recent = 0,
            p.single_spend * IF(p.single_orders >= 30, 0.25, 0.33) * 1.75, NULL) AS efv36_floor
  FROM paid p
  LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons` h USING (customer_key)
),
v AS (
  SELECT customer_key, first_tx_date, realised_gbp,
         GREATEST(efv36_model, IFNULL(efv36_floor, 0)) AS future_36m_gbp,
         realised_gbp + GREATEST(efv36_model, IFNULL(efv36_floor, 0)) AS ltv_36m_gbp,
         IFNULL(efv36_floor, 0) > efv36_model AS floor_applied
  FROM fv
),
r AS (
  SELECT v.*, COUNT(*) OVER () AS n_paid,
         ROW_NUMBER() OVER (ORDER BY realised_gbp DESC, first_tx_date ASC, customer_key ASC) AS rk_realised,
         ROW_NUMBER() OVER (ORDER BY future_36m_gbp DESC, first_tx_date ASC, customer_key ASC) AS rk_future,
         ROW_NUMBER() OVER (ORDER BY ltv_36m_gbp DESC, first_tx_date ASC, customer_key ASC) AS rk_ltv
  FROM v
),
t AS (
  SELECT r.*,
         (rk_realised - 1) / n_paid AS pos_realised,
         (rk_future - 1) / n_paid AS pos_future,
         (rk_ltv - 1) / n_paid AS pos_ltv
  FROM r
),
tiered AS (
  SELECT customer_key, first_tx_date, realised_gbp, future_36m_gbp, ltv_36m_gbp, floor_applied,
    CASE WHEN pos_realised < 0.001 THEN 1 WHEN pos_realised < 0.01 THEN 2 WHEN pos_realised < 0.03 THEN 3
         WHEN pos_realised < 0.10 THEN 4 WHEN pos_realised < 0.20 THEN 5 WHEN pos_realised < 0.35 THEN 6
         WHEN pos_realised < 0.60 THEN 7 ELSE 8 END AS tier_realised_num,
    CASE WHEN future_36m_gbp = 0 THEN 8
         WHEN pos_future < 0.001 THEN 1 WHEN pos_future < 0.01 THEN 2 WHEN pos_future < 0.03 THEN 3
         WHEN pos_future < 0.10 THEN 4 WHEN pos_future < 0.20 THEN 5 WHEN pos_future < 0.35 THEN 6
         WHEN pos_future < 0.60 THEN 7 ELSE 8 END AS tier_future_num,
    CASE WHEN pos_ltv < 0.001 THEN 1 WHEN pos_ltv < 0.01 THEN 2 WHEN pos_ltv < 0.03 THEN 3
         WHEN pos_ltv < 0.10 THEN 4 WHEN pos_ltv < 0.20 THEN 5 WHEN pos_ltv < 0.35 THEN 6
         WHEN pos_ltv < 0.60 THEN 7 ELSE 8 END AS tier_ltv_num
  FROM t
)
SELECT
  d.customer_key,
  x.first_tx_date,
  ROUND(IFNULL(x.realised_gbp, 0), 2) AS realised_gbp,
  ROUND(IFNULL(x.future_36m_gbp, 0), 2) AS future_36m_gbp,
  ROUND(IFNULL(x.ltv_36m_gbp, 0), 2) AS ltv_36m_gbp,
  IFNULL(x.tier_realised_num, 9) AS tier_realised_num,
  IFNULL(x.tier_future_num, 9) AS tier_future_num,
  IFNULL(x.tier_ltv_num, 9) AS tier_ltv_num,
  CASE IFNULL(x.tier_realised_num, 9)
    WHEN 1 THEN '01. VIP1' WHEN 2 THEN '02. VIP 2' WHEN 3 THEN '03. Premium' WHEN 4 THEN '04. Very High'
    WHEN 5 THEN '05. High' WHEN 6 THEN '06. Medium' WHEN 7 THEN '07. Low' WHEN 8 THEN '08. Very Low' ELSE '09. Free Only' END AS value_tier_realised,
  CASE IFNULL(x.tier_future_num, 9)
    WHEN 1 THEN '01. VIP1' WHEN 2 THEN '02. VIP 2' WHEN 3 THEN '03. Premium' WHEN 4 THEN '04. Very High'
    WHEN 5 THEN '05. High' WHEN 6 THEN '06. Medium' WHEN 7 THEN '07. Low' WHEN 8 THEN '08. Very Low' ELSE '09. Free Only' END AS value_tier_future,
  CASE IFNULL(x.tier_ltv_num, 9)
    WHEN 1 THEN '01. VIP1' WHEN 2 THEN '02. VIP 2' WHEN 3 THEN '03. Premium' WHEN 4 THEN '04. Very High'
    WHEN 5 THEN '05. High' WHEN 6 THEN '06. Medium' WHEN 7 THEN '07. Low' WHEN 8 THEN '08. Very Low' ELSE '09. Free Only' END AS value_tier_ltv,
  IFNULL(x.floor_applied, FALSE) AS floor_applied,
  CURRENT_TIMESTAMP() AS built_at
FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` d
LEFT JOIN tiered x USING (customer_key);
