import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from google.cloud import bigquery
from google.cloud import bigquery_datatransfer_v1
from google.protobuf import field_mask_pb2, timestamp_pb2
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
LTV_CONFIG_NAME = "projects/613976204637/locations/europe-west2/transferConfigs/6ae09451-0000-271c-b631-34c7e9450713"
REVERT_SQL_PATH = "backups/scheduled_queries/2026-10-27/customer_ltv_horizons_20261027_194309Z.sql"

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

def measure_ltv(bq_client, label):
    sql = f"""
    WITH med AS (
      SELECT
        PERCENTILE_CONT(CAST(ltv_12m AS FLOAT64), 0.5) OVER () AS med_ltv_12m,
        PERCENTILE_CONT(CAST(sub_efv_12m AS FLOAT64), 0.5) OVER () AS med_sub_efv_12m
      FROM `{PROJECT}.rh_curated_Bright_Test.customer_ltv_horizons`
      WHERE ltv_segment = 'Active subscriber'
      LIMIT 1
    ),
    cust_spend AS (
      SELECT SUM(revenue_gbp) AS sum_total_spend_gbp
      FROM `{PROJECT}.rh_curated_Bright_Test.fact_transactions`
    )
    SELECT
      COUNT(*) AS total_customers,
      COUNTIF(ltv_segment = 'Active subscriber') AS active_subscribers,
      ROUND(SUM(IF(ltv_segment = 'Active subscriber', ltv_12m, 0)), 2) AS active_sub_sum_ltv_12m,
      ROUND(AVG(IF(ltv_segment = 'Active subscriber', ltv_12m, NULL)), 2) AS active_sub_avg_ltv_12m,
      ROUND(ANY_VALUE(m.med_ltv_12m), 2) AS active_sub_med_ltv_12m,
      ROUND(SUM(IF(ltv_segment = 'Active subscriber', sub_efv_12m, 0)), 2) AS active_sub_sum_sub_efv_12m,
      ROUND(AVG(IF(ltv_segment = 'Active subscriber', sub_efv_12m, NULL)), 2) AS active_sub_avg_sub_efv_12m,
      ROUND(ANY_VALUE(m.med_sub_efv_12m), 2) AS active_sub_med_sub_efv_12m,
      ROUND(SUM(realised_value), 2) AS sum_realised_value,
      ROUND(ANY_VALUE(cs.sum_total_spend_gbp), 2) AS sum_total_spend_gbp,
      MAX(built_at) AS built_at
    FROM `{PROJECT}.rh_curated_Bright_Test.customer_ltv_horizons`
    CROSS JOIN med m
    CROSS JOIN cust_spend cs
    """
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry = bq_client.query(sql, job_config=dry_cfg)
    if dry.total_bytes_processed > 1024**3:
        raise RuntimeError(f"Exceeds 1 GB: {dry.total_bytes_processed}")
    print(f"[Dry run ({label}): {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024**2):.2f} MB)]")
    row = list(bq_client.query(sql, job_config=bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})).result())[0]
    res = {
        "label": label,
        "total_customers": int(row["total_customers"]),
        "active_subscribers": int(row["active_subscribers"]),
        "active_sub_sum_ltv_12m": float(row["active_sub_sum_ltv_12m"]),
        "active_sub_avg_ltv_12m": float(row["active_sub_avg_ltv_12m"]),
        "active_sub_med_ltv_12m": float(row["active_sub_med_ltv_12m"]),
        "active_sub_sum_sub_efv_12m": float(row["active_sub_sum_sub_efv_12m"]),
        "active_sub_avg_sub_efv_12m": float(row["active_sub_avg_sub_efv_12m"]),
        "active_sub_med_sub_efv_12m": float(row["active_sub_med_sub_efv_12m"]),
        "sum_realised_value": float(row["sum_realised_value"]),
        "sum_total_spend_gbp": float(row["sum_total_spend_gbp"]),
        "built_at": str(row["built_at"]),
    }
    print(f"--- {label} ---")
    print(f"  built_at                     : {res['built_at']}")
    print(f"  total_customers              : {res['total_customers']:,}")
    print(f"  active_subscribers           : {res['active_subscribers']:,}")
    print(f"  active_sub SUM(ltv_12m)      : £{res['active_sub_sum_ltv_12m']:,.2f}")
    print(f"  active_sub AVG(ltv_12m)      : £{res['active_sub_avg_ltv_12m']:,.2f}")
    print(f"  active_sub MEDIAN(ltv_12m)   : £{res['active_sub_med_ltv_12m']:,.2f}")
    print(f"  active_sub SUM(sub_efv_12m)  : £{res['active_sub_sum_sub_efv_12m']:,.2f}")
    print(f"  active_sub AVG(sub_efv_12m)  : £{res['active_sub_avg_sub_efv_12m']:,.2f}")
    print(f"  active_sub MED(sub_efv_12m)  : £{res['active_sub_med_sub_efv_12m']:,.2f}")
    print(f"  SUM(realised_value)          : £{res['sum_realised_value']:,.2f}")
    print(f"  SUM(total_spend_gbp)         : £{res['sum_total_spend_gbp']:,.2f}")
    return res

