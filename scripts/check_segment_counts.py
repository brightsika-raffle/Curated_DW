import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=Credentials(token))

sql = """
SELECT
  spend_date,
  COUNT(*) AS row_count,
  ROUND(SUM(spend_gbp), 2) AS total_spend,
  ROUND(SUM(purchase_value_gbp), 2) AS total_plat_rev,
  COUNT(DISTINCT user_segment_key) AS distinct_segments
FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
WHERE spend_date >= '2026-11-01'
GROUP BY 1
ORDER BY 1
"""
job = client.query(sql)
print("spend_date | rows | spend | plat_rev | segments")
print("-" * 50)
for r in job.result():
    print(f"{r.spend_date} | {r.row_count:>4} | £{r.total_spend:>8.2f} | £{r.total_plat_rev:>8.2f} | {r.distinct_segments}")

