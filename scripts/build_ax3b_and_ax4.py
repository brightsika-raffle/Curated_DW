import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=Credentials(token))

# AX3b: Create View
sql_view = """
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
"""

print("Creating view vw_meta_audience_compare_daily...")
job_v = client.query(sql_view)
job_v.result()
print("View created successfully.")

# AX3b Verification: Query view for 1 Sep to 3 Nov
sql_ax3b_check = """
SELECT
  bucket,
  SUM(wh_orders) AS wh_orders,
  ROUND(SUM(meta_purchases), 2) AS meta_purchases
FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_meta_audience_compare_daily`
WHERE report_date BETWEEN '2026-09-01' AND '2026-11-03'
GROUP BY 1
ORDER BY 1
"""
print("\n--- AX3b: View Summary (1 Sep to 3 Nov) ---")
res_ax3b = list(client.query(sql_ax3b_check).result())
for r in res_ax3b:
    print(f"bucket: {r.bucket:<16} | wh_orders: {r.wh_orders} | meta_purchases: {r.meta_purchases}")

# AX4: Section 4 check against Meta
sql_ax4 = """
WITH w AS (
  SELECT
    audience_label AS seg,
    COUNT(*) AS orders,
    ROUND(SUM(revenue_gbp), 2) AS rev
  FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`
  WHERE channel_group = 'Meta'
    AND audience_label != 'free'
    AND NOT is_renewal
    AND purchase_date BETWEEN '2026-09-01' AND '2026-11-03'
  GROUP BY 1
),
m AS (
  SELECT
    CASE user_segment_key WHEN 'prospecting' THEN 'new' ELSE user_segment_key END AS seg,
    ROUND(SUM(purchases), 2) AS meta_purchases
  FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
  WHERE spend_date BETWEEN '2026-09-01' AND '2026-11-03'
  GROUP BY 1
)
SELECT
  seg,
  w.orders AS wh_orders,
  w.rev AS wh_revenue_gbp,
  m.meta_purchases
FROM w
FULL JOIN m USING (seg)
ORDER BY seg
"""
print("\n--- AX4: Section 4 Check Against Meta ---")
res_ax4 = list(client.query(sql_ax4).result())
for r in res_ax4:
    print(f"seg: {r.seg:<12} | wh_orders: {r.wh_orders} | wh_revenue_gbp: £{r.wh_revenue_gbp} | meta_purchases: {r.meta_purchases}")

# AX4: GA4 prior-visit share for GA4 'new' purchases over 1 Sep to 3 Nov
sql_ga4_new = """
SELECT
  ga4_customer_type,
  SUM(ga4_purchases) AS total_purchases,
  SUM(with_prior_visit_30d) AS with_prior_visit,
  ROUND(SAFE_DIVIDE(SUM(with_prior_visit_30d), SUM(ga4_purchases)) * 100, 2) AS prior_visit_share_pct
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`
WHERE purchase_date BETWEEN '2026-09-01' AND '2026-11-03'
GROUP BY 1
ORDER BY 1
"""
print("\n--- AX4: GA4 Prior-Visit Share (1 Sep to 3 Nov) ---")
res_ga4 = list(client.query(sql_ga4_new).result())
for r in res_ga4:
    print(f"ga4_customer_type: {r.ga4_customer_type:<10} | purchases: {r.total_purchases} | with_prior: {r.with_prior_visit} | share: {r.prior_visit_share_pct}%")

