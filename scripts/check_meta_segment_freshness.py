import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=Credentials(token))

sql = """
SELECT
  MAX(spend_date) AS max_spend_date,
  CURRENT_DATE('Europe/London') AS today_london,
  DATE_DIFF(CURRENT_DATE('Europe/London'), MAX(spend_date), DAY) AS meta_segment_days
FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
"""
row = list(client.query(sql).result())[0]
print(f"Max spend date: {row.max_spend_date}, Today: {row.today_london}, Diff: {row.meta_segment_days} days")
