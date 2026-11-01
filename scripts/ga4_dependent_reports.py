#!/usr/bin/env python3
"""
scripts/ga4_dependent_reports.py

Section 2 audit: Lists each table and view in rh_curated_Bright_Test and published reports
that depend on GA4 sessions, visits, or GA4 purchases, with their columns and affected date ranges.
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
    print("=" * 115)
    print("SECTION 2: GA4 SESSIONS, VISITS & PURCHASES DEPENDENCY LINEAGE AUDIT")
    print("=" * 115)

    reports = [
        {
            "object": "rpt_visit_source_daily",
            "type": "TABLE",
            "published_report": "Traffic & Landings Source Report (vw_rpt_visit_source_daily)",
            "columns": "landings, page_views, consent, channel, source, medium, ga4_complete",
            "dependency": "GA4 raw events (session_start, page_view) from analytics_354198910",
            "affected_dates": "2026-10-24 to 2026-10-31 (unassigned surged 77-88%, consent dropped to 10-17%, 31 Oct has 0 landings / incomplete)"
        },
        {
            "object": "rpt_paid_media_page_daily",
            "type": "TABLE",
            "published_report": "Paid Media Reporting Page & CVR / CPA Dashboards (vw_rpt_paid_media_page_daily)",
            "columns": "wh_orders, wh_rev, new_cust, new_subs, ltv3m (and derived CVR = wh_orders/clicks)",
            "dependency": "GA4 landing touch attribution (fact_transaction_touch joined on UTM touch URL)",
            "affected_dates": "2026-10-24 to 2026-10-31 (unassigned landing surge and missing consent degrade web touch capture)"
        },
        {
            "object": "rpt_paid_media_page_status",
            "type": "TABLE",
            "published_report": "Paid Media Reporting Page Status Banner (vw_rpt_paid_media_page_status)",
            "columns": "consent_setting_changed_on, change_dates, built_at, wh_to",
            "dependency": "Status header tracking consent setting change (2026-10-24) and max data freshness",
            "affected_dates": "2026-10-24 to 2026-10-31 (reflects active consent change period)"
        },
        {
            "object": "rpt_organic_social_weekly",
            "type": "TABLE",
            "published_report": "Organic Social Reporting Suite",
            "columns": "site_sessions, site_purchases, site_revenue, site_new_customers, site_sessions_unclear, site_purchases_unclear",
            "dependency": "GA4 session starts and UTM/referrer session landing attribution from rpt_visit_source_daily",
            "affected_dates": "2026-10-19 to 2026-11-01 (weeks 2026-10-19 partial, 2026-10-26 full week degraded by unassigned traffic)"
        },
        {
            "object": "fact_ga4_purchases",
            "type": "TABLE",
            "published_report": "GA4 Purchase Parity & Reconciliation (vw_fact_ga4_purchases)",
            "columns": "event_date, user_pseudo_id, ga_session_id, session_source, session_medium, session_campaign, bt_events_that_day",
            "dependency": "GA4 event 'BoughtTickets' from analytics_354198910",
            "affected_dates": "2026-10-01 to 2026-10-31 (October parity captured only 65.5% vs web checkouts; 31 Oct export has 0 events)"
        },
        {
            "object": "rpt_exec_summary",
            "type": "TABLE",
            "published_report": "Executive Dashboard Snapshot",
            "columns": "value, note for kpi_ids: org_clearly_sessions_latest_week, org_unclear_sessions_latest_week, paid_tracking_coverage",
            "dependency": "GA4 sessions via rpt_organic_social_weekly and tracking coverage from rpt_tracking_coverage_daily",
            "affected_dates": "2026-10-24 to 2026-10-31 (session counts and site CVR understated due to unassigned traffic and denied consent)"
        }
    ]

    print(f"{'Table / Object':<28} | {'Type':<6} | {'Affected Columns':<45} | {'Affected Dates Since 24 Oct'}")
    print("-" * 125)
    for r in reports:
        print(f"{r['object']:<28} | {r['type']:<6} | {r['columns'][:43]:<45} | {r['affected_dates'][:55]}")
        print(f"  -> Report:     {r['published_report']}")
        print(f"  -> Dependency: {r['dependency']}")
        print()

    # Verify existing row counts in BigQuery
    token = get_token()
    client = bigquery.Client(project=PROJECT, location=LOCATION, credentials=Credentials(token))
    
    print("=" * 115)
    print("CURRENT BIGQUERY ROW COUNTS & DATE RANGES")
    print("=" * 115)
    for r in reports:
        tbl = r['object']
        try:
            date_col = 'd'
            if tbl == 'rpt_organic_social_weekly':
                date_col = 'week_commencing'
            elif tbl == 'fact_ga4_purchases':
                date_col = 'event_date'
            elif tbl == 'rpt_exec_summary':
                date_col = 'as_of_date'
            elif tbl == 'rpt_paid_media_page_status':
                date_col = 'built_at'

            q = f"SELECT COUNT(*) as cnt, MIN({date_col}) as min_d, MAX({date_col}) as max_d FROM `rh-ga4-491819.rh_curated_Bright_Test.{tbl}`"
            res = list(client.query(q).result())[0]
            print(f"{tbl:<28}: {res.cnt:>7,} rows | Date Range: {str(res.min_d)[:10]} to {str(res.max_d)[:10]}")
        except Exception as e:
            print(f"{tbl:<28}: Error checking dates: {e}")

if __name__ == "__main__":
    main()
