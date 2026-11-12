import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=Credentials(token))

# Step 1: Create Table
sql_create = """
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`
PARTITION BY purchase_date
CLUSTER BY audience_label, channel_group
OPTIONS (
  description = 'One row per accepted order with the warehouse audience label (existing / engaged / new / free). Built from curated fact_transactions. Compare with Meta user_segment_key in stg_meta_segment_daily.',
  labels = [('layer', 'fact'), ('owner', 'bright_sika')]
)
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
"""

# Dry-run create
dry_config = bigquery.QueryJobConfig(dry_run=True)
dry_job = client.query(sql_create, job_config=dry_config)
print(f"AX1 create dry-run bytes: {dry_job.total_bytes_processed:,} bytes ({dry_job.total_bytes_processed / (1024**2):.2f} MB)")

print("Executing AX1 table creation...")
job = client.query(sql_create)
job.result()
print(f"AX1 Table created successfully. Job ID: {job.job_id}")

# Step 2: Set Column Descriptions
col_descriptions = {
    "transaction_id": "Warehouse order identifier (24 characters).",
    "purchase_date": "Calendar date of purchase in Europe/London.",
    "purchase_at": "Timestamp of purchase in Europe/London.",
    "customer_key": "Customer identifier (UNKNOWN_DELETED excluded from label calculation).",
    "revenue_gbp": "Order revenue in pounds sterling (converted from pence in fact_transactions).",
    "channel_last_touch_key": "Surrogate key for last-touch attribution channel.",
    "channel_group": "Marketing channel group from dim_channel_last_touch (e.g. Meta, Google, Direct, Unattributed).",
    "utm_campaign": "Campaign identifier or name from last-touch landing URL.",
    "audience_label": "Warehouse audience label: existing (prior paid order), engaged (prior £0/free order or account older than order day), new (none of the above), free (order itself is £0).",
    "label_reason": "Exact rule explaining the audience label: 'order is £0', 'prior paid order', 'prior £0 order only', 'account older than order day', 'first order, account created same day'.",
    "prior_paid_orders": "Count of customer previous accepted orders with revenue_gbp > 0.",
    "prior_free_orders": "Count of customer previous accepted orders with revenue_gbp = 0.",
    "is_renewal": "Boolean flag indicating whether order is a recurring subscription renewal (sub_payment_no > 1).",
    "loaded_at": "Timestamp when this table build was executed."
}

table_ref = client.get_table("rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label")
new_schema = []
for field in table_ref.schema:
    desc = col_descriptions.get(field.name, field.description)
    new_schema.append(field.to_api_repr())
    new_schema[-1]["description"] = desc

table_ref.schema = [bigquery.SchemaField.from_api_repr(f) for f in new_schema]
client.update_table(table_ref, ["schema"])
print("Updated column descriptions on fact_order_audience_label.")

# Verify count and sample
verify_sql = """
SELECT
  COUNT(*) AS total_rows,
  COUNTIF(audience_label = 'existing') AS existing_orders,
  COUNTIF(audience_label = 'engaged') AS engaged_orders,
  COUNTIF(audience_label = 'new') AS new_orders,
  COUNTIF(audience_label = 'free') AS free_orders
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`
"""
res = list(client.query(verify_sql).result())[0]
print(f"Total Rows: {res.total_rows:,} | existing: {res.existing_orders:,} | engaged: {res.engaged_orders:,} | new: {res.new_orders:,} | free: {res.free_orders:,}")

