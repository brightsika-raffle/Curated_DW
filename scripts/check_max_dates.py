import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=Credentials(token))

sql1 = "SELECT MAX(purchase_date) AS max_d FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_audience_label`"
sql2 = "SELECT MAX(purchase_date) AS max_d FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_ga4_prior_visit_daily`"

d1 = list(client.query(sql1).result())[0].max_d
d2 = list(client.query(sql2).result())[0].max_d

print(f"fact_order_audience_label max purchase_date: {d1}")
print(f"rpt_ga4_prior_visit_daily max purchase_date:  {d2}")
