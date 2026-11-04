import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

sys.stdout.reconfigure(encoding='utf-8')

SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
GCP_PROJECT = "rh-ga4-491819"
TABLE_ID = "rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily"

def get_client():
    token = subprocess.check_output(
        f"gcloud.cmd auth print-access-token --impersonate-service-account={SA}",
        shell=True, text=True
    ).strip()
    creds = Credentials(token)
    return bigquery.Client(project=GCP_PROJECT, location="europe-west2", credentials=creds)

def main():
    client = get_client()
    
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{TABLE_ID}` (
      spend_date DATE,
      campaign_id STRING,
      campaign_name STRING,
      adset_id STRING,
      adset_name STRING,
      user_segment_key STRING,
      spend_gbp NUMERIC,
      impressions INT64,
      link_clicks INT64,
      purchases NUMERIC,
      purchase_value_gbp NUMERIC,
      loaded_at TIMESTAMP
    )
    PARTITION BY spend_date
    CLUSTER BY campaign_id, user_segment_key
    OPTIONS(
      description="Meta Ads daily ad set level audience segments (prospecting, engaged, existing, unknown, null)."
    );
    """
    
    # Dry run
    job_config = bigquery.QueryJobConfig(dry_run=True)
    d_job = client.query(ddl, job_config=job_config)
    print(f"Dry run succeeded: {d_job.total_bytes_processed} bytes processed.")
    
    # Execute DDL
    job = client.query(ddl)
    job.result()
    print(f"Table created or confirmed: {TABLE_ID}")
    
    # Verify table schema
    tbl = client.get_table(TABLE_ID)
    print("\nVerified Table Schema:")
    for f in tbl.schema:
        print(f"  - {f.name:<20} {f.field_type}")
    print(f"Partitioning: {tbl.time_partitioning.field or 'DAY'}")
    print(f"Clustering: {tbl.clustering_fields}")

if __name__ == "__main__":
    main()

