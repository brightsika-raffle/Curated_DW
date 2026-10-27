import os
import subprocess
import sys
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_bq_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, credentials=Credentials(token), location=LOCATION)

def main():
    client = get_bq_client()
    sql = """
    SELECT
      `Channel Group` AS channel_group,
      MIN(`Period`) AS min_period_all_rows,
      MAX(`Period`) AS max_period_all_rows,
      MIN(IF(`Cost` <> 0, `Period`, NULL)) AS min_period_nonzero_cost,
      MAX(IF(`Cost` <> 0, `Period`, NULL)) AS max_period_nonzero_cost,
      COUNT(*) AS total_rows,
      COUNTIF(`Cost` <> 0) AS nonzero_rows,
      ROUND(SUM(`Cost`), 2) AS total_cost_gbp
    FROM `rh-ga4-491819.rh_warehouse.marketing_spend_by_channel_manual_upload`
    GROUP BY 1
    ORDER BY max_period_nonzero_cost DESC NULLS LAST, channel_group ASC
    """
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry = client.query(sql, job_config=dry_cfg)
    print(f"Dry run bytes: {dry.total_bytes_processed:,} bytes")
    rows = list(client.query(sql, job_config=bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})).result())
    print("\n=== marketing_spend_by_channel_manual_upload by Channel Group ===")
    print(f"{'channel_group':40s} | {'min_nonzero':12s} | {'max_nonzero':12s} | {'max_all_rows':12s} | {'nonzero_rows':12s} | {'total_cost_gbp':14s}")
    print("-" * 116)
    for r in rows:
        print(
            f"{str(r['channel_group']):40s} | {str(r['min_period_nonzero_cost']):12s} | "
            f"{str(r['max_period_nonzero_cost']):12s} | {str(r['max_period_all_rows']):12s} | "
            f"{int(r['nonzero_rows']):12d} | £{float(r['total_cost_gbp'] or 0):13.2f}"
        )

if __name__ == "__main__":
    main()

