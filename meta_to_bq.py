#!/usr/bin/env python3
"""
Meta Graph API Insights to BigQuery Staging Loader
Idempotently loads daily campaign-level ad spend & conversion metrics into
rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily and archives raw responses in GCS.

Graph API Version: v26.0 (latest stable release per Meta changelog)
"""

import argparse
import datetime
import json
import os
import re
import sys
import subprocess
import urllib.request
import urllib.parse
import urllib.error

# Pinned Meta Graph API Version
GRAPH_API_VERSION = "v26.0"
AD_ACCOUNT_ID = "act_596135250732901"
ACCOUNT_ID_NUMERIC = "596135250732901"
GCS_BUCKET = "rh-ga4-491819-raw"
BQ_TABLE = "rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily"
HISTORY_BQ_TABLE = "rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily_history"
REACH_BQ_TABLE = "rh-ga4-491819.rh_curated_Bright_Test.stg_meta_campaign_reach"
BREAKDOWN_BQ_TABLE = "rh-ga4-491819.rh_curated_Bright_Test.stg_meta_breakdown_daily"
SEGMENT_BQ_TABLE = "rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily"
SECRET_ID = "meta-access-token"
GCP_PROJECT = "rh-ga4-491819"




def mask_token(text: str) -> str:
    """Mask any potential token patterns in strings or URLs for safe logging/errors."""
    if not text:
        return ""
    return re.sub(r'(access_token=)[^&"\s]+', r'\1[REDACTED]', str(text))


def get_gcp_credentials():
    """Obtain credentials, falling back to gcloud access token under impersonation."""
    try:
        from google.auth import default
        creds, _ = default()
        return creds
    except Exception:
        pass
    
    from google.oauth2.credentials import Credentials
    res = subprocess.run(["gcloud.cmd", "auth", "print-access-token"], capture_output=True, text=True, check=True)
    return Credentials(res.stdout.strip())


def get_secret_token(project_id: str = GCP_PROJECT, secret_id: str = SECRET_ID, version_id: str = "latest") -> str:
    """Read the Meta access token from Google Secret Manager.
    Holds the token in memory only; never prints or logs it.
    """
    try:
        from google.cloud import secretmanager
        creds = get_gcp_credentials()
        client = secretmanager.SecretManagerServiceClient(credentials=creds)
        name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
        response = client.access_secret_version(request={"name": name})
        token = response.payload.data.decode("UTF-8").strip()
        if not token:
            raise ValueError("Retrieved secret token is empty.")
        return token
    except Exception as e:
        safe_err = mask_token(str(e))
        raise RuntimeError(f"Failed to fetch secret '{secret_id}': {safe_err}")


def make_meta_request_with_retry(url: str, max_retries: int = 5, backoff_base: int = 30) -> dict:
    """Make HTTP GET request to Meta Graph API with exponential backoff on rate limits (codes 17, 613)."""
    import time
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers={"User-Agent": "RaffleHouse-MetaLoader/1.0"})
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            safe_err = mask_token(body)
            err_code = None
            err_msg = ""
            try:
                err_data = json.loads(body).get("error", {})
                err_code = err_data.get("code")
                err_msg = err_data.get("message", "")
            except Exception:
                pass
            
            # Rate limit codes: 17, 613 or message contains rate limit
            if err_code in (17, 613) or "rate limit" in err_msg.lower() or "calls to this api" in err_msg.lower() or "user request limit" in err_msg.lower():
                wait_time = backoff_base * (2 ** attempt)
                print(f"Rate limit encountered (code={err_code}). Waiting {wait_time}s before retry (attempt {attempt+1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            else:
                raise RuntimeError(f"Meta Graph API HTTP {e.code}: {safe_err}")
        except Exception as e:
            raise RuntimeError(f"Meta Graph API request failed: {mask_token(str(e))}")
    raise RuntimeError(f"Meta Graph API failed after {max_retries} retries due to rate limiting.")


def fetch_meta_insights(token: str, start_date: str, end_date: str) -> list:
    """Fetch campaign-level daily insights from Meta Graph API, handling pagination & rate limits."""
    fields = [
        "date_start",
        "campaign_id",
        "campaign_name",
        "objective",
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "cpm",
        "actions",
        "action_values",
        "account_currency"
    ]
    
    base_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": token,
        "level": "campaign",
        "time_increment": "1",
        "time_range": json.dumps({"since": start_date, "until": end_date}),
        "fields": ",".join(fields),
        "limit": "500"
    }
    
    all_data = []
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    page = 1
    
    while url:
        data = make_meta_request_with_retry(url)
        records = data.get("data", [])
        all_data.extend(records)
        page += 1
        
        # Follow pagination
        paging = data.get("paging", {})
        url = paging.get("next")
        
    return all_data


def extract_metrics(row: dict):
    """Extract purchases, purchase value, link clicks from actions/action_values."""
    actions = row.get("actions", [])
    action_values = row.get("action_values", [])
    
    purchases = 0.0
    purchase_val = 0.0
    action_type_used = None
    link_clicks = 0
    
    # 1. Purchases count
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
            
    # 2. Purchase value
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


