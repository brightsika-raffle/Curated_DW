-- Staging build of customer_ltv_horizons with prepaid 3/6/12-month plan fix
-- Target: rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons_stg (europe-west2)
-- Fix: spreads each prepaid subscription payment evenly over the `months` it covers
--      (amt / months across DATE_ADD(d, INTERVAL k MONTH) for k = 0..months-1),
--      so monthly_value, tier, tenure, and survival-curve EFV value prepaid customers
--      like monthly subscribers, while realised_value retains actual cash received
--      and future EFV starts after the prepaid period (current_tenure_month + 1).

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons_stg`
CLUSTER BY customer_key
OPTIONS (description = 'Staging: Customer LTV at 3/6/12/36-month horizons with prepaid 3/6/12-month spreading fix.')
AS
WITH fs AS (
  SELECT subscription_key, customer_key AS customer_id, status, GREATEST(COALESCE(months, 1), 1) AS months
  FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_subscriptions`
),
tx_raw AS (
  SELECT
    t.customer_key AS customer_id,
    DATE(t.purchase_date) AS d,
    t.revenue_gbp AS amt,
    t.subscription_key IS NULL AS one_off,
    t.subscription_key,
    t.purchase_date,
    IF(t.subscription_key IS NULL, 1, COALESCE(fs.months, 1)) AS cover_months
  FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_fact_transactions` t
  LEFT JOIN fs USING (subscription_key)
  WHERE NOT IFNULL(t.is_free_entry, FALSE)
    AND t.revenue_gbp > 0
    AND t.customer_key != 'UNKNOWN_DELETED'
),
as_of AS (SELECT MAX(d) AS as_of_date FROM tx_raw),

-- Spread prepaid subscription payments evenly over the months they cover for forward-value & tenure modelling
tx AS (
  SELECT
    tr.customer_id,
    DATE_ADD(tr.d, INTERVAL m_off MONTH) AS d,
    tr.amt / tr.cover_months AS amt,
    tr.one_off,
    tr.subscription_key,
    tr.d AS orig_d
  FROM tx_raw tr,
  UNNEST(GENERATE_ARRAY(0, tr.cover_months - 1)) AS m_off
),

-- ---------- Snapshot dates ----------
snaps AS (
  SELECT 'recent' AS kind, h, s
  FROM as_of, UNNEST([3, 6, 12]) AS h,
  UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(DATE_TRUNC(as_of_date, MONTH), INTERVAL 48 MONTH), as_of_date, INTERVAL 1 MONTH)) AS m,
  UNNEST([LAST_DAY(m)]) AS s
  WHERE DATE_ADD(s, INTERVAL h MONTH) <= as_of_date
    AND s > DATE_SUB(LAST_DAY(DATE_SUB(as_of_date, INTERVAL h + 1 MONTH)), INTERVAL 12 MONTH)
  UNION ALL
  SELECT 'old', h, LAST_DAY(m)
  FROM UNNEST([12, 36]) AS h,
  UNNEST(GENERATE_DATE_ARRAY(DATE '2022-10-01', DATE '2023-09-01', INTERVAL 1 MONTH)) AS m
),
snap_dates AS (SELECT DISTINCT s FROM snaps),

hist_state AS (
  SELECT sd.s, t.customer_id,
    COUNTIF(NOT t.one_off) > 0 AS has_sub,
    COUNTIF(t.one_off) AS depth,
    DATE_DIFF(sd.s, MAX(IF(t.one_off, t.d, NULL)), DAY) AS silent
  FROM snap_dates sd JOIN tx t ON t.orig_d <= sd.s AND t.d <= sd.s
  GROUP BY sd.s, t.customer_id
  HAVING depth > 0
),
hist_banded AS (
  SELECT s, customer_id, has_sub,
    CASE WHEN depth = 1 THEN '1' WHEN depth = 2 THEN '2' WHEN depth <= 4 THEN '3-4'
         WHEN depth <= 9 THEN '5-9' ELSE '10+' END AS depth_band,
    CASE WHEN silent <= 30 THEN '0-30' WHEN silent <= 90 THEN '31-90' WHEN silent <= 180 THEN '91-180'
         WHEN silent <= 365 THEN '181-365' ELSE '366+' END AS silent_band
  FROM hist_state
),
hist_fwd AS (
  SELECT sn.kind, sn.h, hb.s, hb.customer_id, hb.has_sub, hb.depth_band, hb.silent_band,
    COALESCE(SUM(IF(t.d <= DATE_ADD(hb.s, INTERVAL sn.h MONTH) AND (t.one_off OR NOT hb.has_sub), t.amt, 0)), 0) AS fwd_value
  FROM hist_banded hb
  JOIN snaps sn ON sn.s = hb.s
  LEFT JOIN tx t ON t.customer_id = hb.customer_id AND t.d > hb.s AND t.d <= DATE_ADD(hb.s, INTERVAL 36 MONTH)
  GROUP BY 1, 2, 3, 4, 5, 6, 7
),
state_curve AS (
  SELECT kind, h, has_sub, depth_band, silent_band, COUNT(*) AS n_obs, AVG(fwd_value) AS efv
  FROM hist_fwd GROUP BY 1, 2, 3, 4, 5
),
ratio_state AS (
  SELECT has_sub, depth_band, silent_band,
    SAFE_DIVIDE(MAX(IF(h = 36, efv, NULL)), MAX(IF(h = 12, efv, NULL))) AS r36_12,
    MIN(n_obs) AS n_obs
  FROM state_curve WHERE kind = 'old' GROUP BY 1, 2, 3
),
ratio_grp AS (
  SELECT has_sub, SAFE_DIVIDE(SUM(IF(h = 36, fwd_value, 0)), SUM(IF(h = 12, fwd_value, 0))) AS r36_12,
    COUNTIF(h = 12) AS n_obs
  FROM hist_fwd WHERE kind = 'old' GROUP BY 1
),
ratio_all AS (
  SELECT SAFE_DIVIDE(SUM(IF(h = 36, fwd_value, 0)), SUM(IF(h = 12, fwd_value, 0))) AS r36_12
  FROM hist_fwd WHERE kind = 'old'
),
oneoff_curve AS (
  SELECT c.has_sub, c.depth_band, c.silent_band,
    MAX(IF(c.h = 3, c.efv, NULL))  AS efv_3m,
    MAX(IF(c.h = 6, c.efv, NULL))  AS efv_6m,
    MAX(IF(c.h = 12, c.efv, NULL)) AS efv_12m,
    MAX(IF(c.h = 12, c.efv, NULL)) * GREATEST(1, ANY_VALUE(COALESCE(
      IF(rs.n_obs >= 200, rs.r36_12, NULL),
      IF(rg.n_obs >= 1000, rg.r36_12, NULL),
      ra.r36_12, 1))) AS efv_36m
  FROM state_curve c
  LEFT JOIN ratio_state rs USING (has_sub, depth_band, silent_band)
  LEFT JOIN ratio_grp rg USING (has_sub)
  CROSS JOIN ratio_all ra
  WHERE c.kind = 'recent'
  GROUP BY 1, 2, 3
),

-- ---------- Current state (realised uses actual cash tx_raw) ----------
cur_state AS (
  SELECT t.customer_id,
    COUNTIF(NOT t.one_off) > 0 AS has_sub,
    COUNTIF(t.one_off) AS depth,
    DATE_DIFF(a.as_of_date, MAX(IF(t.one_off, t.d, NULL)), DAY) AS silent,
    SUM(IF(t.one_off, t.amt, 0)) AS oneoff_realised
  FROM tx_raw t CROSS JOIN as_of a
  GROUP BY t.customer_id, a.as_of_date
),
cur_oneoff AS (
  SELECT cs.customer_id, cs.oneoff_realised, oc.efv_3m,
    GREATEST(oc.efv_3m, oc.efv_6m) AS efv_6m,
    GREATEST(oc.efv_3m, oc.efv_6m, oc.efv_12m) AS efv_12m,
    GREATEST(oc.efv_3m, oc.efv_6m, oc.efv_12m, oc.efv_36m) AS efv_36m
  FROM cur_state cs
  JOIN oneoff_curve oc
    ON oc.has_sub = cs.has_sub
   AND oc.depth_band = CASE WHEN cs.depth = 1 THEN '1' WHEN cs.depth = 2 THEN '2' WHEN cs.depth <= 4 THEN '3-4'
                            WHEN cs.depth <= 9 THEN '5-9' ELSE '10+' END
   AND oc.silent_band = CASE WHEN cs.silent <= 30 THEN '0-30' WHEN cs.silent <= 90 THEN '31-90' WHEN cs.silent <= 180 THEN '91-180'
                             WHEN cs.silent <= 365 THEN '181-365' ELSE '366+' END
  WHERE cs.depth > 0
),

-- ---------- Subscriptions: survival curve on monthly-equivalent payments ----------
sub_pay AS (
  SELECT customer_id, subscription_key, d, amt,
    CASE
      WHEN amt IN (10, 15) THEN 'Low'
      WHEN amt IN (20, 25, 30) THEN 'Mid'
      WHEN amt IN (35, 50) THEN 'High'
      WHEN amt IN (55, 75) THEN 'Very High'
      ELSE CASE
        WHEN ABS(amt - 10) <= ABS(amt - 15)
             AND ABS(amt - 10) < LEAST(ABS(amt-20), ABS(amt-30), ABS(amt-35), ABS(amt-50), ABS(amt-55), ABS(amt-75)) THEN 'Low'
        WHEN LEAST(ABS(amt-20), ABS(amt-25), ABS(amt-30)) <= LEAST(ABS(amt-35), ABS(amt-50), ABS(amt-55), ABS(amt-75)) THEN 'Mid'
        WHEN LEAST(ABS(amt-35), ABS(amt-50)) <= LEAST(ABS(amt-55), ABS(amt-75)) THEN 'High'
        ELSE 'Very High' END
    END AS tier
  FROM tx WHERE NOT one_off
),
cust_modal AS (
  SELECT customer_id, tier AS modal_tier FROM (
    SELECT customer_id, tier, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY COUNT(*) DESC) AS rn
    FROM sub_pay GROUP BY customer_id, tier) WHERE rn = 1
),
tenure_detail AS (
  SELECT t.customer_id, IF(m.modal_tier = 'Very High', 'High', m.modal_tier) AS curve_tier,
    DATE_DIFF(MAX(t.d), MIN(t.d), MONTH) + 1 AS tenure_months,
    NOT LOGICAL_OR(IFNULL(st.active, FALSE)) AS is_churned
  FROM sub_pay t
  JOIN cust_modal m USING (customer_id)
  LEFT JOIN (SELECT customer_id, COUNTIF(status = 'ACTIVE') > 0 AS active FROM fs GROUP BY 1) st USING (customer_id)
  GROUP BY t.customer_id, curve_tier
),
raw_hazard AS (
  SELECT curve_tier, m AS month, COUNT(*) AS at_risk,
    SAFE_DIVIDE(COUNTIF(m = tenure_months AND is_churned), COUNT(*)) AS hazard_rate,
    COUNT(*) >= 50 AS is_reliable
  FROM tenure_detail, UNNEST(GENERATE_ARRAY(0, tenure_months)) AS m
  GROUP BY 1, 2
),
frozen_rate AS (
  SELECT curve_tier, AVG(hazard_rate) AS frozen_hazard FROM (
    SELECT curve_tier, hazard_rate, ROW_NUMBER() OVER (PARTITION BY curve_tier ORDER BY month DESC) AS rn
    FROM raw_hazard WHERE is_reliable) WHERE rn <= 3 GROUP BY 1
),
last_reliable AS (SELECT curve_tier, MAX(month) AS last_reliable FROM raw_hazard WHERE is_reliable GROUP BY 1),
survival_curve AS (
  SELECT curve_tier AS tier, month,
    EXP(SUM(LN(1 - hazard_rate)) OVER (PARTITION BY curve_tier ORDER BY month ROWS UNBOUNDED PRECEDING)) AS survival_probability
  FROM (
    SELECT f.curve_tier, f.month, IF(f.month <= l.last_reliable, h.hazard_rate, fr.frozen_hazard) AS hazard_rate
    FROM (SELECT t.curve_tier, month FROM (SELECT DISTINCT curve_tier FROM raw_hazard) t, UNNEST(GENERATE_ARRAY(0, 120)) AS month) f
    JOIN last_reliable l ON l.curve_tier = f.curve_tier
    JOIN frozen_rate fr ON fr.curve_tier = f.curve_tier
    LEFT JOIN raw_hazard h ON h.curve_tier = f.curve_tier AND h.month = f.month
  )
),
sub_modal AS (
  SELECT subscription_key, tier, avg_amt AS monthly_value FROM (
    SELECT subscription_key, tier, AVG(amt) AS avg_amt,
      ROW_NUMBER() OVER (PARTITION BY subscription_key ORDER BY COUNT(*) DESC) AS rn
    FROM sub_pay GROUP BY 1, 2) WHERE rn = 1
),
subs AS (
  SELECT a.subscription_key AS subscription_id, a.customer_id,
    IF(m.tier = 'Very High', 'High', m.tier) AS tier, f.status, m.monthly_value,
    a.current_tenure_month, a.realised_value, sc.survival_probability AS current_survival_probability
  FROM (
    SELECT subscription_key, ANY_VALUE(customer_id) AS customer_id,
      COUNT(*) AS current_tenure_month, SUM(amt) AS realised_value
    FROM sub_pay GROUP BY 1
  ) a
  JOIN sub_modal m USING (subscription_key)
  JOIN fs f USING (subscription_key)
  LEFT JOIN survival_curve sc ON sc.tier = IF(m.tier = 'Very High', 'High', m.tier) AND sc.month = LEAST(a.current_tenure_month, 120)
),
sub_future AS (
  SELECT s.subscription_id, h, m
  FROM subs s, UNNEST([3, 6, 12, 36]) AS h,
  UNNEST(GENERATE_ARRAY(s.current_tenure_month + 1, s.current_tenure_month + h)) AS m
  WHERE s.status = 'ACTIVE'
),
sub_efv_h AS (
  SELECT sf.subscription_id, sf.h,
    SUM(SAFE_DIVIDE(c.survival_probability, s.current_survival_probability) * s.monthly_value) AS efv
  FROM sub_future sf
  JOIN subs s USING (subscription_id)
  JOIN survival_curve c
    ON c.tier = s.tier AND c.month = LEAST(sf.m, 120)
  GROUP BY 1, 2
),
sub_realised AS (
  SELECT customer_id, SUM(realised_value) AS sub_realised, LOGICAL_OR(status = 'ACTIVE') AS has_active_sub
  FROM subs GROUP BY 1
),
sub_efv AS (
  SELECT s.customer_id,
    SUM(IF(e.h = 3, e.efv, 0))  AS efv_3m,
    SUM(IF(e.h = 6, e.efv, 0))  AS efv_6m,
    SUM(IF(e.h = 12, e.efv, 0)) AS efv_12m,
    SUM(IF(e.h = 36, e.efv, 0)) AS efv_36m
  FROM subs s JOIN sub_efv_h e USING (subscription_id)
  GROUP BY 1
),
all_customers AS (
  SELECT customer_id FROM cur_state
  UNION DISTINCT SELECT customer_id FROM sub_realised
)

SELECT
  c.customer_id AS customer_key,
  CASE WHEN sr.has_active_sub THEN 'Active subscriber'
       WHEN sr.customer_id IS NOT NULL THEN 'Lapsed subscriber'
       ELSE 'One-off only' END AS ltv_segment,
  ROUND(COALESCE(o.oneoff_realised, 0) + COALESCE(sr.sub_realised, 0), 2) AS realised_value,
  ROUND(COALESCE(o.efv_3m, 0),  2) AS oneoff_efv_3m,
  ROUND(COALESCE(o.efv_12m, 0), 2) AS oneoff_efv_12m,
  ROUND(COALESCE(se.efv_3m, 0),  2) AS sub_efv_3m,
  ROUND(COALESCE(se.efv_12m, 0), 2) AS sub_efv_12m,
  ROUND(COALESCE(o.efv_3m, 0)  + COALESCE(se.efv_3m, 0),  2) AS efv_3m,
  ROUND(COALESCE(o.efv_6m, 0)  + COALESCE(se.efv_6m, 0),  2) AS efv_6m,
  ROUND(COALESCE(o.efv_12m, 0) + COALESCE(se.efv_12m, 0), 2) AS efv_12m,
  ROUND(COALESCE(o.efv_36m, 0) + COALESCE(se.efv_36m, 0), 2) AS efv_36m,
  ROUND(COALESCE(o.oneoff_realised, 0) + COALESCE(sr.sub_realised, 0) + COALESCE(o.efv_3m, 0)  + COALESCE(se.efv_3m, 0),  2) AS ltv_3m,
  ROUND(COALESCE(o.oneoff_realised, 0) + COALESCE(sr.sub_realised, 0) + COALESCE(o.efv_6m, 0)  + COALESCE(se.efv_6m, 0),  2) AS ltv_6m,
  ROUND(COALESCE(o.oneoff_realised, 0) + COALESCE(sr.sub_realised, 0) + COALESCE(o.efv_12m, 0) + COALESCE(se.efv_12m, 0), 2) AS ltv_12m,
  ROUND(COALESCE(o.oneoff_realised, 0) + COALESCE(sr.sub_realised, 0) + COALESCE(o.efv_36m, 0) + COALESCE(se.efv_36m, 0), 2) AS ltv_36m,
  (SELECT as_of_date FROM as_of) AS as_of_date,
  CURRENT_TIMESTAMP() AS built_at
FROM all_customers c
LEFT JOIN cur_oneoff o USING (customer_id)
LEFT JOIN sub_realised sr USING (customer_id)
LEFT JOIN sub_efv se USING (customer_id);
