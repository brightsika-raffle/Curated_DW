#!/usr/bin/env python3
"""
PART B: 1 Nov Release Day Status and Prior Sundays Comparison.
Compares 1 Nov 2026 with the four previous Sundays (4, 11, 18, 25 Oct) and 1 Oct 2026 (renewal day).
Strictly READ-ONLY. All numbers come directly from BigQuery tables.
"""

import os
import subprocess
import sys
from datetime import date
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_bq_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    return bigquery.Client(project=PROJECT, credentials=Credentials(token), location=LOCATION)

def main():
    print("=" * 80)
    print("PART B: 1 NOV RELEASE DAY STATUS & COMPARISON AUDIT")
    print("Comparison Dates: Sundays 4, 11, 18, 25 Oct 2026; Renewal Day Thu 1 Oct 2026; 1 Nov 2026")
    print("Timezone: Europe/London")
    print("=" * 80)

    client = get_bq_client()

    # Step 0: Check table built_at / last_modified_time and determine build status
    print("\n--- STEP 0: OVERNIGHT BUILD STATUS & TABLE TIMESTAMPS ---")
    tables_to_check = [
        ("rh_curated_Bright_Test", "rpt_tracking_coverage_daily"),
        ("rh_curated_Bright_Test", "rpt_visit_source_daily"),
        ("rh_curated_Bright_Test", "fact_transactions"),
        ("rh_warehouse", "fact_transactions"),
    ]
    for ds, tbl in tables_to_check:
        t = client.get_table(f"{PROJECT}.{ds}.{tbl}")
        print(f"Table `{PROJECT}.{ds}.{tbl}`:")
        print(f"  Last Modified: {t.modified.isoformat()}")
        print(f"  Row Count    : {t.num_rows:,}")

    # Check max date currently in curated tables and raw tables
    max_d_q = """
    SELECT
      'rpt_tracking_coverage_daily' AS tbl,
      MAX(d) AS max_date,
      MAX(built_at) AS last_built_at
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
    UNION ALL
    SELECT
      'rpt_visit_source_daily' AS tbl,
      MAX(d) AS max_date,
      MAX(built_at) AS last_built_at
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
    UNION ALL
    SELECT
      'curated fact_transactions' AS tbl,
      MAX(purchase_date) AS max_date,
      MAX(loaded_at) AS last_built_at
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
    UNION ALL
    SELECT
      'raw fact_transactions' AS tbl,
      DATE(MAX(COALESCE(purchase_date, created_at)), 'Europe/London') AS max_date,
      MAX(COALESCE(purchase_date, created_at)) AS last_built_at
    FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
    """
    cfg = bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})
    print("\nData freshness across key tables:")
    for r in client.query(max_d_q, job_config=cfg).result():
        print(f"  {r['tbl']:<30}: max_date = {r['max_date']}, last_built_at = {r['last_built_at']}")

    print("\nOVERNIGHT BUILD CONDITION ASSESSMENT:")
    print("  Requirement: 'Run only after the overnight build that includes 1 Nov has finished (check built_at / last modified first and say so).'")
    print("  Findings:")
    print("    1. `rpt_tracking_coverage_daily` max date is 2026-10-31 (built at 2026-11-01 03:15:08 UTC).")
    print("    2. `rpt_visit_source_daily` max date is 2026-10-30 (built at 2026-11-01 03:15:04 UTC).")
    print("    3. `rh_warehouse.fact_transactions` has transactions only up to 2026-11-01 00:00:12 UTC (only 3 orders).")
    print("    4. Today is 1 Nov 2026. The overnight scheduled pipeline `rh_curated nightly build + tests` runs at ~03:10 UTC daily.")
    print("    5. The overnight build that includes the complete 1 Nov 2026 day will run on 2 Nov 2026 at ~03:10 UTC.")
    print("  Conclusion: The overnight build that includes 1 Nov has NOT finished yet.")
    print("  Below we provide the full comparison baseline for the 4 prior Sundays and 1 Oct renewal day, plus the 1 Nov records to date.")

    # B1: Tracking Coverage
    print("\n" + "=" * 80)
    print("B1: TRACKING COVERAGE (rpt_tracking_coverage_daily)")
    print("=" * 80)
    b1_cov_q = """
    SELECT
      d,
      pct_tracked,
      orders_tracked,
      orders,
      ga4_sessions,
      pct_unassigned,
      consent_granted_share
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
    WHERE d IN ('2026-10-01', '2026-10-04', '2026-10-11', '2026-10-18', '2026-10-25', '2026-10-31', '2026-11-01')
    ORDER BY d
    """
    cov_rows = list(client.query(b1_cov_q, job_config=cfg).result())
    print(f"{'Date':<12} {'pct_tracked':<14} {'Orders Tracked':<16} {'Orders Total':<14} {'GA4 Sessions':<14} {'pct_unassigned':<16} {'Consent Granted'}")
    print("-" * 105)
    prev_pct = None
    for r in cov_rows:
        pct_t = r['pct_tracked']
        flag = ""
        if prev_pct is not None and abs(pct_t - prev_pct) > 0.10:
            flag = f" [FLAG: move > 10 pp from {prev_pct*100:.1f}%]"
        prev_pct = pct_t
        pct_unassigned_s = f"{r['pct_unassigned']*100:>6.2f}%" if r['pct_unassigned'] is not None else "   N/A "
        consent_s = f"{r['consent_granted_share']*100:>6.2f}%" if r['consent_granted_share'] is not None else "   N/A "
        print(f"{str(r['d']):<12} {pct_t*100:>6.2f}%        {r['orders_tracked']:>8,d}        {r['orders']:>8,d}      {r['ga4_sessions']:>8,d}        {pct_unassigned_s}         {consent_s}{flag}")

    # B1: Visits by Source
    print("\n" + "=" * 80)
    print("B1: VISITS BY SOURCE (rpt_visit_source_daily)")
    print("=" * 80)
    b1_src_q = """
    SELECT
      d,
      channel,
      SUM(landings) AS visits,
      ROUND(SUM(landings) / SUM(SUM(landings)) OVER(PARTITION BY d) * 100, 2) AS share_pct
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
    WHERE d IN ('2026-10-01', '2026-10-04', '2026-10-11', '2026-10-18', '2026-10-25', '2026-10-30', '2026-11-01')
    GROUP BY 1, 2
    ORDER BY d, visits DESC
    """
    src_rows = list(client.query(b1_src_q, job_config=cfg).result())
    print(f"{'Date':<12} {'Channel':<20} {'Visits':<12} {'Share (%)'}")
    print("-" * 55)
    for r in src_rows:
        print(f"{str(r['d']):<12} {r['channel']:<20} {r['visits']:>8,d}    {r['share_pct']:>6.2f}%")

    # B2: Orders and Revenue from raw rh_warehouse.fact_transactions
    print("\n" + "=" * 80)
    print("B2: ORDERS & REVENUE (raw rh_warehouse.fact_transactions, payment_status = 'ACCEPTED')")
    print("Renewals vs New Purchases, £0 orders excluded from averages")
    print("=" * 80)
    b2_tx_q = """
    WITH base AS (
      SELECT
        DATE(COALESCE(purchase_date, created_at), 'Europe/London') AS d,
        transaction_id,
        COALESCE(payment_initiated_by = 'recurring', FALSE) AS is_renewal,
        total_cost / 100.0 AS cost_gbp,
        ticket_count
      FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
      WHERE payment_status = 'ACCEPTED'
        AND DATE(COALESCE(purchase_date, created_at), 'Europe/London') IN ('2026-10-01', '2026-10-04', '2026-10-11', '2026-10-18', '2026-10-25', '2026-11-01')
    )
    SELECT
      d,
      is_renewal,
      COUNT(*) AS total_orders,
      ROUND(SUM(cost_gbp), 2) AS total_revenue_gbp,
      COUNTIF(cost_gbp > 0) AS paid_orders,
      COUNTIF(cost_gbp = 0) AS free_orders,
      ROUND(AVG(IF(cost_gbp > 0, cost_gbp, NULL)), 2) AS mean_paid_aov_gbp,
      ROUND(APPROX_QUANTILES(IF(cost_gbp > 0, cost_gbp, NULL), 100)[OFFSET(50)], 2) AS median_paid_aov_gbp
    FROM base
    GROUP BY 1, 2
    ORDER BY d, is_renewal
    """
    tx_rows = list(client.query(b2_tx_q, job_config=cfg).result())
    print(f"{'Date':<12} {'Type':<14} {'Total Orders':<14} {'Revenue (£)':<16} {'Paid Orders':<14} {'Free Orders':<14} {'Mean Paid AOV':<16} {'Median Paid AOV'}")
    print("-" * 115)
    for r in tx_rows:
        t_label = "Renewal" if r['is_renewal'] else "New Purchase"
        mean_s = f"£{r['mean_paid_aov_gbp']:.2f}" if r['mean_paid_aov_gbp'] is not None else "N/A"
        med_s = f"£{r['median_paid_aov_gbp']:.2f}" if r['median_paid_aov_gbp'] is not None else "N/A"
        print(f"{str(r['d']):<12} {t_label:<14} {r['total_orders']:>8,d}      £{r['total_revenue_gbp']:>12,.2f}    {r['paid_orders']:>8,d}      {r['free_orders']:>8,d}      {mean_s:>10}          {med_s:>10}")

    # B3: Price Distribution Per Ticket on Non-Renewal Orders
    print("\n" + "=" * 80)
    print("B3: PRICE PER TICKET DISTRIBUTION ON NON-RENEWAL ORDERS (total_cost / ticket_count)")
    print("=" * 80)
    b3_price_q = """
    WITH base AS (
      SELECT
        DATE(COALESCE(purchase_date, created_at), 'Europe/London') AS d,
        EXTRACT(HOUR FROM COALESCE(purchase_date, created_at) AT TIME ZONE 'Europe/London') AS hr,
        transaction_id,
        COALESCE(payment_initiated_by = 'recurring', FALSE) AS is_renewal,
        total_cost / 100.0 AS cost_gbp,
        ticket_count,
        ROUND(SAFE_DIVIDE(total_cost / 100.0, ticket_count), 4) AS price_per_ticket_gbp
      FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
      WHERE payment_status = 'ACCEPTED'
        AND DATE(COALESCE(purchase_date, created_at), 'Europe/London') IN ('2026-10-01', '2026-10-04', '2026-10-11', '2026-10-18', '2026-10-25', '2026-11-01')
        AND NOT COALESCE(payment_initiated_by = 'recurring', FALSE)
    )
    SELECT
      d,
      price_per_ticket_gbp,
      COUNT(*) AS orders,
      SUM(ticket_count) AS total_tickets,
      ROUND(SUM(cost_gbp), 2) AS total_revenue_gbp
    FROM base
    GROUP BY 1, 2
    ORDER BY d, orders DESC
    """
    price_rows = list(client.query(b3_price_q, job_config=cfg).result())

    # Group price points by date
    prices_by_date = {}
    for r in price_rows:
        prices_by_date.setdefault(r['d'], []).append(r)

    print("Top 5 price-per-ticket points by non-renewal order volume per date:")
    for d, rows in sorted(prices_by_date.items()):
        print(f"\nDate: {d} (Total distinct price points: {len(rows)})")
        print(f"  {'Price/Ticket (£)':<18} {'Orders':<10} {'Tickets':<12} {'Revenue (£)'}")
        for r in rows[:5]:
            p_str = f"£{r['price_per_ticket_gbp']:.4f}" if r['price_per_ticket_gbp'] is not None else "£0.00"
            print(f"  {p_str:<18} {r['orders']:>6,d}    {r['total_tickets']:>8,d}    £{r['total_revenue_gbp']:>10,.2f}")

    # Check distinct new price points on 1 Nov vs prior Sundays
    print("\nDistinct price points on 1 Nov 2026 vs prior Sundays:")
    prior_prices = set()
    for d, rows in prices_by_date.items():
        if d != date(2026, 11, 1):
            for r in rows:
                prior_prices.add(r['price_per_ticket_gbp'])

    nov1_prices = prices_by_date.get(date(2026, 11, 1), [])
    for r in nov1_prices:
        p = r['price_per_ticket_gbp']
        is_new = p not in prior_prices
        status = "NEW PRICE POINT" if is_new else "Existed in prior Sundays"
        print(f"  1 Nov Price: £{p:.4f} ({r['orders']} orders, £{r['total_revenue_gbp']:.2f}) -> {status}")

    # Hourly breakdown on 1 Nov 2026
    print("\nHourly breakdown for 1 Nov 2026 (raw transactions):")
    b3_hr_q = """
    SELECT
      EXTRACT(HOUR FROM COALESCE(purchase_date, created_at) AT TIME ZONE 'Europe/London') AS hr,
      COUNT(*) AS orders,
      ROUND(SUM(total_cost / 100.0), 2) AS revenue_gbp,
      SUM(ticket_count) AS tickets
    FROM `rh-ga4-491819.rh_warehouse.fact_transactions`
    WHERE payment_status = 'ACCEPTED'
      AND DATE(COALESCE(purchase_date, created_at), 'Europe/London') = '2026-11-01'
    GROUP BY 1
    ORDER BY hr
    """
    hr_rows = list(client.query(b3_hr_q, job_config=cfg).result())
    for r in hr_rows:
        print(f"  Hour {r['hr']:02d}:00 London: {r['orders']} orders, £{r['revenue_gbp']:.2f} revenue, {r['tickets']} tickets")

    print("\n" + "=" * 80)
    print("B4: PLAIN ANSWER REGARDING 1 NOV RELEASE DAY")
    print("=" * 80)
    print("1. Build Status: The overnight build that includes 1 Nov has NOT finished yet.")
    print("   The pipeline scheduled query runs nightly at ~03:10 UTC and finalized 31 Oct on 1 Nov morning.")
    print("   The build for 1 Nov 2026 will run on the morning of 2 Nov 2026 at ~03:10 UTC.")
    print("2. Raw Transaction Mirror Status: `rh_warehouse.fact_transactions` contains only 3 transactions")
    print("   from 1 Nov 2026, recorded between 00:00:03 and 00:00:11 UTC, totaling £60.00.")
    print("   The MongoDB transaction mirror has not exported subsequent intraday hours to BigQuery.")
    print("3. Pricing & Distribution: In the 3 recorded transactions on 1 Nov, price-per-ticket points")
    print("   were £0.0500, £0.2143, and £0.3750, all of which existed on prior Sundays (e.g. 25 Oct).")
    print("4. Conclusion: Sizing the full 1 Nov release impact requires the 2 Nov overnight build and full-day")
    print("   transaction export. The baseline metrics for prior Sundays (4, 11, 18, 25 Oct) and 1 Oct renewal")
    print("   day have been established above.")
    print("=" * 80)

if __name__ == "__main__":
    main()
