import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from google.cloud import bigquery_datatransfer_v1
from google.cloud import bigquery
from google.protobuf import field_mask_pb2, timestamp_pb2
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
TRANSFER_CONFIG_NAME = "projects/613976204637/locations/europe-west2/transferConfigs/6abf66d9-0000-2436-a7b2-14c14ef3c450"

def get_user_token():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token"],
        capture_output=True, text=True, check=True, env=env
    )
    return res.stdout.strip().splitlines()[-1].strip()

def get_sa_token():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    return res.stdout.strip().splitlines()[-1].strip()

def main():
    with open("nightly_build_and_test.sql", "r", encoding="utf-8") as f:
        sql = f.read()

    # Also keep nightly_build_and_test_live_scheduled.sql in sync
    with open("nightly_build_and_test_live_scheduled.sql", "w", encoding="utf-8") as f:
        f.write(sql)

    sa_token = get_sa_token()
    bq_client = bigquery.Client(project=PROJECT, credentials=Credentials(sa_token), location=LOCATION)

    # Dry run the full nightly script first (Rule 5)
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry_job = bq_client.query(sql, job_config=job_config)
    total_bytes = dry_job.total_bytes_processed
    print(f"Nightly build dry-run bytes: {total_bytes:,} bytes ({total_bytes / (1024**2):.2f} MB / {total_bytes / (1024**3):.4f} GB)")
    if total_bytes > 1024**3:
        raise RuntimeError(f"Dry run exceeds 1 GB limit: {total_bytes}")

    user_token = get_user_token()
    dts_client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=Credentials(user_token))

    config = dts_client.get_transfer_config(name=TRANSFER_CONFIG_NAME)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_dir = f"backups/scheduled_queries/{date_dir}"
    os.makedirs(backup_dir, exist_ok=True)
    backup_sql_path = f"{backup_dir}/nightly_build_and_test_{ts_str}.sql"
    backup_cfg_path = f"{backup_dir}/nightly_build_and_test_{ts_str}.json"

    with open(backup_sql_path, "w", encoding="utf-8") as bf:
        bf.write(config.params.get("query", ""))
    with open(backup_cfg_path, "w", encoding="utf-8") as cf:
        json.dump({
            "name": config.name,
            "display_name": config.display_name,
            "schedule": config.schedule,
            "data_source_id": config.data_source_id,
            "backed_up_at": ts_str,
        }, cf, indent=2)

    print(f"Backup SQL path: {backup_sql_path}")
    print(f"Backup config path: {backup_cfg_path}")

    params = dict(config.params)
    params["query"] = sql
    config.params = params

    update_mask = field_mask_pb2.FieldMask(paths=["params"])
    updated = dts_client.update_transfer_config(transfer_config=config, update_mask=update_mask)
    print(f"Updated transfer config: {updated.display_name} ({updated.name})")

    now = datetime.now(timezone.utc)
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(now)
    req = bigquery_datatransfer_v1.StartManualTransferRunsRequest(
        parent=TRANSFER_CONFIG_NAME,
        requested_run_time=ts
    )
    res = dts_client.start_manual_transfer_runs(request=req)
    run = res.runs[0] if res.runs else list(dts_client.list_transfer_runs(parent=TRANSFER_CONFIG_NAME, page_size=5))[0]
    run_name = run.name
    print(f"Triggered manual run: {run_name}")

    start_time = time.time()
    while True:
        r = dts_client.get_transfer_run(name=run_name)
        state_str = r.state.name
        if state_str in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(10)

    elapsed = int(time.time() - start_time)
    print(f"Manual nightly run finished in {elapsed}s with state: {state_str}")
    if state_str != "SUCCEEDED":
        print(f"Error status: {r.error_status}")
        raise RuntimeError(f"Manual nightly run failed: {state_str}")

    # Refresh token in case run took >1 min and query DQ gate results
    sa_token = get_sa_token()
    bq_client = bigquery.Client(project=PROJECT, credentials=Credentials(sa_token), location=LOCATION)
    q_tests = """
    SELECT
      run_ts,
      run_date,
      test,
      severity,
      result,
      source_value,
      curated_value,
      detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE run_ts = (SELECT MAX(run_ts) FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`)
    ORDER BY severity DESC, result DESC, test ASC
    """
    rows = list(bq_client.query(q_tests, job_config=bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})).result())
    print(f"DQ Gate Results for run_ts={rows[0]['run_ts']}:")
    hard_pass = sum(1 for row in rows if row["severity"] == "HARD" and row["result"] == "PASS")
    hard_fail = sum(1 for row in rows if row["severity"] == "HARD" and row["result"] != "PASS")
    warn_cnt = sum(1 for row in rows if row["severity"] == "WARN" and row["result"] == "WARN")
    warn_pass = sum(1 for row in rows if row["severity"] == "WARN" and row["result"] == "PASS")
    for row in rows:
        if row["severity"] == "HARD" or "paid_media" in row["test"] or "trial" in row["test"]:
            print(f"  [{row['severity']}] {row['test']}: {row['result']} (source={row['source_value']}, curated={row['curated_value']}) | {row['detail']}")
    print(f"DQ Summary: HARD PASS={hard_pass}, HARD FAIL={hard_fail}, WARN PASS={warn_pass}, WARN={warn_cnt}")

if __name__ == "__main__":
    main()

