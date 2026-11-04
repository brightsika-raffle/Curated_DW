import os, sys, json, time, datetime, subprocess
import urllib.request, urllib.parse, urllib.error
from google.cloud import bigquery, storage
from google.oauth2.credentials import Credentials

sys.stdout.reconfigure(encoding='utf-8')

# Constants
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
GCP_PROJECT = "rh-ga4-491819"
SECRET_ID = "meta-access-token"
AD_ACCOUNT_ID = "act_596135250732901"
GRAPH_API_VERSION = "v26.0"
GCS_BUCKET = "rh-ga4-491819-raw"
SEGMENT_BQ_TABLE = "rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily"

def get_gcp_credentials():
    token = subprocess.check_output(
        f"gcloud.cmd auth print-access-token --impersonate-service-account={SA}",
        shell=True, text=True
    ).strip()
    return Credentials(token)

def get_secret_token():
    from google.cloud import secretmanager
    creds = get_gcp_credentials()
    client = secretmanager.SecretManagerServiceClient(credentials=creds)
    name = f"projects/{GCP_PROJECT}/secrets/{SECRET_ID}/versions/latest"
    resp = client.access_secret_version(request={"name": name})
    return resp.payload.data.decode("utf-8").strip()

def get_bq_client():
    creds = get_gcp_credentials()
    default_config = bigquery.QueryJobConfig(labels={"datacloud": "antigravity", "purpose": "meta_feed"})
    return bigquery.Client(project=GCP_PROJECT, location="europe-west2", credentials=creds, default_query_job_config=default_config)

def extract_metrics(row: dict):
    """Extract purchases, purchase value, link clicks from actions/action_values matching meta_to_bq.py."""
    actions = row.get("actions", [])
    action_values = row.get("action_values", [])
    
    purchases = 0.0
    purchase_val = 0.0
    action_type_used = None
    link_clicks = 0
    
    if actions:
        act_dict = {a.get("action_type"): float(a.get("value", 0.0)) for a in actions if "action_type" in a}
        if "purchase" in act_dict:
            purchases = act_dict["purchase"]
            action_type_used = "purchase"
        elif "offsite_conversion.fb_pixel_purchase" in act_dict:
            purchases = act_dict["offsite_conversion.fb_pixel_purchase"]
            action_type_used = "offsite_conversion.fb_pixel_purchase"
        elif "omni_purchase" in act_dict:
            purchases = act_dict["omni_purchase"]
            action_type_used = "omni_purchase"
        
        if "link_click" in act_dict:
            link_clicks = int(float(act_dict["link_click"]))
            
    if action_values:
        val_dict = {v.get("action_type"): float(v.get("value", 0.0)) for v in action_values if "action_type" in v}
        if "purchase" in val_dict:
            purchase_val = val_dict["purchase"]
            if not action_type_used:
                action_type_used = "purchase"
        elif "offsite_conversion.fb_pixel_purchase" in val_dict:
            purchase_val = val_dict["offsite_conversion.fb_pixel_purchase"]
            if not action_type_used:
                action_type_used = "offsite_conversion.fb_pixel_purchase"
        elif "omni_purchase" in val_dict:
            purchase_val = val_dict["omni_purchase"]
            if not action_type_used:
                action_type_used = "omni_purchase"
            
    return purchases, purchase_val, link_clicks, action_type_used

def fetch_meta_segment_insights(token: str, start_date: str, end_date: str):
    """
    Fetch adset-level daily insights with user_segment_key breakdown.
    Handles pagination and rate limits with exponential backoff.
    Returns (records, api_call_count, throttling_events).
    """
    fields = [
        "date_start",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "spend",
        "impressions",
        "actions",
        "action_values"
    ]
    base_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": token,
        "level": "adset",
        "time_increment": "1",
        "time_range": json.dumps({"since": start_date, "until": end_date}),
        "fields": ",".join(fields),
        "breakdowns": "user_segment_key",
        "limit": "500"
    }
    
    all_data = []
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    api_calls = 0
    throttling_events = []
    
    while url:
        # Request with retry
        for attempt in range(5):
            req = urllib.request.Request(url, headers={"User-Agent": "RaffleHouse-MetaLoader/1.0"})
            try:
                api_calls += 1
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                err_code = None
                err_msg = ""
                try:
                    err_data = json.loads(body).get("error", {})
                    err_code = err_data.get("code")
                    err_msg = err_data.get("message", "")
                except Exception:
                    pass
                if err_code in (17, 613) or "rate limit" in err_msg.lower() or "user request limit" in err_msg.lower():
                    wait_time = 30 * (2 ** attempt)
                    msg = f"Rate limit code={err_code}. Waiting {wait_time}s (attempt {attempt+1}/5)"
                    throttling_events.append(msg)
                    print(msg)
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(f"Meta Graph API HTTP {e.code}: {err_msg}")
        else:
            raise RuntimeError("Exceeded maximum retries for Meta Graph API.")
            
        records = data.get("data", [])
        all_data.extend(records)
        url = data.get("paging", {}).get("next")
        
    return all_data, api_calls, throttling_events

