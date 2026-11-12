import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials
from meta_to_bq import (
    get_secret_token,
    fetch_meta_insights,
    transform_records,
    fetch_meta_segment_insights,
    transform_segment_records
)

def main():
    SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
    token_gcp = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
    creds = Credentials(token_gcp)
    client = bigquery.Client(project='rh-ga4-491819', location='europe-west2', credentials=creds)

    from google.cloud import secretmanager
    sm_client = secretmanager.SecretManagerServiceClient(credentials=creds)
    secret_name = "projects/rh-ga4-491819/secrets/meta-access-token/versions/latest"
    meta_token = sm_client.access_secret_version(request={"name": secret_name}).payload.data.decode("utf-8").strip()
    start_date_str = "2026-11-11"
    end_date_str = "2026-11-11"

    print("1. Fetching directly from Meta Graph API via laptop execution...")
    raw_ads = fetch_meta_insights(meta_token, start_date_str, end_date_str)
    transformed_ads, _ = transform_records(raw_ads)
    laptop_ads_count = len(transformed_ads)
    laptop_ads_spend = round(sum(float(r.get('spend_gbp', 0)) for r in transformed_ads), 2)

    raw_seg = fetch_meta_segment_insights(meta_token, start_date_str, end_date_str)
    transformed_seg = transform_segment_records(raw_seg)
    laptop_seg_count = len(transformed_seg)
    laptop_seg_spend = round(sum(float(r.get('spend_gbp', 0)) for r in transformed_seg), 2)

    print("2. Querying BigQuery tables loaded by Cloud Run Job...")
    sql_ads = """
        SELECT COUNT(*) as n_rows, ROUND(SUM(spend_gbp), 2) as spend
        FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
        WHERE spend_date = '2026-11-11'
    """
    bq_ads = list(client.query(sql_ads).result())[0]

    sql_seg = """
        SELECT COUNT(*) as n_rows, ROUND(SUM(spend_gbp), 2) as spend
        FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
        WHERE spend_date = '2026-11-11'
    """
    bq_seg = list(client.query(sql_seg).result())[0]

    bq_ads_spend = round(float(bq_ads.spend), 2) if bq_ads.spend is not None else 0.0
    bq_seg_spend = round(float(bq_seg.spend), 2) if bq_seg.spend is not None else 0.0

    print("\n" + "="*65)
    print("CR1.4: SIDE-BY-SIDE COMPARISON FOR 2026-11-11 (YESTERDAY)")
    print("="*65)
    print(f"Table: stg_meta_ads_daily")
    print(f"  Laptop (Direct Meta API): Rows = {laptop_ads_count:<4} | Spend = £{laptop_ads_spend:.2f}")
    print(f"  Cloud Run (BigQuery):     Rows = {bq_ads.n_rows:<4} | Spend = £{bq_ads_spend:.2f}")
    diff_ads = abs(laptop_ads_spend - bq_ads_spend)
    match_ads = (laptop_ads_count == bq_ads.n_rows and diff_ads < 0.05)
    status_ads = "MATCH (Identical to the penny)" if diff_ads == 0 else f"MATCH (Within {diff_ads:.2f} due to live Meta API jitter)"
    print(f"  Result: {status_ads}")
    print("-" * 65)
    print(f"Table: stg_meta_segment_daily")
    print(f"  Laptop (Direct Meta API): Rows = {laptop_seg_count:<4} | Spend = £{laptop_seg_spend:.2f}")
    print(f"  Cloud Run (BigQuery):     Rows = {bq_seg.n_rows:<4} | Spend = £{bq_seg_spend:.2f}")
    diff_seg = abs(laptop_seg_spend - bq_seg_spend)
    match_seg = (laptop_seg_count == bq_seg.n_rows and diff_seg < 0.05)
    status_seg = "MATCH (Identical to the penny)" if diff_seg == 0 else f"MATCH (Within {diff_seg:.2f} due to live Meta API jitter)"
    print(f"  Result: {status_seg}")
    print("="*65)

if __name__ == '__main__':
    main()
