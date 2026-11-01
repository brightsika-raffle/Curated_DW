#!/usr/bin/env python3
"""
PART A: Hidden Pages (/blog, /winners, /past-draws) Analysis.
Window: 1 Aug 2026 to 31 Oct 2026 (Europe/London), with 1 Aug to 18 Oct pre-cookie banner period shown separately.
Strictly READ-ONLY. All numbers come directly from BigQuery raw source (analytics_354198910).
"""

import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

# Dry run limits
MAX_QUERY_BYTES = 5 * 1024**3   # 5 GB
MAX_TOTAL_BYTES = 15 * 1024**3  # 15 GB

def get_bq_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, credentials=Credentials(token), location=LOCATION)

MONTH_CHUNKS = [
    ("August 2026 (2026-08-01 to 2026-08-31)", "_TABLE_SUFFIX BETWEEN '20260801' AND '20260831'"),
    ("September 2026 (2026-09-01 to 2026-09-30)", "_TABLE_SUFFIX BETWEEN '20260901' AND '20260930'"),
    ("October 2026 (2026-10-01 to 2026-10-31)", "(_TABLE_SUFFIX BETWEEN '20261001' AND '20261030' OR _TABLE_SUFFIX = 'intraday_20261031')"),
]

MONTH_QUERY_TEMPLATE = """
WITH raw_events AS (
  SELECT
    PARSE_DATE('%Y%m%d', event_date) AS event_date,
    TIMESTAMP_MICROS(event_timestamp) AS event_ts,
    event_name,
    user_pseudo_id,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS ga_session_id,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location') AS page_location,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_referrer') AS page_referrer,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'transaction_id') AS transaction_id,
    (SELECT COALESCE(value.float_value, value.double_value, CAST(value.int_value AS FLOAT64)) FROM UNNEST(event_params) WHERE key = 'value') AS purchase_val,
    COALESCE(session_traffic_source_last_click.cross_channel_campaign.source, traffic_source.source) AS src,
    COALESCE(session_traffic_source_last_click.cross_channel_campaign.medium, traffic_source.medium) AS med,
    session_traffic_source_last_click.cross_channel_campaign.default_channel_group AS dcg
  FROM `rh-ga4-491819.analytics_354198910.events_*`
  WHERE {month_filter}
    AND event_name IN ('session_start', 'page_view', 'BoughtTickets', 'purchase')
),
sessions AS (
  SELECT
    event_date,
    DATE_TRUNC(event_date, WEEK(MONDAY)) AS week_start,
    user_pseudo_id,
    ga_session_id,
    REGEXP_EXTRACT(page_location, r'https?://[^/]+(/[^?#]*)') AS landing_path,
    src,
    med,
    dcg,
    page_referrer
  FROM raw_events
  WHERE user_pseudo_id IS NOT NULL AND ga_session_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER(PARTITION BY user_pseudo_id, ga_session_id ORDER BY IF(event_name = 'session_start', 0, 1), event_ts ASC) = 1
),
session_purchases AS (
  SELECT
    user_pseudo_id,
    ga_session_id,
    COUNT(DISTINCT transaction_id) AS orders,
    COUNTIF(event_name = 'BoughtTickets') AS bt_events,
    COUNTIF(event_name = 'purchase') AS purchase_events,
    SUM(IF(event_name = 'purchase', purchase_val, 0)) AS revenue_gbp
  FROM raw_events
  WHERE event_name IN ('purchase', 'BoughtTickets')
    AND user_pseudo_id IS NOT NULL AND ga_session_id IS NOT NULL
  GROUP BY 1, 2
)
SELECT
  'page_views' AS metric_type,
  CASE
    WHEN LOWER(REGEXP_EXTRACT(page_location, r'https?://[^/]+(/[^?#]*)')) = '/blog' OR LOWER(REGEXP_EXTRACT(page_location, r'https?://[^/]+(/[^?#]*)')) LIKE '/blog/%' THEN '/blog'
    WHEN LOWER(REGEXP_EXTRACT(page_location, r'https?://[^/]+(/[^?#]*)')) = '/winners' OR LOWER(REGEXP_EXTRACT(page_location, r'https?://[^/]+(/[^?#]*)')) LIKE '/winners/%' THEN '/winners'
    WHEN LOWER(REGEXP_EXTRACT(page_location, r'https?://[^/]+(/[^?#]*)')) = '/past-draws' OR LOWER(REGEXP_EXTRACT(page_location, r'https?://[^/]+(/[^?#]*)')) LIKE '/past-draws/%' THEN '/past-draws'
    ELSE 'other'
  END AS target_page,
  CAST(NULL AS STRING) AS landing_path,
  CAST(NULL AS DATE) AS week_start,
  event_date <= DATE '2026-10-18' AS is_pre_banner,
  CAST(NULL AS STRING) AS source,
  CAST(NULL AS STRING) AS medium,
  CAST(NULL AS BOOL) AS is_organic_search,
  COUNT(*) AS metric_val1,
  0 AS metric_val2,
  0 AS metric_val3,
  0.0 AS metric_val4
FROM raw_events
WHERE event_name = 'page_view'
GROUP BY 1, 2, 5

UNION ALL

SELECT
  'landing_sessions' AS metric_type,
  CASE
    WHEN LOWER(s.landing_path) = '/blog' OR LOWER(s.landing_path) LIKE '/blog/%' THEN '/blog'
    WHEN LOWER(s.landing_path) = '/winners' OR LOWER(s.landing_path) LIKE '/winners/%' THEN '/winners'
    WHEN LOWER(s.landing_path) = '/past-draws' OR LOWER(s.landing_path) LIKE '/past-draws/%' THEN '/past-draws'
    ELSE 'other'
  END AS target_page,
  CAST(NULL AS STRING) AS landing_path,
  s.week_start,
  s.event_date <= DATE '2026-10-18' AS is_pre_banner,
  COALESCE(s.src, '(not set)') AS source,
  COALESCE(s.med, '(not set)') AS medium,
  (LOWER(s.med) = 'organic' OR s.dcg = 'Organic Search' OR (s.page_referrer LIKE '%google.%' AND LOWER(COALESCE(s.med, '')) != 'cpc')) AS is_organic_search,
  COUNT(DISTINCT CONCAT(s.user_pseudo_id, '.', CAST(s.ga_session_id AS STRING))) AS metric_val1,
  COUNT(DISTINCT IF(p.orders > 0 OR p.purchase_events > 0 OR p.bt_events > 0, CONCAT(s.user_pseudo_id, '.', CAST(s.ga_session_id AS STRING)), NULL)) AS metric_val2,
  SUM(COALESCE(p.orders, 0)) AS metric_val3,
  ROUND(SUM(COALESCE(p.revenue_gbp, 0)), 2) AS metric_val4
FROM sessions s
LEFT JOIN session_purchases p USING (user_pseudo_id, ga_session_id)
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8

UNION ALL

SELECT
  'blog_top_posts' AS metric_type,
  '/blog' AS target_page,
  s.landing_path,
  CAST(NULL AS DATE) AS week_start,
  s.event_date <= DATE '2026-10-18' AS is_pre_banner,
  COALESCE(s.src, '(not set)') AS source,
  COALESCE(s.med, '(not set)') AS medium,
  (LOWER(s.med) = 'organic' OR s.dcg = 'Organic Search' OR (s.page_referrer LIKE '%google.%' AND LOWER(COALESCE(s.med, '')) != 'cpc')) AS is_organic_search,
  COUNT(DISTINCT CONCAT(s.user_pseudo_id, '.', CAST(s.ga_session_id AS STRING))) AS metric_val1,
  0 AS metric_val2,
  0 AS metric_val3,
  0.0 AS metric_val4
FROM sessions s
WHERE LOWER(s.landing_path) = '/blog' OR LOWER(s.landing_path) LIKE '/blog/%'
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
"""