def transform_segment_records(raw_records: list):
    """
    Transform and filter raw segment records.
    Rules:
    - Drop rows where spend, impressions and purchases are all zero.
    - If user_segment_key is null/None/empty:
        - If spend > 0: keep and label as 'null'
        - If spend == 0: drop row
    """
    transformed = []
    dropped_per_day = {}
    null_segment_adsets_with_spend = []
    loaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    for r in raw_records:
        spend_date = r.get("date_start")
        spend = round(float(r.get("spend", 0.0)), 2)
        impressions = int(r.get("impressions", 0))
        purchases, purchase_val, link_clicks, _ = extract_metrics(r)
        purchases = round(purchases, 2)
        purchase_val = round(purchase_val, 2)
        
        seg = r.get("user_segment_key")
        
        # Check null segment
        if seg is None or str(seg).strip().lower() in ("", "none"):
            if spend > 0:
                seg = "null"
                null_segment_adsets_with_spend.append({
                    "spend_date": spend_date,
                    "campaign_id": str(r.get("campaign_id", "")),
                    "campaign_name": str(r.get("campaign_name", "")),
                    "adset_id": str(r.get("adset_id", "")),
                    "adset_name": str(r.get("adset_name", "")),
                    "spend_gbp": spend
                })
            else:
                dropped_per_day[spend_date] = dropped_per_day.get(spend_date, 0) + 1
                continue
                
        # Drop rows where spend, impressions and purchases are all zero
        if spend == 0.0 and impressions == 0 and purchases == 0.0:
            dropped_per_day[spend_date] = dropped_per_day.get(spend_date, 0) + 1
            continue
            
        transformed.append({
            "spend_date": spend_date,
            "campaign_id": str(r.get("campaign_id", "")),
            "campaign_name": str(r.get("campaign_name", "")),
            "adset_id": str(r.get("adset_id", "")),
            "adset_name": str(r.get("adset_name", "")),
            "user_segment_key": seg,
            "spend_gbp": spend,
            "impressions": impressions,
            "link_clicks": link_clicks,
            "purchases": purchases,
            "purchase_value_gbp": purchase_val,
            "loaded_at": loaded_at
        })
        
    return transformed, dropped_per_day, null_segment_adsets_with_spend

def archive_raw_to_gcs(records: list, start_date: str, end_date: str):
    """Archive raw records to GCS bucket partitioned by date."""
    creds = get_gcp_credentials()
    storage_client = storage.Client(project=GCP_PROJECT, credentials=creds)
    bucket = storage_client.bucket(GCS_BUCKET)
    
    grouped = {}
    for r in records:
        d = r.get("date_start", start_date)
        grouped.setdefault(d, []).append(r)
        
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    for d, items in grouped.items():
        blob_path = f"meta/insights_segments/dt={d}/segments_{ts}.json"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(json.dumps(items, indent=2), content_type="application/json")
    print(f"Archived {len(records)} raw segment records across {len(grouped)} dates to gs://{GCS_BUCKET}/meta/insights_segments/")

def load_segments_to_bq(client, rows: list, start_date: str, end_date: str):
    """Idempotently replace rows for date range in stg_meta_segment_daily."""
    del_query = f"""
    DELETE FROM `{SEGMENT_BQ_TABLE}`
    WHERE spend_date BETWEEN '{start_date}' AND '{end_date}'
    """
    print(f"Executing idempotent cleanup: DELETE FROM `{SEGMENT_BQ_TABLE}` BETWEEN '{start_date}' AND '{end_date}'...")
    del_job = client.query(del_query)
    del_job.result()
    
    if rows:
        print(f"Loading {len(rows)} rows into `{SEGMENT_BQ_TABLE}` via batch LoadJob...")
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            labels={"datacloud": "antigravity", "purpose": "meta_feed"}
        )
        load_job = client.load_table_from_json(rows, SEGMENT_BQ_TABLE, job_config=job_config)
        load_job.result()
        print(f"BigQuery load complete: {len(rows)} rows inserted.")
    else:
        print("No rows to load.")