def transform_records(raw_records: list, source_system: str = "meta_api"):
    """Transform Meta insights records into BigQuery stg_meta_ads_daily / history schema rows."""
    transformed = []
    loaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    observed_actions = set()
    
    for r in raw_records:
        purchases, purchase_val, link_clicks, act_type = extract_metrics(r)
        if act_type:
            observed_actions.add(act_type)
        
        transformed.append({
            "spend_date": r.get("date_start"),
            "account_id": ACCOUNT_ID_NUMERIC,
            "campaign_id": str(r.get("campaign_id", "")),
            "campaign_name": str(r.get("campaign_name", "")),
            "campaign_objective": r.get("objective"),
            "adset_id": None,
            "ad_id": None,
            "device": "all",
            "spend_gbp": round(float(r.get("spend", 0.0)), 2),
            "impressions": int(r.get("impressions", 0)),
            "reach": int(r.get("reach", 0)),
            "clicks": int(r.get("clicks", 0)),
            "link_clicks": link_clicks,
            "platform_conversions": round(purchases, 2),
            "platform_conversion_value_gbp": round(purchase_val, 2),
            "source_system": source_system,
            "loaded_at": loaded_at
        })
    return transformed, observed_actions


def save_raw_to_gcs(records: list, start_date: str, end_date: str, prefix: str = "meta/insights/"):
    """Save raw JSON payload to Google Cloud Storage partitioned by date."""
    from google.cloud import storage
    creds = get_gcp_credentials()
    client = storage.Client(credentials=creds, project=GCP_PROJECT)
    bucket = client.bucket(GCS_BUCKET)
    
    # Group by date_start
    grouped = {}
    for r in records:
        d = r.get("date_start", start_date)
        grouped.setdefault(d, []).append(r)
        
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    clean_prefix = prefix.strip("/")
    for d, items in grouped.items():
        blob_path = f"{clean_prefix}/dt={d}/insights_{ts}.json"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(json.dumps(items, indent=2), content_type="application/json")
    print(f"Archived {len(records)} raw records across {len(grouped)} dates to gs://{GCS_BUCKET}/{clean_prefix}/")


def load_into_bigquery(rows: list, start_date: str, end_date: str):
    """Idempotently delete existing meta_api rows for the date range and insert new rows."""
    from google.cloud import bigquery
    creds = get_gcp_credentials()
    default_config = bigquery.QueryJobConfig(labels={"datacloud": "antigravity", "purpose": "meta_feed"})
    client = bigquery.Client(credentials=creds, project=GCP_PROJECT, default_query_job_config=default_config)
    
    # 1. Delete date range for source_system = 'meta_api'
    del_query = f"""
    DELETE FROM `{BQ_TABLE}`
    WHERE source_system = 'meta_api'
      AND spend_date BETWEEN '{start_date}' AND '{end_date}'
    """
    print(f"Executing idempotent cleanup: DELETE BETWEEN '{start_date}' AND '{end_date}' (source_system = 'meta_api')...")
    del_job = client.query(del_query)
    del_job.result()
    
    # 2. Load new rows via batch LoadJob (no streaming buffer lock)
    if rows:
        print(f"Loading {len(rows)} rows into {BQ_TABLE} via batch load job...")
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            labels={"datacloud": "antigravity", "purpose": "meta_feed"}
        )
        job = client.load_table_from_json(rows, BQ_TABLE, job_config=job_config)
        job.result()
        print("BigQuery batch load complete.")
    else:
        print("No rows to load.")


def generate_fake_sample(start_date: str, end_date: str) -> list:
    """Generate representative fake Meta insights records for dry-run testing."""
    return [
        {
            "date_start": start_date,
            "campaign_id": "12021000000001",
            "campaign_name": "RH_Meta_Prospecting_Dorset_2026",
            "objective": "OUTCOME_SALES",
            "spend": "452.30",
            "impressions": 18250,
            "reach": 14100,
            "frequency": 1.29,
            "clicks": 320,
            "cpm": 24.78,
            "actions": [
                {"action_type": "link_click", "value": "295"},
                {"action_type": "purchase", "value": "18"},
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "18"}
            ],
            "action_values": [
                {"action_type": "purchase", "value": "630.00"},
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "630.00"}
            ],
            "account_currency": "GBP"
        },
        {
            "date_start": start_date,
            "campaign_id": "12021000000002",
            "campaign_name": "RH_Meta_Retargeting_London_2026",
            "objective": "OUTCOME_SALES",
            "spend": "215.80",
            "impressions": 7800,
            "reach": 5400,
            "frequency": 1.44,
            "clicks": 180,
            "cpm": 27.67,
            "actions": [
                {"action_type": "link_click", "value": "165"},
                {"action_type": "purchase", "value": "12"},
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "12"}
            ],
            "action_values": [
                {"action_type": "purchase", "value": "420.00"},
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "420.00"}
            ],
            "account_currency": "GBP"
        },
        {
            "date_start": end_date,
            "campaign_id": "12021000000003",
            "campaign_name": "RH_Meta_Remarketing_Car_2026",
            "objective": "OUTCOME_SALES",
            "spend": "120.45",
            "impressions": 4200,
            "reach": 3100,
            "frequency": 1.35,
            "clicks": 95,
            "cpm": 28.68,
            "actions": [
                {"action_type": "link_click", "value": "88"},
                {"action_type": "purchase", "value": "5"},
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "5"}
            ],
            "action_values": [
                {"action_type": "purchase", "value": "175.00"},
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "175.00"}
            ],
            "account_currency": "GBP"
        }
    ]