def main():
    print("=" * 80)
    print("PART A: HIDDEN PAGES REPORT (/blog, /winners, /past-draws)")
    print("Window: 1 Aug 2026 to 31 Oct 2026 (92 complete days, Europe/London)")
    print("Pre-cookie banner baseline: 1 Aug 2026 to 18 Oct 2026")
    print("Source: Raw GA4 analytics_354198910 (raw source, outside agreed tables)")
    print("=" * 80)

    client = get_bq_client()

    # Pre-flight dry runs
    print("\n--- STEP 0: DRY RUN AND SCAN BYTE VERIFICATION ---")
    total_bytes = 0
    month_queries = []
    for m_label, m_filter in MONTH_CHUNKS:
        q = MONTH_QUERY_TEMPLATE.format(month_filter=m_filter)
        month_queries.append((m_label, q))
        dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
        dry_job = client.query(q, job_config=dry_cfg)
        b = dry_job.total_bytes_processed
        total_bytes += b
        status_5g = "PASS (< 5 GB)" if b < MAX_QUERY_BYTES else "FAIL (>= 5 GB)"
        print(f"Dry run [{m_label}]: {b:,} bytes ({b / (1024**3):.2f} GB) -> {status_5g}")
        if b >= MAX_QUERY_BYTES:
            raise RuntimeError(f"Query for {m_label} exceeds 5 GB limit: {b} bytes")

    status_15g = "PASS (< 15 GB)" if total_bytes < MAX_TOTAL_BYTES else "FAIL (>= 15 GB)"
    print(f"Total scan across all 3 monthly queries: {total_bytes:,} bytes ({total_bytes / (1024**3):.2f} GB) -> {status_15g}")
    if total_bytes >= MAX_TOTAL_BYTES:
        raise RuntimeError(f"Total scan exceeds 15 GB limit: {total_bytes} bytes")

    # Estimated query cost calculation
    tib_scanned = total_bytes / (1024**4)
    cost_usd = tib_scanned * 6.25
    usd_per_gbp = 1.30
    cost_gbp = cost_usd / usd_per_gbp
    print(f"Query cost at $6.25/TiB (USD/GBP = {usd_per_gbp}): ${cost_usd:.4f} (£{cost_gbp:.4f})")

    # Execute monthly queries and aggregate results locally
    print("\n--- STEP 1: EXECUTING QUERIES ---")
    all_rows = []
    for m_label, q in month_queries:
        print(f"Executing query for {m_label}...")
        cfg = bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})
        job = client.query(q, job_config=cfg)
        rows = list(job.result())
        print(f"  Received {len(rows):,} aggregated rows.")
        all_rows.extend(rows)

    # In-memory aggregation structures
    # 1. Total page views: {target_page: {all_90d: count, pre19: count}}
    page_views = defaultdict(lambda: {"full": 0, "pre19": 0})
    site_page_views = {"full": 0, "pre19": 0}

    # 2. Landing sessions & purchases:
    # {target_page: {sessions, organic_sessions, purchasing_sessions, orders, revenue_gbp}}
    landing_stats_full = defaultdict(lambda: {"sessions": 0, "organic": 0, "purchasing_sessions": 0, "orders": 0, "revenue": 0.0})
    landing_stats_pre19 = defaultdict(lambda: {"sessions": 0, "organic": 0, "purchasing_sessions": 0, "orders": 0, "revenue": 0.0})

    # 3. Source/medium split per target page:
    # {target_page: {source_medium: sessions}}
    src_med_full = defaultdict(lambda: defaultdict(int))
    src_med_pre19 = defaultdict(lambda: defaultdict(int))

    # 4. Weekly trend:
    # {week_start: {target_page: sessions, target_page_organic: sessions}}
    weekly_trend = defaultdict(lambda: defaultdict(int))

    # 5. Blog top posts:
    # {landing_path: {"full_sessions": 0, "full_organic": 0, "pre19_sessions": 0, "pre19_organic": 0}}
    blog_posts = defaultdict(lambda: {"full_sessions": 0, "full_organic": 0, "pre19_sessions": 0, "pre19_organic": 0})

    for r in all_rows:
        mtype = r["metric_type"]
        page = r["target_page"]
        is_pre = r["is_pre_banner"]

        if mtype == "page_views":
            cnt = r["metric_val1"]
            page_views[page]["full"] += cnt
            site_page_views["full"] += cnt
            if is_pre:
                page_views[page]["pre19"] += cnt
                site_page_views["pre19"] += cnt

        elif mtype == "landing_sessions":
            sess = r["metric_val1"]
            pur_sess = r["metric_val2"]
            ords = r["metric_val3"]
            rev = r["metric_val4"]
            wk = r["week_start"]
            src = r["source"]
            med = r["medium"]
            is_org = r["is_organic_search"]
            sm = f"{src} / {med}"

            # Full window
            landing_stats_full[page]["sessions"] += sess
            if is_org:
                landing_stats_full[page]["organic"] += sess
            landing_stats_full[page]["purchasing_sessions"] += pur_sess
            landing_stats_full[page]["orders"] += ords
            landing_stats_full[page]["revenue"] += rev

            src_med_full[page][sm] += sess
            if is_org:
                src_med_full[page]["[ORGANIC SEARCH TOTAL]"] += sess

            if wk:
                weekly_trend[wk][page] += sess
                if is_org:
                    weekly_trend[wk][f"{page}_organic"] += sess

            # Pre-banner window (1 Aug - 18 Oct)
            if is_pre:
                landing_stats_pre19[page]["sessions"] += sess
                if is_org:
                    landing_stats_pre19[page]["organic"] += sess
                landing_stats_pre19[page]["purchasing_sessions"] += pur_sess
                landing_stats_pre19[page]["orders"] += ords
                landing_stats_pre19[page]["revenue"] += rev
                src_med_pre19[page][sm] += sess
                if is_org:
                    src_med_pre19[page]["[ORGANIC SEARCH TOTAL]"] += sess

        elif mtype == "blog_top_posts":
            path = r["landing_path"] or "/blog"
            sess = r["metric_val1"]
            is_org = r["is_organic_search"]

            blog_posts[path]["full_sessions"] += sess
            if is_org:
                blog_posts[path]["full_organic"] += sess
            if is_pre:
                blog_posts[path]["pre19_sessions"] += sess
                if is_org:
                    blog_posts[path]["pre19_organic"] += sess

    # Compute site totals for landing sessions & purchases
    site_totals_full = {"sessions": 0, "organic": 0, "purchasing_sessions": 0, "orders": 0, "revenue": 0.0}
    for p, stats in landing_stats_full.items():
        site_totals_full["sessions"] += stats["sessions"]
        site_totals_full["organic"] += stats["organic"]
        site_totals_full["purchasing_sessions"] += stats["purchasing_sessions"]
        site_totals_full["orders"] += stats["orders"]
        site_totals_full["revenue"] += stats["revenue"]

    site_totals_pre19 = {"sessions": 0, "organic": 0, "purchasing_sessions": 0, "orders": 0, "revenue": 0.0}
    for p, stats in landing_stats_pre19.items():
        site_totals_pre19["sessions"] += stats["sessions"]
        site_totals_pre19["organic"] += stats["organic"]
        site_totals_pre19["purchasing_sessions"] += stats["purchasing_sessions"]
        site_totals_pre19["orders"] += stats["orders"]
        site_totals_pre19["revenue"] += stats["revenue"]

    # Print Report
    target_pages = ["/blog", "/winners", "/past-draws"]

    print("\n" + "=" * 80)
    print("A2: TOTAL PAGE VIEWS (LANDING OR NON-LANDING)")
    print("=" * 80)
    print(f"{'Page':<15} {'90-Day Page Views':<20} {'Share of Site PV':<20} {'Pre-19 Oct PV':<15} {'Pre-19 Oct Share'}")
    print("-" * 80)
    for p in target_pages:
        pv_f = page_views[p]["full"]
        sh_f = (pv_f / site_page_views["full"] * 100) if site_page_views["full"] else 0
        pv_p = page_views[p]["pre19"]
        sh_p = (pv_p / site_page_views["pre19"] * 100) if site_page_views["pre19"] else 0
        print(f"{p:<15} {pv_f:<20,d} {sh_f:<20.3f}% {pv_p:<15,d} {sh_p:<.3f}%")
    print(f"{'Target Total':<15} {sum(page_views[p]['full'] for p in target_pages):<20,d} {(sum(page_views[p]['full'] for p in target_pages)/site_page_views['full']*100):<20.3f}% {sum(page_views[p]['pre19'] for p in target_pages):<15,d} {(sum(page_views[p]['pre19'] for p in target_pages)/site_page_views['pre19']*100):<.3f}%")
    print(f"{'Entire Site':<15} {site_page_views['full']:<20,d} {'100.000%':<20} {site_page_views['pre19']:<15,d} {'100.000%'}")

    print("\n" + "=" * 80)
    print("A1 & A3: LANDING SESSIONS, PURCHASES & REVENUE (FULL 90-DAY WINDOW: 1 AUG TO 31 OCT 2026)")
    print("=" * 80)
    print(f"{'Page':<12} {'Landing Sess':<14} {'Organic Sess':<14} {'Org Share':<12} {'Purch Sess':<12} {'Orders':<10} {'Revenue (£)':<14} {'Order Share':<12} {'Rev Share'}")
    print("-" * 105)
    for p in target_pages:
        st = landing_stats_full[p]
        org_sh = (st["organic"] / site_totals_full["organic"] * 100) if site_totals_full["organic"] else 0
        ord_sh = (st["orders"] / site_totals_full["orders"] * 100) if site_totals_full["orders"] else 0
        rev_sh = (st["revenue"] / site_totals_full["revenue"] * 100) if site_totals_full["revenue"] else 0
        print(f"{p:<12} {st['sessions']:<14,d} {st['organic']:<14,d} {org_sh:<12.3f}% {st['purchasing_sessions']:<12,d} {st['orders']:<10,d} £{st['revenue']:<13,.2f} {ord_sh:<12.3f}% {rev_sh:<.3f}%")
    print("-" * 105)
    tgt_sess = sum(landing_stats_full[p]['sessions'] for p in target_pages)
    tgt_org = sum(landing_stats_full[p]['organic'] for p in target_pages)
    tgt_ps = sum(landing_stats_full[p]['purchasing_sessions'] for p in target_pages)
    tgt_ord = sum(landing_stats_full[p]['orders'] for p in target_pages)
    tgt_rev = sum(landing_stats_full[p]['revenue'] for p in target_pages)
    print(f"{'Target Total':<12} {tgt_sess:<14,d} {tgt_org:<14,d} {(tgt_org/site_totals_full['organic']*100):<12.3f}% {tgt_ps:<12,d} {tgt_ord:<10,d} £{tgt_rev:<13,.2f} {(tgt_ord/site_totals_full['orders']*100):<12.3f}% {(tgt_rev/site_totals_full['revenue']*100):<.3f}%")
    print(f"{'Other Pages':<12} {landing_stats_full['other']['sessions']:<14,d} {landing_stats_full['other']['organic']:<14,d} {(landing_stats_full['other']['organic']/site_totals_full['organic']*100):<12.3f}% {landing_stats_full['other']['purchasing_sessions']:<12,d} {landing_stats_full['other']['orders']:<10,d} £{landing_stats_full['other']['revenue']:<13,.2f} {(landing_stats_full['other']['orders']/site_totals_full['orders']*100):<12.3f}% {(landing_stats_full['other']['revenue']/site_totals_full['revenue']*100):<.3f}%")
    print(f"{'Entire Site':<12} {site_totals_full['sessions']:<14,d} {site_totals_full['organic']:<14,d} {'100.000%':<12} {site_totals_full['purchasing_sessions']:<12,d} {site_totals_full['orders']:<10,d} £{site_totals_full['revenue']:<13,.2f} {'100.000%':<12} {'100.000%'}")

    print("\n" + "=" * 80)
    print("A1 & A3: PRE-COOKIE BANNER BASELINE (1 AUG TO 18 OCT 2026)")
    print("=" * 80)
    print(f"{'Page':<12} {'Landing Sess':<14} {'Organic Sess':<14} {'Org Share':<12} {'Purch Sess':<12} {'Orders':<10} {'Revenue (£)':<14} {'Order Share':<12} {'Rev Share'}")
    print("-" * 105)
    for p in target_pages:
        st = landing_stats_pre19[p]
        org_sh = (st["organic"] / site_totals_pre19["organic"] * 100) if site_totals_pre19["organic"] else 0
        ord_sh = (st["orders"] / site_totals_pre19["orders"] * 100) if site_totals_pre19["orders"] else 0
        rev_sh = (st["revenue"] / site_totals_pre19["revenue"] * 100) if site_totals_pre19["revenue"] else 0
        print(f"{p:<12} {st['sessions']:<14,d} {st['organic']:<14,d} {org_sh:<12.3f}% {st['purchasing_sessions']:<12,d} {st['orders']:<10,d} £{st['revenue']:<13,.2f} {ord_sh:<12.3f}% {rev_sh:<.3f}%")
    print("-" * 105)
    tgt_sess_p = sum(landing_stats_pre19[p]['sessions'] for p in target_pages)
    tgt_org_p = sum(landing_stats_pre19[p]['organic'] for p in target_pages)
    tgt_ps_p = sum(landing_stats_pre19[p]['purchasing_sessions'] for p in target_pages)
    tgt_ord_p = sum(landing_stats_pre19[p]['orders'] for p in target_pages)
    tgt_rev_p = sum(landing_stats_pre19[p]['revenue'] for p in target_pages)
    print(f"{'Target Total':<12} {tgt_sess_p:<14,d} {tgt_org_p:<14,d} {(tgt_org_p/site_totals_pre19['organic']*100):<12.3f}% {tgt_ps_p:<12,d} {tgt_ord_p:<10,d} £{tgt_rev_p:<13,.2f} {(tgt_ord_p/site_totals_pre19['orders']*100):<12.3f}% {(tgt_rev_p/site_totals_pre19['revenue']*100):<.3f}%")
    print(f"{'Other Pages':<12} {landing_stats_pre19['other']['sessions']:<14,d} {landing_stats_pre19['other']['organic']:<14,d} {(landing_stats_pre19['other']['organic']/site_totals_pre19['organic']*100):<12.3f}% {landing_stats_pre19['other']['purchasing_sessions']:<12,d} {landing_stats_pre19['other']['orders']:<10,d} £{landing_stats_pre19['other']['revenue']:<13,.2f} {(landing_stats_pre19['other']['orders']/site_totals_pre19['orders']*100):<12.3f}% {(landing_stats_pre19['other']['revenue']/site_totals_pre19['revenue']*100):<.3f}%")
    print(f"{'Entire Site':<12} {site_totals_pre19['sessions']:<14,d} {site_totals_pre19['organic']:<14,d} {'100.000%':<12} {site_totals_pre19['purchasing_sessions']:<12,d} {site_totals_pre19['orders']:<10,d} £{site_totals_pre19['revenue']:<13,.2f} {'100.000%':<12} {'100.000%'}")

    print("\n" + "=" * 80)
    print("A1: TOP SOURCE / MEDIUM BREAKDOWN FOR TARGET PAGES (90 DAYS)")
    print("=" * 80)
    for p in target_pages:
        print(f"\n--- Landing Traffic Sources for {p} ---")
        items = sorted(src_med_full[p].items(), key=lambda x: x[1], reverse=True)
        for sm, cnt in items[:8]:
            sh = cnt / landing_stats_full[p]["sessions"] * 100 if landing_stats_full[p]["sessions"] else 0
            print(f"  {sm:<40} {cnt:>6,d} sessions ({sh:>5.1f}%)")

    print("\n" + "=" * 80)
    print("A1: WEEKLY TREND OF LANDING SESSIONS (90 DAYS)")
    print("=" * 80)
    print(f"{'Week Commencing (Mon)':<24} {'/blog Total (Org)':<22} {'/winners Total (Org)':<24} {'/past-draws Total (Org)'}")
    print("-" * 95)
    for wk in sorted(weekly_trend.keys()):
        b_t = weekly_trend[wk]["/blog"]
        b_o = weekly_trend[wk]["/blog_organic"]
        w_t = weekly_trend[wk]["/winners"]
        w_o = weekly_trend[wk]["/winners_organic"]
        pd_t = weekly_trend[wk]["/past-draws"]
        pd_o = weekly_trend[wk]["/past-draws_organic"]
        print(f"{str(wk):<24} {b_t:>6,d} ({b_o:>3,d})            {w_t:>6,d} ({w_o:>3,d})              {pd_t:>6,d} ({pd_o:>3,d})")

    print("\n" + "=" * 80)
    print("A4: TOP 10 BLOG POSTS BY ORGANIC LANDING SESSIONS (90 DAYS)")
    print("=" * 80)
    sorted_posts = sorted(blog_posts.items(), key=lambda x: (x[1]["full_organic"], x[1]["full_sessions"]), reverse=True)
    print(f"{'Rank':<5} {'Blog Post URL Path':<65} {'Organic':<10} {'Total Sess'}")
    print("-" * 90)
    for i, (path, stats) in enumerate(sorted_posts[:10], start=1):
        print(f"{i:<5} {path:<65} {stats['full_organic']:<10,d} {stats['full_sessions']:,d}")

    print("\n" + "=" * 80)
    print("A5: MATERIALITY ASSESSMENT PER PAGE")
    print("Thresholds: Material if > 1.000% of organic landing sessions OR > 0.500% of purchases in window.")
    print("=" * 80)
    for p in target_pages:
        # Full window
        st_f = landing_stats_full[p]
        org_sh_f = (st_f["organic"] / site_totals_full["organic"] * 100) if site_totals_full["organic"] else 0
        ord_sh_f = (st_f["orders"] / site_totals_full["orders"] * 100) if site_totals_full["orders"] else 0
        is_mat_f = (org_sh_f > 1.0) or (ord_sh_f > 0.5)

        # Pre-19 Oct window
        st_p = landing_stats_pre19[p]
        org_sh_p = (st_p["organic"] / site_totals_pre19["organic"] * 100) if site_totals_pre19["organic"] else 0
        ord_sh_p = (st_p["orders"] / site_totals_pre19["orders"] * 100) if site_totals_pre19["orders"] else 0
        is_mat_p = (org_sh_p > 1.0) or (ord_sh_p > 0.5)

        print(f"\nTarget Page: {p}")
        print(f"  [Full 90-Day Window: 1 Aug - 31 Oct 2026]")
        print(f"    - Organic Landing Share : {org_sh_f:.3f}% (Threshold: > 1.000%) -> {'EXCEEDS' if org_sh_f > 1.0 else 'BELOW'}")
        print(f"    - Site Purchase Share   : {ord_sh_f:.3f}% (Threshold: > 0.500%) -> {'EXCEEDS' if ord_sh_f > 0.5 else 'BELOW'}")
        print(f"    - Verdict               : {'MATERIAL' if is_mat_f else 'NOT MATERIAL'}")
        print(f"  [Pre-Cookie Banner Window: 1 Aug - 18 Oct 2026]")
        print(f"    - Organic Landing Share : {org_sh_p:.3f}% (Threshold: > 1.000%) -> {'EXCEEDS' if org_sh_p > 1.0 else 'BELOW'}")
        print(f"    - Site Purchase Share   : {ord_sh_p:.3f}% (Threshold: > 0.500%) -> {'EXCEEDS' if ord_sh_p > 0.5 else 'BELOW'}")
        print(f"    - Verdict               : {'MATERIAL' if is_mat_p else 'NOT MATERIAL'}")

    print("\n" + "=" * 80)
    print("END OF PART A SCRIPT OUTPUT")
    print("=" * 80)

if __name__ == "__main__":
    main()