def run_backfill(start_date: str = "2026-09-01", end_date: str = "2026-11-03"):
    print(f"=== BACKFILL META AUDIENCE SEGMENTS ({start_date} to {end_date}) ===")
    token = get_secret_token()
    print("Meta access token retrieved from Secret Manager.")
    bq_client = get_bq_client()
    
    # We can pull in 7-day or 14-day slices to monitor progress and stay within URL/response limits
    curr = datetime.date.fromisoformat(start_date)
    final = datetime.date.fromisoformat(end_date)
    
    total_raw_records = []
    total_api_calls = 0
    all_throttling = []
    
    slice_size = 7 # days per request
    print(f"Pulling in {slice_size}-day increments from {start_date} to {end_date}...")
    
    while curr <= final:
        slice_end = min(curr + datetime.timedelta(days=slice_size - 1), final)
        s_str = curr.strftime("%Y-%m-%d")
        e_str = slice_end.strftime("%Y-%m-%d")
        
        print(f"\nFetching {s_str} to {e_str}...")
        recs, calls, throttles = fetch_meta_segment_insights(token, s_str, e_str)
        print(f"  -> {len(recs)} records returned in {calls} API call(s).")
        
        total_raw_records.extend(recs)
        total_api_calls += calls
        all_throttling.extend(throttles)
        
        curr = slice_end + datetime.timedelta(days=1)
        time.sleep(1.0) # gentle rate-limiting gap between slices
        
    print(f"\nTotal raw records retrieved across window: {len(total_raw_records)}")
    print(f"Total Meta Graph API calls: {total_api_calls}")
    print(f"Total Throttling events: {len(all_throttling)}")
    if all_throttling:
        for t in all_throttling:
            print("  Throttling note:", t)
    else:
        print("  Throttling: None (zero 17/613 rate limits encountered)")
        
    # Archive to GCS
    print("\nArchiving raw data to GCS...")
    archive_raw_to_gcs(total_raw_records, start_date, end_date)
    
    # Transform and filter
    print("\nTransforming and filtering records...")
    transformed_rows, dropped_per_day, null_adsets = transform_segment_records(total_raw_records)
    print(f"Transformed rows to load: {len(transformed_rows)}")
    
    # Summary of dropped zero rows per day
    total_dropped = sum(dropped_per_day.values())
    print(f"\nTotal rows dropped (zero spend, impressions & purchases): {total_dropped}")
    print("Dropped zero-rows per day breakdown:")
    for d in sorted(dropped_per_day.keys()):
        print(f"  {d}: {dropped_per_day[d]} rows dropped")
        
    # Summary of null segment adsets with spend
    print(f"\nNull segment ad sets with spend: {len(null_adsets)}")
    if null_adsets:
        for na in null_adsets:
            print(f"  {na['spend_date']} | adset {na['adset_id']} ({na['adset_name']}) | spend: £{na['spend_gbp']}")
    else:
        print("  None. No ad set carried spend on a null segment.")
        
    # First date with non-null segments
    non_null_dates = sorted(list(set(r["spend_date"] for r in transformed_rows if r["user_segment_key"] != "null")))
    first_non_null_date = non_null_dates[0] if non_null_dates else "None"
    print(f"\nFirst date with non-null segments: {first_non_null_date}")
    
    # Load into BigQuery
    print(f"\nLoading into BigQuery: {SEGMENT_BQ_TABLE}...")
    load_segments_to_bq(bq_client, transformed_rows, start_date, end_date)
    
    # Uniqueness check on loaded table
    print("\n=== UNIQUENESS CHECK ===")
    uniq_sql = f"""
    SELECT
      COUNT(*) AS total_rows,
      COUNT(DISTINCT CONCAT(CAST(spend_date AS STRING), '|', campaign_id, '|', adset_id, '|', user_segment_key)) AS distinct_keys,
      COUNT(*) - COUNT(DISTINCT CONCAT(CAST(spend_date AS STRING), '|', campaign_id, '|', adset_id, '|', user_segment_key)) AS duplicate_count
    FROM `{SEGMENT_BQ_TABLE}`
    """
    uniq_res = list(bq_client.query(uniq_sql).result())[0]
    print(f"Total Rows:     {uniq_res.total_rows:,}")
    print(f"Distinct Keys:  {uniq_res.distinct_keys:,}")
    print(f"Duplicate Count: {uniq_res.duplicate_count}")
    if uniq_res.duplicate_count == 0:
        print("RESULT: PASS - Exactly 1 row per (spend_date, campaign_id, adset_id, user_segment_key).")
    else:
        print("RESULT: FAIL - Duplicates detected!")

if __name__ == "__main__":
    run_backfill("2026-09-01", "2026-11-03")

