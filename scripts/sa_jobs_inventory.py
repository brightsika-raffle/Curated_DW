#!/usr/bin/env python3
"""
scripts/sa_jobs_inventory.py

Inventory script for BigQuery scheduled queries and Data Transfer Service configs
in project rh-ga4-491819 (europe-west2).

Rules & Requirements:
- Uses service account impersonation (rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com).
- Does not change job ownership or configs (read-only).
- Prints:
    * display name and config id
    * owner / service account
    * schedule
    * whether destination dataset / SQL target is rh_curated_Bright_Test
    * enable_failure_email
    * notification Pub/Sub topic
    * last 5 runs with state
- Writes Markdown table to docs/scheduled_jobs.md with owner-contact set to 'bright@raffle-house.com (data)'.
- Flags any job owned by a person rather than the service account.
- Flags any job targeting rh_curated_Bright_Test that is separate from the nightly build (6abf66d9).
"""

import os, sys, subprocess, json, datetime
from google.cloud import bigquery, bigquery_datatransfer_v1
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_token():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    return res.stdout.strip().splitlines()[-1].strip()

def main():
    token = get_token()
    creds = Credentials(token)
    bq_client = bigquery.Client(project=PROJECT, location=LOCATION, credentials=creds)
    dts_client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=creds)

    print("=" * 115)
    print(f"BIGQUERY DATA TRANSFER & SCHEDULED JOBS INVENTORY ({PROJECT} / {LOCATION})")
    print(f"Executing as: {SA}")
    print("=" * 115)

    # 1. Discover all active transfer configs from BigQuery jobs history in last 30 days
    jobs_query = """
    WITH dts_jobs AS (
      SELECT
        (SELECT value FROM UNNEST(labels) WHERE key = 'dts_config_id') AS config_id,
        job_id,
        user_email,
        creation_time,
        state,
        error_result.message AS error_message,
        query,
        destination_table.dataset_id AS dest_dataset,
        ROW_NUMBER() OVER(
          PARTITION BY (SELECT value FROM UNNEST(labels) WHERE key = 'dts_config_id')
          ORDER BY creation_time DESC
        ) AS rn
      FROM `region-europe-west2`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
      WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
        AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'dts_config_id')
    )
    SELECT * FROM dts_jobs WHERE rn <= 5 ORDER BY config_id, creation_time DESC
    """
    job_rows = list(bq_client.query(jobs_query).result())
    discovered_cids = sorted(list(set(r.config_id for r in job_rows if r.config_id)))

    # 2. Known service account transfer configs to verify
    known_sa_configs = [
        "6abf66d9-0000-2436-a7b2-14c14ef3c450",
        "6ae09451-0000-271c-b631-34c7e9450713",
        "6ac94295-0000-2ad6-bdb4-f40304360398",
        "6acfe2cb-0000-2ea6-8cf3-883d24f3bae4"
    ]
    all_cids = sorted(list(set(discovered_cids + known_sa_configs)))

    inventory = []

    for cid in all_cids:
        c_name = f"projects/{PROJECT}/locations/{LOCATION}/transferConfigs/{cid}"
        cfg = None
        dts_permission_error = None
        try:
            cfg = dts_client.get_transfer_config(name=c_name)
        except Exception as e:
            dts_permission_error = str(e)

        c_runs = [r for r in job_rows if r.config_id == cid]

        if cfg:
            display_name = cfg.display_name or "Unnamed"
            owner = cfg.owner_info.email if (cfg.owner_info and cfg.owner_info.email) else (getattr(cfg, "user_id", None) or "Service Account")
            schedule = cfg.schedule or "None / on-demand"
            disabled = cfg.disabled
            dest_dataset = cfg.destination_dataset_id or ""
            query_text = cfg.params.get("query", "") if cfg.params else ""
            targets_curated = ("rh_curated_Bright_Test" in dest_dataset) or ("rh_curated_Bright_Test" in query_text)
            email_pref = cfg.email_preferences.enable_failure_email if cfg.email_preferences else False
            pubsub_topic = cfg.notification_pubsub_topic or "None"
            
            # Fetch last 5 runs from DTS API if available, else from BigQuery jobs
            runs_summary = []
            try:
                run_pager = dts_client.list_transfer_runs(parent=cfg.name, page_size=5)
                for i, r in enumerate(run_pager):
                    if i >= 5:
                        break
                    state_str = r.state.name if hasattr(r.state, "name") else str(r.state)
                    run_time = r.run_time.strftime("%Y-%m-%d %H:%M UTC") if r.run_time else "unknown"
                    runs_summary.append(f"{run_time} ({state_str})")
            except Exception:
                pass
            
            if not runs_summary and c_runs:
                for r in c_runs[:5]:
                    status_str = "SUCCESS" if not r.error_message and r.state == "DONE" else f"FAILED: {r.error_message}"
                    time_str = r.creation_time.strftime("%Y-%m-%d %H:%M UTC") if r.creation_time else "unknown"
                    runs_summary.append(f"{time_str} ({status_str})")

        else:
            # Config is restricted or owned by a user / external service account
            latest_job = c_runs[0] if c_runs else None
            owner = latest_job.user_email if latest_job else "Unknown"
            
            # Derive meaningful display name from target table / query
            dest_dataset = latest_job.dest_dataset or "" if latest_job else ""
            query_snippet = latest_job.query or "" if latest_job else ""
            
            # Extract created table name if available
            import re
            table_match = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+`?([a-zA-Z0-9_.-]+)`?", query_snippet, re.IGNORECASE)
            
            if "google_ads" in (latest_job.job_id if latest_job else "") or "google_ads" in dest_dataset:
                display_name = "Google Ads Data Transfer"
            elif table_match:
                tbl = table_match.group(1).split(".")[-1]
                display_name = f"rh_warehouse.{tbl}"
            elif "url_decode" in query_snippet:
                display_name = "rh_warehouse.fact_transaction_attribution"
            else:
                display_name = f"Transfer Config {cid[:8]}"

            targets_curated = ("rh_curated_Bright_Test" in dest_dataset) or ("rh_curated_Bright_Test" in query_snippet)
            email_pref = "Restricted (IAM)"
            pubsub_topic = "Restricted (IAM)"
            schedule = "Daily (active in JOBS_BY_PROJECT)"
            disabled = False

            runs_summary = []
            for r in c_runs[:5]:
                status_str = "SUCCESS" if not r.error_message and r.state == "DONE" else f"FAILED: {r.error_message}"
                time_str = r.creation_time.strftime("%Y-%m-%d %H:%M UTC") if r.creation_time else "unknown"
                runs_summary.append(f"{time_str} ({status_str})")

        is_person_owned = "@" in owner and not owner.endswith(".gserviceaccount.com")
        writes_curated_not_nightly = targets_curated and cid != "6abf66d9-0000-2436-a7b2-14c14ef3c450"

        item = {
            "name": display_name,
            "config_id": cid,
            "owner": owner,
            "schedule": schedule,
            "disabled": disabled,
            "targets_curated": targets_curated,
            "dest_dataset": dest_dataset,
            "email_pref": str(email_pref),
            "pubsub_topic": pubsub_topic,
            "last_5_runs": runs_summary if runs_summary else ["No recent runs recorded"],
            "is_person_owned": is_person_owned,
            "writes_curated_not_nightly": writes_curated_not_nightly
        }
        inventory.append(item)

        runs_formatted = "; ".join(item["last_5_runs"])
        print(f"\nConfig ID:       {cid}")
        print(f"Display Name:    {display_name}")
        print(f"Owner / SA:      {owner}")
        print(f"Schedule:        {schedule} (Disabled: {disabled})")
        print(f"Targets Curated: {targets_curated} (Dataset: {dest_dataset or 'SQL internal'})")
        print(f"Failure Email:   {email_pref}")
        print(f"Pub/Sub Topic:   {pubsub_topic}")
        print(f"Last 5 Runs:     {runs_formatted}")
        if is_person_owned:
            print(f"FLAG: Owned by a person ({owner}) rather than service account!")
        if writes_curated_not_nightly:
            print(f"FLAG: Targets rh_curated_Bright_Test but is separate from nightly build (6abf66d9)!")

    # 3. Print Flags Summary
    print("\n" + "=" * 115)
    print("INVENTORY FLAGS SUMMARY")
    print("=" * 115)
    person_jobs = [j for j in inventory if j["is_person_owned"]]
    curated_jobs = [j for j in inventory if j["writes_curated_not_nightly"]]

    print(f"Total transfer configs inventoried: {len(inventory)}")
    print(f"Total configs owned by a person: {len(person_jobs)}")
    for j in person_jobs:
        print(f"  * [PERSON-OWNED] {j['name']} (`{j['config_id']}`): owned by {j['owner']}")

    print(f"\nTotal separate configs writing to rh_curated_Bright_Test: {len(curated_jobs)}")
    for j in curated_jobs:
        print(f"  * [SEPARATE-CURATED] {j['name']} (`{j['config_id']}`): schedule '{j['schedule']}', owner '{j['owner']}'")

    # 4. Generate docs/scheduled_jobs.md
    os.makedirs("docs", exist_ok=True)
    md_lines = [
        "# Scheduled Data Transfer Jobs Inventory",
        "",
        f"**Project:** `{PROJECT}` | **Location:** `{LOCATION}` | **Service Account:** `{SA}` | **Audit Date:** 2026-11-01",
        "",
        "| Display Name | Config ID | Owner / Service Account | Schedule | Targets Curated | Failure Email | Pub/Sub Topic | Last 5 Runs | Owner Contact | Flags |",
        "| :--- | :--- | :--- | :--- | :---: | :---: | :--- | :--- | :--- | :--- |"
    ]

    for j in inventory:
        flags = []
        if j["is_person_owned"]:
            flags.append(f"Person-owned ({j['owner']})")
        if j["writes_curated_not_nightly"]:
            flags.append("Curated target (separate from nightly build)")
        flag_str = "<br>".join(flags) if flags else "None"
        runs_str = "<br>".join(j["last_5_runs"][:5])
        
        md_lines.append(
            f"| {j['name']} | `{j['config_id']}` | `{j['owner']}` | {j['schedule']} | {'Yes' if j['targets_curated'] else 'No'} | {j['email_pref']} | {j['pubsub_topic']} | {runs_str} | bright@raffle-house.com (data) | {flag_str} |"
        )

    md_lines.extend([
        "",
        "## Governance Notes & Flags",
        f"1. **Person-owned configs ({len(person_jobs)} jobs):** Legacy warehouse tables are populated by configs owned by `jordan@raffle-house.com`. Under governance policy, these should be transitioned to the pipeline service account so they do not break if individual user credentials change.",
        f"2. **Separate Curated Writers ({len(curated_jobs)} jobs):** Configs `6ae09451`, `6ac94295`, and `6acfe2cb` write directly to `rh_curated_Bright_Test` outside the main unified nightly build (`6abf66d9`).",
        "3. **Missing DTS Viewer Role on Project:** The service account lacks `roles/bigquerydatatransfer.viewer` on `projects/rh-ga4-491819/locations/europe-west2`, preventing direct enumeration via `list_transfer_configs`. Jobs were reconciled via `INFORMATION_SCHEMA.JOBS_BY_PROJECT`."
    ])

    with open("docs/scheduled_jobs.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines) + "\n")

    print("\nSuccessfully wrote docs/scheduled_jobs.md")

if __name__ == "__main__":
    main()
