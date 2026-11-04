import os, sys, subprocess
from decimal import Decimal
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

sys.stdout.reconfigure(encoding='utf-8')

SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
GCP_PROJECT = "rh-ga4-491819"

def get_client():
    token = subprocess.check_output(
        f"gcloud.cmd auth print-access-token --impersonate-service-account={SA}",
        shell=True, text=True
    ).strip()
    creds = Credentials(token)
    return bigquery.Client(project=GCP_PROJECT, location="europe-west2", credentials=creds)

def main():
    client = get_client()

    print("=== RECONCILIATION 1: DAILY SPEND SINCE 1 SEP ===")
    print("Comparing SUM(spend_gbp) in stg_meta_segment_daily vs SUM(spend) in rpt_paid_media_entity_daily (entity_id='ps', basis='L')...")
    
    daily_sql = """
    WITH seg AS (
      SELECT
        spend_date AS d,
        ROUND(SUM(spend_gbp), 2) AS seg_spend
      FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
      WHERE spend_date >= '2026-09-01'
      GROUP BY 1
    ),
    rpt AS (
      SELECT
        d,
        ROUND(SUM(spend), 2) AS rpt_spend
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
      WHERE d >= '2026-09-01' AND entity_id = 'ps' AND basis = 'L'
      GROUP BY 1
    )
    SELECT
      COALESCE(s.d, r.d) AS d,
      COALESCE(s.seg_spend, 0) AS seg_spend,
      COALESCE(r.rpt_spend, 0) AS rpt_spend,
      ROUND(COALESCE(s.seg_spend, 0) - COALESCE(r.rpt_spend, 0), 2) AS diff_gbp,
      ROUND(SAFE_DIVIDE(ABS(COALESCE(s.seg_spend, 0) - COALESCE(r.rpt_spend, 0)), r.rpt_spend) * 100, 3) AS diff_pct
    FROM seg s
    FULL OUTER JOIN rpt r ON s.d = r.d
    ORDER BY d
    """
    
    d_job1 = client.query(daily_sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    print(f"Daily query dry run: {d_job1.total_bytes_processed:,} bytes")
    daily_rows = list(client.query(daily_sql).result())
    
    gaps_over_1 = [r for r in daily_rows if abs(r.diff_gbp) > 1.0]
    print(f"Total days analyzed: {len(daily_rows)}")
    print(f"Days where the gap is over £1: {len(gaps_over_1)}")
    if gaps_over_1:
        print("\nDays with gap > £1:")
        print(f"{'Date':<12} | {'Segment Spend':<14} | {'Report Spend':<14} | {'Diff (£)':<10} | {'Diff (%)'}")
        print("-" * 65)
        for g in gaps_over_1:
            diff_pct_str = f"{g.diff_pct:>6.2f}%" if g.diff_pct is not None else "   N/A "
            print(f"{str(g.d):<12} | £{g.seg_spend:>12.2f} | £{g.rpt_spend:>12.2f} | £{g.diff_gbp:>8.2f} | {diff_pct_str}")
    else:
        print("None! Every day since 1 Sep matches within £1.00.")

    print("\nSample of daily matches (first 5 and last 5):")
    print(f"{'Date':<12} | {'Segment Spend':<14} | {'Report Spend':<14} | {'Diff (£)':<10} | {'Diff (%)'}")
    print("-" * 65)
    for r in daily_rows[:5] + daily_rows[-5:]:
        diff_pct_str = f"{r.diff_pct:>6.2f}%" if r.diff_pct is not None else "   N/A "
        print(f"{str(r.d):<12} | £{r.seg_spend:>12.2f} | £{r.rpt_spend:>12.2f} | £{r.diff_gbp:>8.2f} | {diff_pct_str}")

    print("\n=== RECONCILIATION 2: PER CAMPAIGN FOR 26 OCT TO 1 NOV ===")
    camp_sql = """
    WITH seg_camp AS (
      SELECT
        campaign_id,
        MAX(campaign_name) AS campaign_name,
        ROUND(SUM(spend_gbp), 2) AS seg_spend
      FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
      WHERE spend_date BETWEEN '2026-10-26' AND '2026-11-01'
      GROUP BY 1
    ),
    rpt_camp AS (
      SELECT
        SUBSTR(entity_id, 2) AS campaign_id,
        ROUND(SUM(spend), 2) AS rpt_spend
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
      WHERE d BETWEEN '2026-10-26' AND '2026-11-01'
        AND entity_id LIKE 'm%'
        AND basis = 'L'
      GROUP BY 1
    )
    SELECT
      COALESCE(r.campaign_id, s.campaign_id) AS campaign_id,
      dim.name AS dim_name,
      s.campaign_name AS seg_camp_name,
      COALESCE(r.rpt_spend, 0) AS rpt_spend,
      COALESCE(s.seg_spend, 0) AS seg_spend,
      ROUND(COALESCE(s.seg_spend, 0) - COALESCE(r.rpt_spend, 0), 2) AS diff_gbp
    FROM rpt_camp r
    FULL OUTER JOIN seg_camp s ON r.campaign_id = s.campaign_id
    LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity` dim
      ON CONCAT('m', COALESCE(r.campaign_id, s.campaign_id)) = dim.entity_id
    ORDER BY rpt_spend DESC
    """
    d_job2 = client.query(camp_sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    print(f"Campaign query dry run: {d_job2.total_bytes_processed:,} bytes")
    camp_rows = list(client.query(camp_sql).result())
    
    print(f"{'Campaign ID':<20} | {'Campaign Name':<45} | {'Report Spend':<13} | {'Segment Spend':<13} | {'Diff (£)'}")
    print("-" * 110)
    no_seg_camps = []
    for c in camp_rows:
        cname = c.dim_name or c.seg_camp_name or "Unknown"
        cname_disp = (cname[:42] + "...") if len(cname) > 45 else cname
        print(f"{c.campaign_id:<20} | {cname_disp:<45} | £{c.rpt_spend:>11.2f} | £{c.seg_spend:>11.2f} | £{c.diff_gbp:>7.2f}")
        if c.seg_spend == 0:
            no_seg_camps.append(c)
            
    print(f"\nCampaigns returning NO segments: {len(no_seg_camps)}")
    if no_seg_camps:
        for nc in no_seg_camps:
            print(f"  - Campaign {nc.campaign_id} ({nc.dim_name}): Report spend = £{nc.rpt_spend:.2f}")
    else:
        print("  None. All active campaigns returned segments.")

    print("\n=== RECONCILIATION 3: 26 OCT TO 1 NOV ACCOUNT TOTALS BY SEGMENT ===")
    seg_tot_sql = """
    SELECT
      user_segment_key,
      ROUND(SUM(spend_gbp), 2) AS total_spend_gbp,
      SUM(impressions) AS total_impressions,
      SUM(link_clicks) AS total_link_clicks,
      ROUND(SUM(purchases), 2) AS total_purchases,
      ROUND(SUM(purchase_value_gbp), 2) AS total_purchase_value_gbp
    FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_segment_daily`
    WHERE spend_date BETWEEN '2026-10-26' AND '2026-11-01'
    GROUP BY 1
    ORDER BY total_spend_gbp DESC
    """
    d_job3 = client.query(seg_tot_sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    print(f"Segment totals dry run: {d_job3.total_bytes_processed:,} bytes")
    seg_rows = list(client.query(seg_tot_sql).result())
    
    print(f"{'Segment':<15} | {'Spend (£)':<12} | {'Target (£)':<12} | {'Diff (£)':<10} | {'Impressions':<12} | {'Link Clicks':<12} | {'Purchases':<10} | {'Value (£)'}")
    print("-" * 105)
    targets = {
        "prospecting": Decimal("27259.40"),
        "existing": Decimal("17590.61"),
        "engaged": Decimal("6505.83"),
        "unknown": Decimal("5.01")
    }
    tot_sp = Decimal("0.00")
    tot_target = sum(targets.values())
    for s in seg_rows:
        tgt = targets.get(s.user_segment_key, Decimal("0.00"))
        diff = s.total_spend_gbp - tgt
        tot_sp += s.total_spend_gbp
        print(f"{s.user_segment_key:<15} | £{s.total_spend_gbp:>10.2f} | £{tgt:>10.2f} | £{diff:>8.2f} | {s.total_impressions:>11,} | {s.total_link_clicks:>11,} | {s.total_purchases:>9.2f} | £{s.total_purchase_value_gbp:>10.2f}")
    print("-" * 105)
    print(f"{'TOTAL':<15} | £{tot_sp:>10.2f} | £{tot_target:>10.2f} | £{tot_sp - tot_target:>8.2f}")

if __name__ == "__main__":
    main()
