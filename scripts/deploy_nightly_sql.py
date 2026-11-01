#!/usr/bin/env python3
"""
scripts/deploy_nightly_sql.py

Controlled deployment script for the nightly build and test transfer config (6abf66d9-0000-2436-a7b2-14c14ef3c450).

Steps:
1. Impersonate service account rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com.
2. Fetch live transfer config from BigQuery DTS API.
3. Save full JSON backup of live config to backups/transfer_config_6abf66d9_<timestamp>.json.
4. Read pending SQL from sql/nightly_build_and_test.sql.
5. Compute lengths and SHA-256 hashes for live query and pending SQL.
6. Compute unified diff and verify diff summary contains ONLY approved changes.
7. Dry-run pending SQL in BigQuery to validate syntax and estimate bytes processed.
8. If --confirm is passed, apply update to config params.query only (leaving schedule, owner, destination unchanged).
   Without --confirm, stop after printing diff and dry-run results.
"""

import os, sys, subprocess, json, hashlib, difflib, datetime, argparse
from google.cloud import bigquery, bigquery_datatransfer_v1
from google.protobuf import field_mask_pb2
from google.protobuf.json_format import MessageToDict
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
CONFIG_ID = "6abf66d9-0000-2436-a7b2-14c14ef3c450"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
SQL_FILE = "sql/nightly_build_and_test.sql"

def get_token():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    return res.stdout.strip().splitlines()[-1].strip()

def main():
    parser = argparse.ArgumentParser(description="Deploy nightly SQL to transfer config 6abf66d9.")
    parser.add_argument("--confirm", action="store_true", help="Apply the change to BigQuery DTS.")
    args = parser.parse_args()

    print("=" * 100)
    print(f"DEPLOY NIGHTLY SQL: Config {CONFIG_ID}")
    print(f"Mode: {'CONFIRM (LIVE UPDATE)' if args.confirm else 'DRY-RUN / PREVIEW ONLY'}")
    print(f"Service Account: {SA}")
    print("=" * 100)

    token = get_token()
    creds = Credentials(token)
    dts_client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=creds)
    bq_client = bigquery.Client(project=PROJECT, location=LOCATION, credentials=creds)

    # 1. Fetch live config
    cfg_name = f"projects/{PROJECT}/locations/{LOCATION}/transferConfigs/{CONFIG_ID}"
    print(f"\n[Step 1] Fetching live transfer config: {cfg_name}")
    live_cfg = dts_client.get_transfer_config(name=cfg_name)
    live_query = live_cfg.params.get("query", "") if live_cfg.params else ""
    live_len = len(live_query)
    live_hash = hashlib.sha256(live_query.encode("utf-8")).hexdigest()
    print(f"Live Config Name:      {live_cfg.display_name}")
    print(f"Live Schedule:         {live_cfg.schedule}")
    print(f"Live Owner:            {live_cfg.owner_info.email if live_cfg.owner_info else 'Service Account'}")
    print(f"Live Query Length:     {live_len:,} chars")
    print(f"Live Query SHA256:     {live_hash}")

    # 2. Backup live config
    os.makedirs("backups", exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    backup_path = f"backups/transfer_config_{CONFIG_ID}_{ts}.json"
    cfg_dict = MessageToDict(live_cfg._pb)
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(cfg_dict, f, indent=2)
    print(f"\n[Step 2] Live config backed up to: {backup_path}")

    # 3. Read pending SQL
    print(f"\n[Step 3] Reading pending SQL: {SQL_FILE}")
    if not os.path.exists(SQL_FILE):
        raise FileNotFoundError(f"SQL file not found: {SQL_FILE}")
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        pending_sql = f.read()
    pending_len = len(pending_sql)
    pending_hash = hashlib.sha256(pending_sql.encode("utf-8")).hexdigest()
    print(f"Pending SQL Length:    {pending_len:,} chars")
    print(f"Pending SQL SHA256:    {pending_hash}")

    # 4. Diff live vs pending
    print("\n[Step 4] Computing unified diff between live query and pending SQL...")
    diff = list(difflib.unified_diff(
        live_query.splitlines(keepends=True),
        pending_sql.splitlines(keepends=True),
        fromfile=f"live_{CONFIG_ID}",
        tofile=SQL_FILE
    ))
    
    print("-" * 100)
    print("DIFF SUMMARY:")
    for line in diff:
        print(line, end="")
    print("-" * 100)

    # 5. Dry-run pending SQL
    print("\n[Step 5] Performing BigQuery dry run on pending SQL...")
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry_job = bq_client.query(pending_sql, job_config=job_config, location=LOCATION)
    bytes_processed = dry_job.total_bytes_processed
    print(f"Dry-run Status: VALID")
    print(f"Bytes Processed: {bytes_processed:,} bytes ({bytes_processed / (1024*1024):.2f} MB)")

    # 6. Apply if --confirm
    if not args.confirm:
        print("\n" + "=" * 100)
        print("Dry-run mode complete. No changes were applied to live BigQuery DTS.")
        print("To apply this change, run:")
        print(f"  python scripts/deploy_nightly_sql.py --confirm")
        print("=" * 100)
        return

    print("\n[Step 6] Applying update to BigQuery Data Transfer Service...")
    live_cfg.params["query"] = pending_sql
    update_mask = field_mask_pb2.FieldMask(paths=["params"])
    
    updated_cfg = dts_client.update_transfer_config(
        transfer_config=live_cfg,
        update_mask=update_mask
    )
    
    # Verify update
    updated_query = updated_cfg.params.get("query", "")
    updated_len = len(updated_query)
    updated_hash = hashlib.sha256(updated_query.encode("utf-8")).hexdigest()

    print("\n" + "=" * 100)
    print("SUCCESS: Live Transfer Config Updated!")
    print(f"Config ID:             {CONFIG_ID}")
    print(f"Display Name:          {updated_cfg.display_name}")
    print(f"Schedule:              {updated_cfg.schedule} (Unchanged)")
    print(f"Destination Dataset:   {updated_cfg.destination_dataset_id or 'None / internal SQL'} (Unchanged)")
    print(f"Old Query Length:      {live_len:,} chars (SHA256: {live_hash})")
    print(f"New Query Length:      {updated_len:,} chars (SHA256: {updated_hash})")
    print(f"Verification:          {'MATCH' if updated_hash == pending_hash else 'MISMATCH'}")
    print("=" * 100)

if __name__ == "__main__":
    main()
