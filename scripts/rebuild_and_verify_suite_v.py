import os, sys, subprocess, json
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

PROJECT_ID = "rh-ga4-491819"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_client():
    token = subprocess.check_output(
        f"gcloud auth print-access-token --impersonate-service-account={SA}",
        shell=True, text=True
    ).strip()
    creds = Credentials(token)
    return bigquery.Client(project=PROJECT_ID, location="europe-west2", credentials=creds)

def main():
    client = get_client()

    # Extract rpt_paid_media_entity_daily SQL from sql/nightly_build_and_test.sql
    with open("sql/nightly_build_and_test.sql", "r", encoding="utf-8") as f:
        sql_content = f.read()

    # Find the CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily` statement
    start_marker = "CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`"
    next_marker = "CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_trial_conversion`"

    start_idx = sql_content.find(start_marker)
    if start_idx == -1:
        raise ValueError("Could not find start marker for rpt_paid_media_entity_daily")
    end_idx = sql_content.find(next_marker, start_idx)
    if end_idx == -1:
        raise ValueError("Could not find next marker after rpt_paid_media_entity_daily")

    table_sql = sql_content[start_idx:end_idx].strip()
    # Strip trailing semicolon if present
    if table_sql.endswith(";"):
        table_sql = table_sql[:-1].strip()

    print(f"Extracted table SQL: length = {len(table_sql)} chars")

    # 1. Dry run
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry_run_job = client.query(table_sql, job_config=job_config)
    bytes_gb = dry_run_job.total_bytes_processed / (1024**3)
    print(f"Dry run succeeded: {dry_run_job.total_bytes_processed:,} bytes ({bytes_gb:.4f} GB)")

    if bytes_gb > 15.0:
        raise ValueError(f"Dry run exceeded 15 GB ceiling: {bytes_gb:.2f} GB")

    # 2. Execute table rebuild
    print("Executing rebuild of rpt_paid_media_entity_daily...")
    job = client.query(table_sql)
    job.result()
    print(f"Rebuild completed! Job ID: {job.job_id}")

    # 3. Check row count and date range
    check_query = """
    SELECT
      COUNT(*) AS total_rows,
      MIN(d) AS min_d,
      MAX(d) AS max_d,
      COUNT(DISTINCT entity_id) AS distinct_entities
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
    """
    res = list(client.query(check_query).result())[0]
    print(f"Rebuilt table: {res.total_rows:,} rows, dates: {res.min_d} to {res.max_d}, {res.distinct_entities} entities")

if __name__ == "__main__":
    main()

