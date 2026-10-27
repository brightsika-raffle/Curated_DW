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
LTV_CONFIG_NAME = "projects/613976204637/locations/europe-west2/transferConfigs/6ae09451-0000-271c-b631-34c7e9450713"

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

def dry_run_stmt(bq_client, label, stmt_sql):
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry_job = bq_client.query(stmt_sql, job_config=job_config)
    b = dry_job.total_bytes_processed
    print(f"Dry-run [{label}]: {b:,} bytes ({b / (1024**2):.2f} MB / {b / (1024**3):.4f} GB)")
    if b > 1024**3:
        raise RuntimeError(f"Dry run [{label}] exceeds 1 GB limit: {b}")
    return b

def backup_and_update_config(dts_client, config_name, file_prefix, new_sql):
    config = dts_client.get_transfer_config(name=config_name)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_dir = f"backups/scheduled_queries/{date_dir}"
    os.makedirs(backup_dir, exist_ok=True)
    backup_sql_path = f"{backup_dir}/{file_prefix}_{ts_str}.sql"
    backup_cfg_path = f"{backup_dir}/{file_prefix}_{ts_str}.json"

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

    print(f"Backup SQL path ({file_prefix}): {backup_sql_path}")
    print(f"Backup config path ({file_prefix}): {backup_cfg_path}")

    params = dict(config.params)
    params["query"] = new_sql
    config.params = params

    update_mask = field_mask_pb2.FieldMask(paths=["params"])
    updated = dts_client.update_transfer_config(transfer_config=config, update_mask=update_mask)
    print(f"Updated transfer config: {updated.display_name} ({updated.name})")
    return backup_sql_path, backup_cfg_path

def trigger_and_wait(dts_client, config_name, label):
    now = datetime.now(timezone.utc)
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(now)
    req = bigquery_datatransfer_v1.StartManualTransferRunsRequest(
        parent=config_name,
        requested_run_time=ts
    )
    res = dts_client.start_manual_transfer_runs(request=req)
    run = res.runs[0] if res.runs else list(dts_client.list_transfer_runs(parent=config_name, page_size=5))[0]
    run_name = run.name
    print(f"Triggered manual run [{label}]: {run_name}")

    start_time = time.time()
    while True:
        r = dts_client.get_transfer_run(name=run_name)
        state_str = r.state.name
        if state_str in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(10)

    elapsed = int(time.time() - start_time)
    print(f"Manual run [{label}] finished in {elapsed}s with state: {state_str}")
    if state_str != "SUCCEEDED":
        print(f"Error status: {r.error_status}")
        raise RuntimeError(f"Manual run [{label}] failed: {state_str}")
    return run_name

def main():
    with open("nightly_build_and_test.sql", "r", encoding="utf-8") as f:
        sql = f.read()

    with open("nightly_build_and_test_live_scheduled.sql", "w", encoding="utf-8") as f:
        f.write(sql)

    with open(os.path.join("Claude outputs", "ltv_horizons.sql"), "r", encoding="utf-8") as f:
        ltv_sql = f.read()

    sa_token = get_sa_token()
    bq_client = bigquery.Client(project=PROJECT, credentials=Credentials(sa_token), location=LOCATION)

    # Dry run individual statements and full script (Rule 5)
    dry_run_stmt(bq_client, "nightly_build_and_test.sql (script syntax check)", sql)
    dry_run_stmt(bq_client, "customer_ltv_horizons.sql", ltv_sql)

    # Extract Section 16 & Section 17 statements to print their individual dry-run bytes
    import re
    s16_start = sql.index("-- SECTION 16")
    s17_start = sql.index("-- SECTION 17")
    s_tests = sql.index("-- DAILY TESTS")
    for idx, stmt in enumerate([s.strip() for s in re.split(r";\s*\r?\n", sql[s16_start:s17_start]) if "SELECT" in s.upper()], 1):
        dry_run_stmt(bq_client, f"Section 16 stmt {idx}", stmt)
    for idx, stmt in enumerate([s.strip() for s in re.split(r";\s*\r?\n", sql[s17_start:s_tests]) if "SELECT" in s.upper()], 1):
        dry_run_stmt(bq_client, f"Section 17 stmt {idx}", stmt)
    dry_run_stmt(bq_client, "SELECT * FROM vw_trial_status", "SELECT * FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_trial_status`")

    user_token = get_user_token()
    dts_client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=Credentials(user_token))

    # 1. Backup and update customer_ltv_horizons nightly
    backup_and_update_config(dts_client, LTV_CONFIG_NAME, "customer_ltv_horizons", ltv_sql)
    trigger_and_wait(dts_client, LTV_CONFIG_NAME, "customer_ltv_horizons nightly")

    # 2. Backup and update rh_curated nightly build + tests
    user_token = get_user_token()
    dts_client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=Credentials(user_token))
    backup_and_update_config(dts_client, TRANSFER_CONFIG_NAME, "nightly_build_and_test", sql)
    trigger_and_wait(dts_client, TRANSFER_CONFIG_NAME, "rh_curated nightly build + tests")

    # 3. Query DQ gate results and INFORMATION_SCHEMA job bytes
    sa_token = get_sa_token()
    bq_client = bigquery.Client(project=PROJECT, credentials=Credentials(sa_token), location=LOCATION)

    q_jobs = """
    SELECT
      job_id,
      creation_time,
      total_bytes_processed,
      total_bytes_billed
    FROM `rh-ga4-491819`.`region-europe-west2`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 MINUTE)
      AND statement_type = 'SCRIPT'
      AND state = 'DONE'
    ORDER BY creation_time DESC
    LIMIT 2
    """
    try:
        for j in bq_client.query(q_jobs, job_config=bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})).result():
            print(f"Script Job {j['job_id']} ({j['creation_time']}): processed={j['total_bytes_processed']:,} bytes ({j['total_bytes_processed']/(1024**2):.2f} MB), billed={j['total_bytes_billed']:,} bytes ({j['total_bytes_billed']/(1024**2):.2f} MB)")
    except Exception as e:
        print(f"Note: INFORMATION_SCHEMA.JOBS_BY_PROJECT lookup: {e}")

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