def main():
    print("================================================================================")
    print("STEP 0: REVERT customer_ltv_horizons TO PRE-STEP-4 VERSION")
    print("================================================================================")

    sa_token = get_sa_token()
    bq_client = bigquery.Client(project=PROJECT, credentials=Credentials(sa_token), location=LOCATION)

    before = measure_ltv(bq_client, "BEFORE REVERT (with Step 4 dynamic 39.7% trial CVR)")

    with open(REVERT_SQL_PATH, "r", encoding="utf-8") as f:
        revert_sql = f.read()

    # Dry run the revert SQL
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry = bq_client.query(revert_sql, job_config=dry_cfg)
    print(f"Dry-run [revert SQL {REVERT_SQL_PATH}]: {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024**2):.2f} MB)")
    if dry.total_bytes_processed > 1024**3:
        raise RuntimeError(f"Exceeds 1 GB limit: {dry.total_bytes_processed}")

    # Restore Claude outputs/ltv_horizons.sql
    with open(os.path.join("Claude outputs", "ltv_horizons.sql"), "w", encoding="utf-8") as f:
        f.write(revert_sql)

    # Backup current transfer config and update to revert_sql
    user_token = get_user_token()
    dts_client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=Credentials(user_token))
    config = dts_client.get_transfer_config(name=LTV_CONFIG_NAME)

    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_dir = f"backups/scheduled_queries/{date_dir}"
    os.makedirs(backup_dir, exist_ok=True)
    backup_sql_path = f"{backup_dir}/customer_ltv_horizons_pre_revert_{ts_str}.sql"
    backup_cfg_path = f"{backup_dir}/customer_ltv_horizons_pre_revert_{ts_str}.json"

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
    params["query"] = revert_sql
    config.params = params
    update_mask = field_mask_pb2.FieldMask(paths=["params"])
    updated = dts_client.update_transfer_config(transfer_config=config, update_mask=update_mask)
    print(f"Updated transfer config to pre-Step-4 SQL: {updated.display_name} ({updated.name})")

    # Trigger manual run and wait for SUCCEEDED
    now = datetime.now(timezone.utc)
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(now)
    req = bigquery_datatransfer_v1.StartManualTransferRunsRequest(
        parent=LTV_CONFIG_NAME,
        requested_run_time=ts
    )
    res = dts_client.start_manual_transfer_runs(request=req)
    run = res.runs[0] if res.runs else list(dts_client.list_transfer_runs(parent=LTV_CONFIG_NAME, page_size=5))[0]
    run_name = run.name
    print(f"Triggered manual run: {run_name}")

    start_time = time.time()
    while True:
        r = dts_client.get_transfer_run(name=run_name)
        state_str = r.state.name
        if state_str in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(5)

    elapsed = int(time.time() - start_time)
    print(f"Manual run finished in {elapsed}s with state: {state_str}")
    if state_str != "SUCCEEDED":
        raise RuntimeError(f"Manual run failed: {state_str} ({r.error_status})")

    sa_token = get_sa_token()
    bq_client = bigquery.Client(project=PROJECT, credentials=Credentials(sa_token), location=LOCATION)
    after = measure_ltv(bq_client, "AFTER REVERT (restored to pre-Step-4 version)")

    diff_sum = after["active_sub_sum_ltv_12m"] - before["active_sub_sum_ltv_12m"]
    diff_avg = after["active_sub_avg_ltv_12m"] - before["active_sub_avg_ltv_12m"]
    diff_med = after["active_sub_med_ltv_12m"] - before["active_sub_med_ltv_12m"]
    diff_cust = after["active_subscribers"] - before["active_subscribers"]

    print("--- REVERT DIFF (AFTER - BEFORE) ---")
    print(f"  active_subscribers diff      : {diff_cust:+,}")
    print(f"  active_sub SUM(ltv_12m) diff : £{diff_sum:+,.2f}")
    print(f"  active_sub AVG(ltv_12m) diff : £{diff_avg:+,.2f}")
    print(f"  active_sub MED(ltv_12m) diff : £{diff_med:+,.2f}")
    print(f"  SUM(total_spend_gbp) diff    : £{after['sum_total_spend_gbp'] - before['sum_total_spend_gbp']:+,.2f}")

    with open(f"{backup_dir}/step0_ltv_baseline.json", "w", encoding="utf-8") as f:
        json.dump({"before_revert": before, "after_revert": after, "backup_sql": backup_sql_path, "backup_cfg": backup_cfg_path, "run_name": run_name, "run_state": state_str}, f, indent=2)

if __name__ == "__main__":
    main()
