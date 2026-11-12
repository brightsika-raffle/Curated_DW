import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=Credentials(token))

create_table_sql = """
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
"""

print("[Step 1] Creating rpt_meta_audience_compare_daily table...")
job = client.query(create_table_sql)
job.result()
print(f"Table created successfully. Job ID: {job.job_id}")

print("\n[Step 2] Verifying row count...")
cnt_sql = "SELECT COUNT(*) AS total_rows, MIN(report_date) AS min_d, MAX(report_date) AS max_d FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_meta_audience_compare_daily`"
cnt_row = list(client.query(cnt_sql).result())[0]
print(f"Total Rows: {cnt_row.total_rows}, Min Date: {cnt_row.min_d}, Max Date: {cnt_row.max_d}")

print("\n[Step 3] Verifying 1 Sep to 3 Nov totals by bucket...")
check_sql = """
SELECT
  bucket,
  SUM(wh_orders) AS wh_orders,
  ROUND(SUM(wh_revenue_gbp), 2) AS wh_rev_gbp,
  SUM(meta_purchases) AS meta_purchases,
  ROUND(SUM(meta_spend_gbp), 2) AS meta_spend_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_meta_audience_compare_daily`
WHERE report_date BETWEEN '2026-09-01' AND '2026-11-03'
GROUP BY bucket
ORDER BY bucket;
"""
rows = list(client.query(check_sql).result())
for r in rows:
    print(f"bucket: {r.bucket} | wh_orders: {r.wh_orders} | wh_rev: £{r.wh_rev_gbp} | meta_purchases: {r.meta_purchases} | meta_spend: £{r.meta_spend_gbp}")

# Verification assertions
b_map = {r.bucket: r for r in rows}
assert b_map['existing'].wh_orders == 4849, f"Expected 4849 existing wh_orders, got {b_map['existing'].wh_orders}"
assert b_map['existing'].meta_purchases == 5587, f"Expected 5587 existing meta_purchases, got {b_map['existing'].meta_purchases}"
assert b_map['not_yet_customer'].wh_orders == 8151, f"Expected 8151 not_yet_customer wh_orders, got {b_map['not_yet_customer'].wh_orders}"
assert b_map['not_yet_customer'].meta_purchases == 11669, f"Expected 11669 not_yet_customer meta_purchases, got {b_map['not_yet_customer'].meta_purchases}"
print("\nAssertions passed: figures match expected values exactly!")

print("\n[Step 4] Dropping view vw_meta_audience_compare_daily...")
drop_sql = "DROP VIEW IF EXISTS `rh-ga4-491819.rh_curated_Bright_Test.vw_meta_audience_compare_daily`"
client.query(drop_sql).result()
print("View dropped successfully.")
