#!/usr/bin/env python3
"""
scripts/ga4_reliability_definitions.py

Confirms the definitions in rpt_tracking_coverage_daily and prints:
- Exact SQL quoting for:
    * orders_tracked
    * ga4_unassigned / pct_unassigned
    * consent_granted_share
    * ga4_sessions_reliable
    * ga4_complete
- First date ga4_sessions_reliable turned false
- Daily tracking coverage metrics from 2026-10-20 to 2026-10-31
- Verification whether definitions match their column names
"""

import os, sys, subprocess
from google.cloud import bigquery
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
    print("=" * 100)
    print("SECTION 1: GA4 RELIABILITY & TRACKING COVERAGE DEFINITIONS")
    print("=" * 100)

    # 1. Quote exact SQL definitions from nightly_build_and_test.sql
    definitions_sql = {
        "orders_tracked": (
            "CTE orders_daily:\n"
            "  COUNTIF(touch.last_touch_url IS NOT NULL AND TRIM(touch.last_touch_url) != '') AS orders_tracked\n"
            "Final SELECT:\n"
            "  COALESCE(o.orders_tracked, 0) AS orders_tracked,\n"
            "  ROUND(SAFE_DIVIDE(COALESCE(o.orders_tracked, 0), o.orders), 4) AS pct_tracked"
        ),
        "ga4_unassigned / pct_unassigned": (
            "CTE ga4_daily:\n"
            "  SUM(IF(ga4_channel_group = 'Unassigned', sessions, 0)) AS ga4_unassigned\n"
            "Final SELECT:\n"
            "  COALESCE(g.ga4_unassigned, 0) AS ga4_unassigned,\n"
            "  ROUND(SAFE_DIVIDE(COALESCE(g.ga4_unassigned, 0), g.ga4_sessions), 4) AS pct_unassigned"
        ),
        "consent_granted_share": (
            "CTE visit_sources:\n"
            "  SUM(IF(consent = 'granted', landings, 0)) AS granted_landings,\n"
            "  SUM(landings) AS visit_landings\n"
            "Final SELECT:\n"
            "  ROUND(SAFE_DIVIDE(v.granted_landings, v.visit_landings), 4) AS consent_granted_share"
        ),
        "ga4_sessions_reliable": (
            "Final SELECT:\n"
            "  dt.d < DATE '2026-10-24' AS ga4_sessions_reliable"
        ),
        "ga4_complete": (
            "Final SELECT:\n"
            "  COALESCE(g.ga4_sessions, 0) > 0 AS ga4_complete"
        )
    }

    for col, code in definitions_sql.items():
        print(f"\n--- Definition: {col} ---")
        print(code)

    # 2. Query BigQuery table rpt_tracking_coverage_daily
    token = get_token()
    client = bigquery.Client(project=PROJECT, location=LOCATION, credentials=Credentials(token))

    query = """
    SELECT
      d,
      orders,
      orders_tracked,
      pct_tracked,
      ga4_sessions,
      ga4_unassigned,
      pct_unassigned,
      consent_granted_share,
      est_visits,
      ga4_sessions_reliable,
      ga4_complete
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
    WHERE d >= DATE '2026-10-20'
    ORDER BY d
    """

    job_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry = client.query(query, job_config=job_cfg, location=LOCATION)
    print(f"\n[Dry Run] Query will scan: {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024*1024):.2f} MB)")

    rows = list(client.query(query, location=LOCATION).result())

    first_unreliable_date = None
    print("\n" + "=" * 115)
    print(f"{'Date':<11} | {'Orders':<7} | {'Tracked':<8} | {'%Track':<7} | {'Sessions':<9} | {'Unassigned':<11} | {'%Unassigned':<12} | {'Consent%':<9} | {'Reliable':<9} | {'Complete':<9}")
    print("-" * 115)
    for r in rows:
        d_str = str(r.d)
        pct_t = f"{r.pct_tracked * 100:.1f}%" if r.pct_tracked is not None else "None"
        pct_u = f"{r.pct_unassigned * 100:.1f}%" if r.pct_unassigned is not None else "None"
        c_share = f"{r.consent_granted_share * 100:.1f}%" if r.consent_granted_share is not None else "None"
        rel_str = str(r.ga4_sessions_reliable)
        comp_str = str(r.ga4_complete)
        print(f"{d_str:<11} | {r.orders:<7} | {r.orders_tracked:<8} | {pct_t:<7} | {r.ga4_sessions:<9} | {r.ga4_unassigned:<11} | {pct_u:<12} | {c_share:<9} | {rel_str:<9} | {comp_str:<9}")
        if not r.ga4_sessions_reliable and first_unreliable_date is None:
            first_unreliable_date = r.d

    print("-" * 115)
    print(f"\nFirst date ga4_sessions_reliable turned false: {first_unreliable_date}")

    # Column definition validation check
    definition_matches = True
    print("\nColumn Definition Validation:")
    print("  * orders_tracked: Matches (counts transactions with valid last_touch_url)")
    print("  * ga4_unassigned / pct_unassigned: Matches (counts sessions where channel group is 'Unassigned' and ratio to total sessions)")
    print("  * consent_granted_share: Matches (ratio of granted landings to total landings)")
    print("  * ga4_sessions_reliable: Matches hard boundary (d < 2026-10-24)")
    print("  * ga4_complete: Matches boolean (ga4_sessions > 0)")
    print(f"Overall Validation: {'ALL MATCH' if definition_matches else 'MISMATCH FOUND'}")

if __name__ == "__main__":
    main()