def get_europe_london_yesterday() -> datetime.date:
    """Get yesterday's date in Europe/London timezone."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/London")
    except Exception:
        try:
            import pytz
            tz = pytz.timezone("Europe/London")
        except Exception:
            from datetime import timezone, timedelta
            tz = timezone(timedelta(hours=1))
    today = datetime.datetime.now(tz).date()
    return today - datetime.timedelta(days=1)


def fetch_meta_reach_insights(token: str, start_date: str, end_date: str) -> list:
    """Fetch campaign-level reach and impressions with no time_increment (one row per campaign per window)."""
    fields = [
        "campaign_id",
        "campaign_name",
        "impressions",
        "reach",
        "frequency"
    ]
    base_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": token,
        "level": "campaign",
        "time_range": json.dumps({"since": start_date, "until": end_date}),
        "fields": ",".join(fields),
        "limit": "500"
    }
    all_data = []
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    page = 1
    while url:
        data = make_meta_request_with_retry(url)
        records = data.get("data", [])
        all_data.extend(records)
        page += 1
        paging = data.get("paging", {})
        url = paging.get("next")
    return all_data


def ensure_reach_table_exists(client, table_id: str = REACH_BQ_TABLE):
    """Ensure stg_meta_campaign_reach exists with the required schema."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_id}` (
      window_days INT64,
      window_start DATE,
      window_end DATE,
      campaign_id STRING,
      campaign_name STRING,
      impressions INT64,
      reach INT64,
      frequency NUMERIC,
      loaded_at TIMESTAMP
    )
    """
    client.query(ddl).result()


def load_reach_into_bigquery(client, window_days: int, window_start: str, window_end: str, records: list) -> int:
    """Replace rows for the specified window in stg_meta_campaign_reach."""
    ensure_reach_table_exists(client, REACH_BQ_TABLE)
    
    del_sql = f"DELETE FROM `{REACH_BQ_TABLE}` WHERE window_days = {window_days}"
    print(f"Replacing rows in {REACH_BQ_TABLE} for window_days = {window_days} ({window_start} to {window_end})...")
    client.query(del_sql).result()
    
    if not records:
        print(f"No reach records returned for window_days = {window_days}.")
        return 0
        
    loaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = []
    for r in records:
        impressions = int(r.get("impressions", 0))
        reach = int(r.get("reach", 0))
        freq_raw = r.get("frequency")
        if freq_raw is not None:
            frequency = round(float(freq_raw), 6)
        elif reach > 0:
            frequency = round(impressions / reach, 6)
        else:
            frequency = 0.0
            
        rows.append({
            "window_days": int(window_days),
            "window_start": window_start,
            "window_end": window_end,
            "campaign_id": str(r.get("campaign_id", "")),
            "campaign_name": str(r.get("campaign_name", "")),
            "impressions": impressions,
            "reach": reach,
            "frequency": frequency,
            "loaded_at": loaded_at
        })
        
    from google.cloud import bigquery
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        labels={"datacloud": "antigravity", "purpose": "meta_feed"}
    )
    job = client.load_table_from_json(rows, REACH_BQ_TABLE, job_config=job_config)
    job.result()
    print(f"Loaded {len(rows)} reach rows for window_days = {window_days}.")
    return len(rows)


def generate_fake_reach_sample(window_days: int, window_start: str, window_end: str) -> list:
    """Generate fake campaign reach insights for dry-run testing."""
    return [
        {
            "campaign_id": "120251663454160306",
            "campaign_name": "2026_Sept-Oct_London_PR_Charity",
            "impressions": 895994 if window_days == 7 else 3500000,
            "reach": 362958 if window_days == 7 else 1200000,
            "frequency": 2.468589 if window_days == 7 else 2.916667
        },
        {
            "campaign_id": "12021000000002",
            "campaign_name": "RH_Meta_Retargeting_London_2026",
            "impressions": 54000 if window_days == 7 else 210000,
            "reach": 22000 if window_days == 7 else 75000,
            "frequency": 2.454545 if window_days == 7 else 2.800000
        }
    ]


# -------------------------------------------------------------------------
# PART A: Historical Backfill (stg_meta_ads_daily_history)
# -------------------------------------------------------------------------

def ensure_history_table_exists(client, table_id: str = HISTORY_BQ_TABLE):
    """Ensure stg_meta_ads_daily_history exists with same schema as stg_meta_ads_daily."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_id}` (
      spend_date DATE,
      account_id STRING,
      campaign_id STRING,
      campaign_name STRING,
      campaign_objective STRING,
      adset_id STRING,
      ad_id STRING,
      device STRING,
      spend_gbp NUMERIC,
      impressions INT64,
      reach INT64,
      clicks INT64,
      link_clicks INT64,
      platform_conversions NUMERIC,
      platform_conversion_value_gbp NUMERIC,
      source_system STRING,
      loaded_at TIMESTAMP
    )
    PARTITION BY spend_date
    CLUSTER BY campaign_id
    OPTIONS(
      description="Historical backfill of Meta Ads daily data prior to 2026-09-01."
    )
    """
    client.query(ddl).result()


def is_month_loaded_in_history(client, start_date: str, end_date: str) -> bool:
    """Check if any rows exist in stg_meta_ads_daily_history for the given month."""
    ensure_history_table_exists(client, HISTORY_BQ_TABLE)
    query = f"""
    SELECT COUNT(*) AS cnt
    FROM `{HISTORY_BQ_TABLE}`
    WHERE spend_date BETWEEN '{start_date}' AND '{end_date}'
    """
    res = list(client.query(query).result())
    return (res[0].cnt > 0) if res else False


def load_history_into_bigquery(client, rows: list) -> int:
    """Load transformed historical records into stg_meta_ads_daily_history via batch LoadJob."""
    ensure_history_table_exists(client, HISTORY_BQ_TABLE)
    if not rows:
        return 0
    from google.cloud import bigquery
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND
    )
    job = client.load_table_from_json(rows, HISTORY_BQ_TABLE, job_config=job_config)
    job.result()
    return len(rows)


def generate_monthly_ranges(start_ym: str = "2023-10", end_ym: str = "2026-08"):
    """Generate (start_date, end_date) for each month in [start_ym, end_ym]."""
    import calendar
    ranges = []
    curr = datetime.date(int(start_ym.split("-")[0]), int(start_ym.split("-")[1]), 1)
    end_date = datetime.date(int(end_ym.split("-")[0]), int(end_ym.split("-")[1]), 1)
    
    while curr <= end_date:
        _, last_day = calendar.monthrange(curr.year, curr.month)
        m_start = curr.strftime("%Y-%m-%d")
        m_end = datetime.date(curr.year, curr.month, last_day).strftime("%Y-%m-%d")
        ranges.append((m_start, m_end))
        if curr.month == 12:
            curr = datetime.date(curr.year + 1, 1, 1)
        else:
            curr = datetime.date(curr.year, curr.month + 1, 1)
    return ranges


def run_history_backfill(token: str, dry_run: bool = False):
    """Backfill campaign-level daily insights from 2023-10 to 2026-08 resumably."""
    import time
    
    monthly_ranges = generate_monthly_ranges("2023-10", "2026-08")
    print(f"\n=== PART A: Meta History Backfill ({len(monthly_ranges)} months: 2023-10 to 2026-08) ===")
    
    if dry_run:
        print("[DRY RUN MODE] Simulating history backfill:")
        print(f"Target BigQuery Table: {HISTORY_BQ_TABLE}")
        print(f"Target GCS Prefix: gs://{GCS_BUCKET}/meta/insights_backfill/")
        for m_start, m_end in monthly_ranges:
            print(f"  - Month {m_start[:7]}: {m_start} to {m_end} -> fetch daily insights, archive raw, load as source_system='meta_api_backfill'")
        fake_sample = generate_fake_sample("2024-02-01", "2024-02-29")
        transformed_sample, _ = transform_records(fake_sample, source_system="meta_api_backfill")
        print("\nSample Transformed History Row:")
        print(json.dumps(transformed_sample[0], indent=2))
        return

    from google.cloud import bigquery

    bq_client = bigquery.Client(credentials=get_gcp_credentials(), project=GCP_PROJECT)
    ensure_history_table_exists(bq_client, HISTORY_BQ_TABLE)
    
    earliest_month_with_data = None
    total_months_loaded = 0
    total_rows_loaded = 0
    total_spend_loaded = 0.0
    
    for m_start, m_end in monthly_ranges:
        m_label = m_start[:7]
        # Resumability check
        if is_month_loaded_in_history(bq_client, m_start, m_end):
            print(f"[{m_label}] Already loaded in {HISTORY_BQ_TABLE}. Skipping.")
            continue
            
        print(f"[{m_label}] Querying Meta Graph API ({m_start} to {m_end})...")
        raw_records = fetch_meta_insights(token, m_start, m_end)
        
        if not raw_records:
            print(f"[{m_label}] 0 records returned from Meta.")
            time.sleep(0.5)
            continue
            
        if not earliest_month_with_data:
            earliest_month_with_data = m_label
            
        # Archive raw JSON to GCS
        save_raw_to_gcs(raw_records, m_start, m_end, prefix="meta/insights_backfill/")
        
        # Transform records
        transformed_rows, _ = transform_records(raw_records, source_system="meta_api_backfill")
        month_spend = sum(r["spend_gbp"] for r in transformed_rows)
        
        # Load into BigQuery
        inserted = load_history_into_bigquery(bq_client, transformed_rows)
        print(f"[{m_label}] Loaded {inserted} rows, spend = £{month_spend:,.2f} into {HISTORY_BQ_TABLE}.")
        
        total_months_loaded += 1
        total_rows_loaded += inserted
        total_spend_loaded += month_spend
        
        # Gentle backoff between months
        time.sleep(1.0)
        
    print(f"\nHistory Backfill Complete:")
    print(f"Earliest Month with Data: {earliest_month_with_data}")
    print(f"Total New Months Loaded: {total_months_loaded}")
    print(f"Total History Rows: {total_rows_loaded}")
    print(f"Total History Spend: £{total_spend_loaded:,.2f}")


# -------------------------------------------------------------------------
# PART B: Audience Breakdown Daily (stg_meta_breakdown_daily)
# -------------------------------------------------------------------------

def ensure_breakdown_table_exists(client, table_id: str = BREAKDOWN_BQ_TABLE):
    """Ensure stg_meta_breakdown_daily exists with the required schema."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_id}` (
      spend_date DATE,
      campaign_id STRING,
      campaign_name STRING,
      breakdown_type STRING,
      region STRING,
      age STRING,
      gender STRING,
      spend_gbp NUMERIC,
      impressions INT64,
      reach INT64,
      link_clicks INT64,
      purchases NUMERIC,
      purchase_value_gbp NUMERIC,
      loaded_at TIMESTAMP
    )
    PARTITION BY spend_date
    CLUSTER BY breakdown_type, campaign_id
    OPTIONS(
      description="Meta Ads daily audience breakdowns (region, age_gender)."
    )
    """
    client.query(ddl).result()


def fetch_meta_breakdown_insights(token: str, start_date: str, end_date: str, breakdown: str) -> list:
    """Fetch campaign-level daily insights with breakdowns (region or age,gender), handling pagination & rate limits."""
    fields = [
        "date_start",
        "campaign_id",
        "campaign_name",
        "spend",
        "impressions",
        "reach",
        "actions",
        "action_values"
    ]
    base_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": token,
        "level": "campaign",
        "time_increment": "1",
        "time_range": json.dumps({"since": start_date, "until": end_date}),
        "fields": ",".join(fields),
        "breakdowns": breakdown,
        "limit": "500"
    }
    all_data = []
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    page = 1
    while url:
        data = make_meta_request_with_retry(url)
        records = data.get("data", [])
        all_data.extend(records)
        page += 1
        paging = data.get("paging", {})
        url = paging.get("next")
    return all_data


def transform_breakdown_records(raw_records: list, breakdown_type: str) -> list:
    """Transform Meta insights breakdown records into BigQuery stg_meta_breakdown_daily rows."""
    transformed = []
    loaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    for r in raw_records:
        purchases, purchase_val, link_clicks, _ = extract_metrics(r)
        
        region_val = r.get("region") if breakdown_type == "region" else None
        age_val = r.get("age") if breakdown_type == "age_gender" else None
        gender_val = r.get("gender") if breakdown_type == "age_gender" else None
        
        transformed.append({
            "spend_date": r.get("date_start"),
            "campaign_id": str(r.get("campaign_id", "")),
            "campaign_name": str(r.get("campaign_name", "")),
            "breakdown_type": breakdown_type,
            "region": region_val,
            "age": age_val,
            "gender": gender_val,
            "spend_gbp": round(float(r.get("spend", 0.0)), 2),
            "impressions": int(r.get("impressions", 0)),
            "reach": int(r.get("reach", 0)),
            "link_clicks": link_clicks,
            "purchases": round(purchases, 2),
            "purchase_value_gbp": round(purchase_val, 2),
            "loaded_at": loaded_at
        })
    return transformed


def load_breakdowns_into_bigquery(client, rows: list, breakdown_type: str, start_date: str, end_date: str) -> int:
    """Idempotently delete existing breakdown rows for the date range and insert new rows."""
    ensure_breakdown_table_exists(client, BREAKDOWN_BQ_TABLE)
    
    del_query = f"""
    DELETE FROM `{BREAKDOWN_BQ_TABLE}`
    WHERE breakdown_type = '{breakdown_type}'
      AND spend_date BETWEEN '{start_date}' AND '{end_date}'
    """
    print(f"Executing idempotent cleanup on {BREAKDOWN_BQ_TABLE} for {breakdown_type} ({start_date} to {end_date})...")
    client.query(del_query).result()
    
    if rows:
        print(f"Inserting {len(rows)} breakdown rows into {BREAKDOWN_BQ_TABLE}...")
        from google.cloud import bigquery
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            labels={"datacloud": "antigravity", "purpose": "meta_feed"}
        )
        job = client.load_table_from_json(rows, BREAKDOWN_BQ_TABLE, job_config=job_config)
        job.result()
        print(f"BigQuery load complete for {breakdown_type}: {len(rows)} rows.")
        return len(rows)
    else:
        print(f"No breakdown rows to insert for {breakdown_type}.")
        return 0


def generate_fake_breakdown_sample(breakdown_type: str, start_date: str, end_date: str) -> list:
    """Generate fake breakdown records for dry-run testing."""
    if breakdown_type == "region":
        return [
            {
                "date_start": start_date,
                "campaign_id": "120251663454160306",
                "campaign_name": "2026_Sept-Oct_London_PR_Charity",
                "spend": "384.44",
                "impressions": 21810,
                "reach": 11187,
                "actions": [{"action_type": "link_click", "value": "383"}, {"action_type": "purchase", "value": "15"}],
                "action_values": [{"action_type": "purchase", "value": "525.00"}],
                "region": "England"
            }
        ]
    else:
        return [
            {
                "date_start": start_date,
                "campaign_id": "120251663454160306",
                "campaign_name": "2026_Sept-Oct_London_PR_Charity",
                "spend": "125.60",
                "impressions": 8500,
                "reach": 4200,
                "actions": [{"action_type": "link_click", "value": "112"}, {"action_type": "purchase", "value": "5"}],
                "action_values": [{"action_type": "purchase", "value": "175.00"}],
                "age": "25-34",
                "gender": "female"
            }
        ]


def run_breakdowns_for_range(token: str, bq_client, start_date: str, end_date: str, dry_run: bool = False):
    """Fetch and load both region and age_gender audience breakdowns for a date range."""
    configs = [
        ("region", "region"),
        ("age,gender", "age_gender")
    ]
    total_breakdown_rows = 0
    print(f"\n--- Audience Breakdowns ({start_date} to {end_date}) ---")
    
    for api_breakdown, b_type in configs:
        print(f"\nProcessing breakdown '{b_type}'...")
        if dry_run:
            fake_raw = generate_fake_breakdown_sample(b_type, start_date, end_date)
            transformed = transform_breakdown_records(fake_raw, b_type)
            print(f"[DRY RUN] Generated {len(transformed)} sample '{b_type}' rows:")
            print(json.dumps(transformed[0], indent=2))
            total_breakdown_rows += len(transformed)
        else:
            raw_recs = fetch_meta_breakdown_insights(token, start_date, end_date, api_breakdown)
            print(f"Retrieved {len(raw_recs)} raw breakdown records for '{b_type}'.")
            transformed = transform_breakdown_records(raw_recs, b_type)
            inserted = load_breakdowns_into_bigquery(bq_client, transformed, b_type, start_date, end_date)
            total_breakdown_rows += inserted
            
    return total_breakdown_rows


def ensure_segment_table_exists(client, table_name: str = SEGMENT_BQ_TABLE):
    """Ensure BigQuery staging table for audience segments exists with correct schema."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_name}`
    (
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
    )
    """
    client.query(ddl).result()


def generate_fake_segment_sample(start_date: str, end_date: str) -> list:
    """Generate fake segment records for dry-run testing."""
    return [
        {
            "date_start": start_date,
            "campaign_id": "120251663454160306",
            "campaign_name": "2026_Sept-Oct_London_PR_Charity",
            "adset_id": "120251663454170306",
            "adset_name": "Prospecting_Lookalike_1pct",
            "user_segment_key": "prospecting",
            "spend": "250.50",
            "impressions": 15000,
            "actions": [
                {"action_type": "link_click", "value": "220"},
                {"action_type": "purchase", "value": "8"}
            ],
            "action_values": [
                {"action_type": "purchase", "value": "280.00"}
            ]
        },
        {
            "date_start": start_date,
            "campaign_id": "120251663454160306",
            "campaign_name": "2026_Sept-Oct_London_PR_Charity",
            "adset_id": "120251663454180306",
            "adset_name": "Retargeting_Engaged_30d",
            "user_segment_key": "engaged",
            "spend": "95.20",
            "impressions": 4200,
            "actions": [
                {"action_type": "link_click", "value": "85"},
                {"action_type": "purchase", "value": "4"}
            ],
            "action_values": [
                {"action_type": "purchase", "value": "140.00"}
            ]
        }
    ]


def fetch_meta_segment_insights(token: str, start_date: str, end_date: str) -> list:
    """Fetch adset-level daily insights with user_segment_key breakdown, handling pagination & rate limits."""
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
    while url:
        data = make_meta_request_with_retry(url)
        records = data.get("data", [])
        all_data.extend(records)
        paging = data.get("paging", {})
        url = paging.get("next")
    return all_data


def transform_segment_records(raw_records: list) -> list:
    """Transform Meta insights segment records into BigQuery stg_meta_segment_daily rows."""
    transformed = []
    loaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    for r in raw_records:
        spend = round(float(r.get("spend", 0.0)), 2)
        impressions = int(r.get("impressions", 0))
        purchases, purchase_val, link_clicks, _ = extract_metrics(r)
        purchases = round(purchases, 2)
        purchase_val = round(purchase_val, 2)
        
        seg = r.get("user_segment_key")
        if seg is None or str(seg).strip().lower() in ("", "none"):
            if spend > 0:
                seg = "null"
            else:
                continue
                
        if spend == 0.0 and impressions == 0 and purchases == 0.0:
            continue
            
        transformed.append({
            "spend_date": r.get("date_start"),
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
    return transformed


def load_segments_into_bigquery(client, rows: list, start_date: str, end_date: str) -> int:
    """Idempotently delete existing segment rows for the date range and insert new rows."""
    ensure_segment_table_exists(client, SEGMENT_BQ_TABLE)
    
    del_query = f"""
    DELETE FROM `{SEGMENT_BQ_TABLE}`
    WHERE spend_date BETWEEN '{start_date}' AND '{end_date}'
    """
    print(f"Executing idempotent cleanup on {SEGMENT_BQ_TABLE} ({start_date} to {end_date})...")
    client.query(del_query).result()
    
    if rows:
        print(f"Inserting {len(rows)} segment rows into {SEGMENT_BQ_TABLE}...")
        from google.cloud import bigquery
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            labels={"datacloud": "antigravity", "purpose": "meta_feed"}
        )
        job = client.load_table_from_json(rows, SEGMENT_BQ_TABLE, job_config=job_config)
        job.result()
        print(f"BigQuery load complete for segments: {len(rows)} rows.")
        return len(rows)
    else:
        print("No segment rows to insert.")
        return 0


def run_segments_for_range(token: str, bq_client, start_date: str, end_date: str, dry_run: bool = False) -> int:
    """Fetch and load audience segments (user_segment_key) for a date range."""
    print(f"\n--- Audience Segments ({start_date} to {end_date}) ---")
    if dry_run:
        fake_raw = generate_fake_segment_sample(start_date, end_date)
        transformed = transform_segment_records(fake_raw)
        print(f"[DRY RUN] Generated {len(transformed)} sample segment rows:")
        print(json.dumps(transformed[0], indent=2))
        return len(transformed)
    
    raw_recs = fetch_meta_segment_insights(token, start_date, end_date)
    print(f"Retrieved {len(raw_recs)} raw segment records from Meta.")
    if raw_recs:
        save_raw_to_gcs(raw_recs, start_date, end_date, prefix="meta/insights_segments/")
    transformed = transform_segment_records(raw_recs)
    inserted = load_segments_into_bigquery(bq_client, transformed, start_date, end_date)
    return inserted


def append_daily_log(date_range: str, rows_loaded: int, spend_total: float, status: str, error_msg: str = ""):
    """Write a one-line log per run to logs/meta_daily.log with masked tokens."""
    log_dir = r"C:\Users\bryte\Downloads\Raffle-House_DW\logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "meta_daily.log")
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if status == "OK":
        line = f"{ts} | range={date_range} | rows={rows_loaded} | spend_total=£{spend_total:,.2f} | status=OK\n"
    else:
        safe_err = mask_token(error_msg).replace("\n", " ").strip()
        line = f"{ts} | range={date_range} | rows={rows_loaded} | spend_total=£{spend_total:,.2f} | status=ERROR | error={safe_err}\n"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)


def main():
    # Ensure service account impersonation is default if not set
    os.environ.setdefault("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com")
    
    parser = argparse.ArgumentParser(description="Load Meta Ads Insights into BigQuery")
    parser.add_argument("--start", required=False, default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=False, default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Dry run using fake sample; skips Meta and BigQuery calls")
    parser.add_argument("--backfill-history", action="store_true", help="Run historical backup to stg_meta_ads_daily_history (earliest date to 2026-08-31)")
    parser.add_argument("--backfill-breakdowns", action="store_true", help="Backfill audience breakdowns from 2026-09-01 to yesterday")
    parser.add_argument("--backfill-segments", action="store_true", help="Backfill audience segments from 2026-09-01 to yesterday")
    
    args = parser.parse_args()
    
    yesterday = get_europe_london_yesterday()
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    
    # -------------------------------------------------------------------------
    # Mode 1: Historical Backfill (Part A)
    # -------------------------------------------------------------------------
    if args.backfill_history:
        print(f"Meta Graph API Historical Backfill (Version: {GRAPH_API_VERSION})")
        print(f"Ad Account: {AD_ACCOUNT_ID} | Range: 2023-10-01 to 2026-08-31")
        if args.dry_run:
            run_history_backfill(token="", dry_run=True)
            return
        token = get_secret_token()
        print("Token fetched successfully (held in memory).")
        run_history_backfill(token=token, dry_run=False)
        return

    # -------------------------------------------------------------------------
    # Mode 2: Backfill Audience Breakdowns (Part B)
    # -------------------------------------------------------------------------
    if args.backfill_breakdowns:
        b_start = "2026-09-01"
        b_end = yesterday_str
        print(f"Meta Graph API Audience Breakdowns Backfill (Version: {GRAPH_API_VERSION})")
        print(f"Ad Account: {AD_ACCOUNT_ID} | Range: {b_start} to {b_end}")
        if args.dry_run:
            print("\n[DRY RUN MODE ACTIVATED - Breakdowns Backfill]")
            run_breakdowns_for_range(token="", bq_client=None, start_date=b_start, end_date=b_end, dry_run=True)
            return
        token = get_secret_token()
        print("Token fetched successfully (held in memory).")
        from google.cloud import bigquery
        bq_client = bigquery.Client(credentials=get_gcp_credentials(), project=GCP_PROJECT)
        run_breakdowns_for_range(token=token, bq_client=bq_client, start_date=b_start, end_date=b_end, dry_run=False)
        return

    # -------------------------------------------------------------------------
    # Mode: Backfill Audience Segments
    # -------------------------------------------------------------------------
    if args.backfill_segments:
        s_start = "2026-09-01"
        s_end = yesterday_str
        print(f"Meta Graph API Audience Segments Backfill (Version: {GRAPH_API_VERSION})")
        print(f"Ad Account: {AD_ACCOUNT_ID} | Range: {s_start} to {s_end}")
        if args.dry_run:
            print("\n[DRY RUN MODE ACTIVATED - Segments Backfill]")
            run_segments_for_range(token="", bq_client=None, start_date=s_start, end_date=s_end, dry_run=True)
            return
        token = get_secret_token()
        print("Token fetched successfully (held in memory).")
        from google.cloud import bigquery
        bq_client = bigquery.Client(credentials=get_gcp_credentials(), project=GCP_PROJECT)
        run_segments_for_range(token=token, bq_client=bq_client, start_date=s_start, end_date=s_end, dry_run=False)
        return

    # -------------------------------------------------------------------------
    # Mode 3: Normal Daily Pipeline Run (Nightly: spend + reach + 7-day breakdowns)
    # -------------------------------------------------------------------------
    if not args.start or not args.end:
        start_date = (yesterday - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
        end_date = yesterday_str
        print(f"No --start/--end specified. Defaulting to last 7 full days (yesterday minus 6 through yesterday, Europe/London): {start_date} to {end_date}")
    else:
        start_date = args.start
        end_date = args.end
    
    print(f"Meta Graph API Loader (Version: {GRAPH_API_VERSION})")
    print(f"Ad Account: {AD_ACCOUNT_ID} | Range: {start_date} to {end_date}")
    
    if args.dry_run:
        print("\n[DRY RUN MODE ACTIVATED - Nightly Flow]")
        print("Skipping Secret Manager token retrieval, Meta API call, and BigQuery write.")
        fake_records = generate_fake_sample(start_date, end_date)
        transformed_rows, observed_actions = transform_records(fake_records)
        
        print(f"\nDry Run Sample Rows Generated: {len(transformed_rows)}")
        print(f"Observed purchase action types: {observed_actions}")
        if transformed_rows:
            sample_keys = list(transformed_rows[0].keys())
            print(f"Output Schema Columns ({len(sample_keys)}):")
            for col in sample_keys:
                print(f"  - {col}")
                
            print("\nPreview of Rows to Load:")
            print(json.dumps(transformed_rows, indent=2))

        # Dry run for rolling windows
        print("\n[DRY RUN - Rolling Windows Campaign Reach & Impressions]")
        for w_days, w_start, w_end in [
            (7, (yesterday - datetime.timedelta(days=6)).strftime("%Y-%m-%d"), yesterday_str),
            (30, (yesterday - datetime.timedelta(days=29)).strftime("%Y-%m-%d"), yesterday_str)
        ]:
            fake_reach = generate_fake_reach_sample(w_days, w_start, w_end)
            print(f"Window: last {w_days} full days ({w_start} to {w_end}) -> {len(fake_reach)} sample campaigns")
            print(json.dumps(fake_reach, indent=2))

        # Dry run for daily audience breakdowns
        print("\n[DRY RUN - Daily Audience Breakdowns]")
        run_breakdowns_for_range(token="", bq_client=None, start_date=start_date, end_date=end_date, dry_run=True)

        # Dry run for daily audience segments
        print("\n[DRY RUN - Daily Audience Segments]")
        run_segments_for_range(token="", bq_client=None, start_date=start_date, end_date=end_date, dry_run=True)
        return

    # Real execution mode
    try:
        print("Fetching token from Secret Manager...")
        token = get_secret_token()
        print("Token fetched successfully (held in memory).")
        
        # 1. Daily Campaign Spend Load
        print("\n--- 1. Daily Campaign Spend Load ---")
        raw_records = fetch_meta_insights(token, start_date, end_date)
        print(f"Retrieved {len(raw_records)} records from Meta.")
        
        if raw_records:
            print("Archiving raw data to GCS...")
            save_raw_to_gcs(raw_records, start_date, end_date, prefix="meta/insights/")
            
            print("Transforming records...")
            transformed_rows, observed_actions = transform_records(raw_records)
            print(f"Purchase action types found in data: {list(observed_actions)}")
            
            total_spend = sum(r["spend_gbp"] for r in transformed_rows)
            print(f"Total spend across {len(transformed_rows)} rows: £{total_spend:,.2f}")
            
            print("Loading into BigQuery...")
            load_into_bigquery(transformed_rows, start_date, end_date)
            print("Daily spend load complete.")
        else:
            print("No records returned from Meta for the specified date range.")
            total_spend = 0.0
            transformed_rows = []

        from google.cloud import bigquery
        default_config = bigquery.QueryJobConfig(labels={"datacloud": "antigravity", "purpose": "meta_feed"})
        bq_client = bigquery.Client(credentials=get_gcp_credentials(), project=GCP_PROJECT, default_query_job_config=default_config)

        # 2. Rolling Windows Reach & Impressions
        print("\n--- 2. Rolling Windows Campaign Reach & Impressions ---")
        rolling_windows = [
            {
                "days": 7,
                "start": (yesterday - datetime.timedelta(days=6)).strftime("%Y-%m-%d"),
                "end": yesterday_str
            },
            {
                "days": 30,
                "start": (yesterday - datetime.timedelta(days=29)).strftime("%Y-%m-%d"),
                "end": yesterday_str
            }
        ]
        
        reach_rows_loaded = 0
        for win in rolling_windows:
            w_days = win["days"]
            w_start = win["start"]
            w_end = win["end"]
            print(f"Fetching reach for last {w_days} full days ({w_start} to {w_end})...")
            reach_records = fetch_meta_reach_insights(token, w_start, w_end)
            print(f"Retrieved {len(reach_records)} campaigns from Meta for {w_days}-day window.")
            n_rows = load_reach_into_bigquery(bq_client, w_days, w_start, w_end, reach_records)
            reach_rows_loaded += n_rows

        # 3. Audience Breakdowns (last 7 days by default, or specified date range)
        print("\n--- 3. Audience Breakdowns (Region & Age/Gender) ---")
        breakdown_rows_loaded = run_breakdowns_for_range(token, bq_client, start_date, end_date, dry_run=False)

        # 4. Audience Segments (last 7 days by default, or specified date range)
        print("\n--- 4. Audience Segments (user_segment_key) ---")
        segment_rows_loaded = run_segments_for_range(token, bq_client, start_date, end_date, dry_run=False)

        print(f"\nAll operations complete: {len(transformed_rows)} spend rows, {reach_rows_loaded} reach rows, {breakdown_rows_loaded} breakdown rows, {segment_rows_loaded} segment rows.")
        append_daily_log(f"{start_date}..{end_date}", len(transformed_rows), total_spend, "OK")
    except Exception as e:
        safe_err = mask_token(str(e))
        append_daily_log(f"{start_date}..{end_date}", 0, 0.0, "ERROR", safe_err)
        raise


if __name__ == "__main__":
    main()


