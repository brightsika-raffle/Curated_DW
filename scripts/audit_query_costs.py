import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

sys.stdout.reconfigure(encoding='utf-8')

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

    sql = """
    SELECT
      job_id,
      creation_time,
      total_bytes_billed,
      ROUND(COALESCE(total_bytes_billed, 0) / (1024*1024*1024), 3) AS billed_gb,
      ROUND((COALESCE(total_bytes_billed, 0) / (1024*1024*1024*1024)) * 6.25, 4) AS cost_usd,
      query
    FROM `region-europe-west2`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 HOUR)
      AND job_type = 'QUERY'
      AND state = 'DONE'
    ORDER BY creation_time DESC
    """
    rows = list(client.query(sql).result())
    print(f"Total query jobs found in last 3 hours: {len(rows)}")
    total_billed = sum(r.total_bytes_billed or 0 for r in rows)
    total_cost = sum(r.cost_usd or 0 for r in rows)
    print(f"Total billed: {total_billed / (1024**3):.3f} GB | Approx on-demand cost: ${total_cost:.4f}")
    print("-" * 120)
    print(f"{'Creation Time':<30} | {'Job ID':<42} | {'Billed GB':<10} | {'Cost USD':<10} | {'Query Snippet'}")
    print("-" * 120)
    for r in rows:
        q_snippet = (r.query.replace('\n', ' ')[:50] + '...') if r.query else ''
        b_gb = r.billed_gb if r.billed_gb is not None else 0.0
        c_usd = r.cost_usd if r.cost_usd is not None else 0.0
        print(f"{str(r.creation_time):<30} | {r.job_id:<42} | {b_gb:>9.3f} | ${c_usd:>8.4f} | {q_snippet}")

if __name__ == "__main__":
    main()
