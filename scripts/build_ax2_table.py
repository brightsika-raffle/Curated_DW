import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=Credentials(token))

# Full build SQL
sql_full = """
CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`
PARTITION BY purchase_date
OPTIONS (
  description = 'GA4 purchases by day and GA4 customer_type, with the share that had a session on an earlier day in the previous 30 days. Estimate only; cannot be joined to orders.',
  labels = [('layer', 'report'), ('owner', 'bright_sika')]
)
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
"""

# Incremental SQL
sql_incremental = """
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
pur AS (
  SELECT tx, ANY_VALUE(u) AS u, MIN(ts) AS ts, ANY_VALUE(ct) AS ct
  FROM ev
  WHERE e = 'purchase' AND tx IS NOT NULL
  GROUP BY tx
),
vis AS (
  SELECT u, DATE(ts, 'Europe/London') AS d
  FROM ev
  WHERE e = 'session_start'
  GROUP BY 1, 2
),
pv AS (
  SELECT p.tx, DATE(p.ts, 'Europe/London') AS purchase_date, p.ct,
    LOGICAL_OR(v.d < DATE(p.ts, 'Europe/London') AND v.d >= DATE_SUB(DATE(p.ts, 'Europe/London'), INTERVAL 30 DAY)) AS prior_visit_30d
  FROM pur p
  LEFT JOIN vis v USING (u)
  WHERE DATE(p.ts, 'Europe/London') >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 3 DAY)
  GROUP BY 1, 2, 3
)
SELECT purchase_date, COALESCE(ct, 'unknown') AS ga4_customer_type, COUNT(*) AS ga4_purchases,
  COUNTIF(prior_visit_30d) AS with_prior_visit_30d, SAFE_DIVIDE(COUNTIF(prior_visit_30d), COUNT(*)) AS prior_visit_share,
  CURRENT_TIMESTAMP() AS loaded_at
FROM pv
GROUP BY 1, 2;
"""

# Dry run full build
dry_full = client.query(sql_full, job_config=bigquery.QueryJobConfig(dry_run=True))
print(f"Full build dry run bytes: {dry_full.total_bytes_processed:,} bytes ({dry_full.total_bytes_processed / (1024**3):.2f} GB)")

# Execute full build first so table exists
print("Executing full build of rpt_ga4_prior_visit_daily...")
job = client.query(sql_full)
job.result()
print(f"Full build completed successfully. Job ID: {job.job_id}")

# Dry run incremental build now that table exists
dry_inc = client.query(sql_incremental, job_config=bigquery.QueryJobConfig(dry_run=True))
print(f"Incremental build dry run bytes: {dry_inc.total_bytes_processed:,} bytes ({dry_inc.total_bytes_processed / (1024**3):.2f} GB)")

# Verification
ver_sql = """
SELECT
  MIN(purchase_date) AS min_date,
  MAX(purchase_date) AS max_date,
  COUNT(*) AS total_rows,
  SUM(ga4_purchases) AS total_ga4_purchases,
  SUM(with_prior_visit_30d) AS total_with_prior_visit,
  ROUND(SAFE_DIVIDE(SUM(with_prior_visit_30d), SUM(ga4_purchases)) * 100, 2) AS overall_prior_visit_share_pct
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`
"""
res = list(client.query(ver_sql).result())[0]
print(f"rpt_ga4_prior_visit_daily: min={res.min_date}, max={res.max_date}, rows={res.total_rows}, purchases={res.total_ga4_purchases:,}, with_prior={res.total_with_prior_visit:,}, share={res.overall_prior_visit_share_pct}%")
