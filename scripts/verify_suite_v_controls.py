import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

# Set UTF-8 output encoding
sys.stdout.reconfigure(encoding='utf-8')

PROJECT_ID = "rh-ga4-491819"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_client():
    token = subprocess.check_output(
        f"gcloud auth print-access-token --impersonate-service-account={SA}",
        shell=True, text=True
    ).strip()
    creds = Credentials(token)
    return bigquery.Client(project=PROJECT_ID, location="europe-west2", credentials=creds)

def main():
    client = get_client()

    # 1. Weekly Controls Query (Before vs After)
    weekly_sql = """
    WITH weeks AS (
      SELECT DATE('2026-09-28') AS wk_start, '2026-09-28' AS wk_label UNION ALL
      SELECT DATE('2026-10-05'), '2026-10-05' UNION ALL
      SELECT DATE('2026-10-12'), '2026-10-12' UNION ALL
      SELECT DATE('2026-10-19'), '2026-10-19' UNION ALL
      SELECT DATE('2026-10-26'), '2026-10-26'
    ),
    summary_before AS (
      SELECT
        w.wk_label,
        b.basis,
        SUM(IF(b.entity_id = 'ps', b.wh_rev, 0)) AS ps_wh_rev,
        SUM(IF(b.entity_id = 'ps', b.wh_n, 0)) AS ps_wh_n,
        SUM(IF(b.entity_id = 'ps', b.no_n + b.ns_n, 0)) AS ps_new_cust,
        SUM(IF(b.entity_id = 'ps-unk', b.wh_rev, 0)) AS ps_unk_wh_rev,
        SUM(IF(b.entity_id = 'gb', b.wh_rev, 0)) AS gb_wh_rev
      FROM weeks w
      JOIN `rh-ga4-491819.rh_backup_Bright_Test.rpt_paid_media_entity_daily_backup_20261103` b
        ON b.d BETWEEN w.wk_start AND DATE_ADD(w.wk_start, INTERVAL 6 DAY)
      GROUP BY 1, 2
    ),
    summary_after AS (
      SELECT
        w.wk_label,
        a.basis,
        SUM(IF(a.entity_id = 'ps', a.wh_rev, 0)) AS ps_wh_rev,
        SUM(IF(a.entity_id = 'ps', a.wh_n, 0)) AS ps_wh_n,
        SUM(IF(a.entity_id = 'ps', a.no_n + a.ns_n, 0)) AS ps_new_cust,
        SUM(IF(a.entity_id = 'ps-unk', a.wh_rev, 0)) AS ps_unk_wh_rev,
        SUM(IF(a.entity_id = 'gb', a.wh_rev, 0)) AS gb_wh_rev
      FROM weeks w
      JOIN `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily` a
        ON a.d BETWEEN w.wk_start AND DATE_ADD(w.wk_start, INTERVAL 6 DAY)
      GROUP BY 1, 2
    )
    SELECT
      b.wk_label,
      b.basis,
      b.ps_wh_rev AS ps_wh_rev_bef,
      a.ps_wh_rev AS ps_wh_rev_aft,
      b.ps_wh_n AS ps_wh_n_bef,
      a.ps_wh_n AS ps_wh_n_aft,
      b.ps_new_cust AS ps_new_cust_bef,
      a.ps_new_cust AS ps_new_cust_aft,
      b.gb_wh_rev AS gb_wh_rev_bef,
      a.gb_wh_rev AS gb_wh_rev_aft,
      b.ps_unk_wh_rev AS ps_unk_bef,
      ROUND(SAFE_DIVIDE(b.ps_unk_wh_rev, b.ps_wh_rev) * 100, 2) AS ps_unk_share_bef_pct,
      a.ps_unk_wh_rev AS ps_unk_aft,
      ROUND(SAFE_DIVIDE(a.ps_unk_wh_rev, a.ps_wh_rev) * 100, 2) AS ps_unk_share_aft_pct
    FROM summary_before b
    JOIN summary_after a
      ON b.wk_label = a.wk_label AND b.basis = a.basis
    ORDER BY b.basis, b.wk_label
    """

    job_config = bigquery.QueryJobConfig(dry_run=True)
    d_job = client.query(weekly_sql, job_config=job_config)
    print(f"Weekly controls dry run: {d_job.total_bytes_processed:,} bytes")
    
    rows = list(client.query(weekly_sql).result())
    print("\n=== WEEKLY CONTROLS: BEFORE VS AFTER ===")
    print(f"{'Basis':<5} | {'Week':<10} | {'Meta Rev Bef':<12} | {'Meta Rev Aft':<12} | {'Diff Rev':<8} | {'N Bef':<6} | {'N Aft':<6} | {'New Bef':<7} | {'New Aft':<7} | {'GB Rev Bef':<10} | {'GB Rev Aft':<10} | {'Unk Bef %':<10} | {'Unk Aft %':<10}")
    print("-" * 135)
    for r in rows:
        diff_rev = r.ps_wh_rev_aft - r.ps_wh_rev_bef
        print(f"{r.basis:<5} | {r.wk_label:<10} | £{r.ps_wh_rev_bef:>10.2f} | £{r.ps_wh_rev_aft:>10.2f} | £{diff_rev:>6.2f} | {r.ps_wh_n_bef:>5} | {r.ps_wh_n_aft:>5} | {r.ps_new_cust_bef:>6} | {r.ps_new_cust_aft:>6} | £{r.gb_wh_rev_bef:>8.2f} | £{r.gb_wh_rev_aft:>8.2f} | {r.ps_unk_share_bef_pct:>8.2f}% | {r.ps_unk_share_aft_pct:>8.2f}%")

    # 2. Per-Campaign Details w/c 26 Oct (Basis L)
    camp_sql = """
    WITH camp_before AS (
      SELECT
        entity_id,
        basis,
        ROUND(SUM(spend), 2) AS spend,
        ROUND(SUM(wh_rev), 2) AS wh_rev,
        ROUND(SAFE_DIVIDE(SUM(wh_rev), SUM(spend)), 2) AS roas
      FROM `rh-ga4-491819.rh_backup_Bright_Test.rpt_paid_media_entity_daily_backup_20261103`
      WHERE d BETWEEN DATE '2026-10-26' AND DATE '2026-11-01'
        AND entity_id LIKE 'm%'
      GROUP BY 1, 2
    ),
    camp_after AS (
      SELECT
        entity_id,
        basis,
        ROUND(SUM(spend), 2) AS spend,
        ROUND(SUM(wh_rev), 2) AS wh_rev,
        ROUND(SAFE_DIVIDE(SUM(wh_rev), SUM(spend)), 2) AS roas
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
      WHERE d BETWEEN DATE '2026-10-26' AND DATE '2026-11-01'
        AND entity_id LIKE 'm%'
      GROUP BY 1, 2
    )
    SELECT
      COALESCE(a.entity_id, b.entity_id) AS entity_id,
      dim.name AS campaign_name,
      COALESCE(a.basis, b.basis) AS basis,
      COALESCE(a.spend, b.spend, 0) AS spend,
      COALESCE(b.wh_rev, 0) AS wh_rev_bef,
      COALESCE(b.roas, 0) AS roas_bef,
      COALESCE(a.wh_rev, 0) AS wh_rev_aft,
      COALESCE(a.roas, 0) AS roas_aft
    FROM camp_after a
    FULL OUTER JOIN camp_before b
      ON a.entity_id = b.entity_id AND a.basis = b.basis
    LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity` dim
      ON COALESCE(a.entity_id, b.entity_id) = dim.entity_id
    WHERE COALESCE(a.basis, b.basis) = 'L'
    ORDER BY spend DESC
    """
    d_job2 = client.query(camp_sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    print(f"\nCampaign details dry run: {d_job2.total_bytes_processed:,} bytes")
    camp_rows = list(client.query(camp_sql).result())
    print("\n=== PER META CAMPAIGN DETAILS w/c 26 Oct (Basis L) ===")
    print(f"{'Entity ID':<20} | {'Campaign Name':<45} | {'Spend':<10} | {'Rev Bef':<10} | {'ROAS Bef':<8} | {'Rev Aft':<10} | {'ROAS Aft':<8}")
    print("-" * 125)
    for r in camp_rows:
        cname = (r.campaign_name[:42] + '...') if len(r.campaign_name) > 45 else r.campaign_name
        print(f"{r.entity_id:<20} | {cname:<45} | £{r.spend:>8.2f} | £{r.wh_rev_bef:>8.2f} | {r.roas_bef:>6.2f}x | £{r.wh_rev_aft:>8.2f} | {r.roas_aft:>6.2f}x")

    # 3. Check remaining unknown in w/c 26 Oct
    unk_sql = """
    SELECT
      NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '') AS utm_camp,
      COUNT(*) AS order_cnt,
      ROUND(SUM(t.revenue_gbp), 2) AS rev_gbp
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
    JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
      ON t.transaction_id = touch.transaction_id
    WHERE touch.last_touch_timestamp IS NOT NULL
      AND DATE(touch.last_touch_timestamp, 'Europe/London') BETWEEN DATE '2026-10-26' AND DATE '2026-11-01'
      AND DATE_DIFF(DATE(t.purchase_at, 'Europe/London'), DATE(touch.last_touch_timestamp, 'Europe/London'), DAY) BETWEEN 0 AND 7
      AND REGEXP_CONTAINS(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'utm_source=(meta|fb|ig|facebook|instagram|meta_fb|meta_ig|an|th)|tw_source=(fb|ig)|fbclid')
      AND NOT EXISTS (
        SELECT 1 FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity` dim
        WHERE dim.kind = 'campaign'
          AND (
            SUBSTR(dim.entity_id, 2) = NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&](?:utm_campaign|tw_campaign)=([^&#]*)'), '')
            OR LOWER(TRIM(dim.name)) = LOWER(TRIM(NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '')))
          )
      )
      AND NOT (
        REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), ''), r'(?i)\\bpr\\b|_pr|pr_|\\brmg\\b|_rmg|rmg_|\\brtg\\b|_rtg|rtg_')
        OR REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]tw_campaign=([^&#]*)'), ''), r'(?i)\\bpr\\b|_pr|pr_|\\brmg\\b|_rmg|rmg_|\\brtg\\b|_rtg|rtg_')
      )
    GROUP BY 1
    ORDER BY rev_gbp DESC
    """
    d_job3 = client.query(unk_sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    print(f"\nRemaining unknown dry run: {d_job3.total_bytes_processed:,} bytes")
    unk_rows = list(client.query(unk_sql).result())
    print("\n=== REMAINING UNKNOWN IN META (w/c 26 Oct) ===")
    if not unk_rows:
        print("None! All Meta touches matched a campaign or group.")
    else:
        for u in unk_rows:
            print(f"utm_campaign: {u.utm_camp} | Orders: {u.order_cnt} | Rev: £{u.rev_gbp:.2f}")

    # 4. Check meta_unknown_revenue_share DQ test query directly
    dq_test_sql = """
    WITH meta_unk_quality AS (
      SELECT
        COUNTIF(unk_share_pct > 10.0) AS days_over_10pct,
        ROUND(MAX(unk_share_pct), 2) AS max_unk_share_pct,
        STRING_AGG(IF(unk_share_pct > 10.0, CONCAT(CAST(d AS STRING), ' (', basis, ': ', CAST(unk_share_pct AS STRING), '%)'), NULL), ', ') AS bad_days
      FROM (
        SELECT
          d,
          basis,
          ROUND(SAFE_DIVIDE(SUM(IF(entity_id = 'ps-unk', wh_rev, 0)), SUM(IF(entity_id = 'ps', wh_rev, 0))) * 100, 2) AS unk_share_pct
        FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
        WHERE d BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)
                    AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
          AND basis IN ('F', 'L')
        GROUP BY 1, 2
        HAVING SUM(IF(entity_id = 'ps', wh_rev, 0)) > 0
      )
    ),
    meta_unk_top_unmatched AS (
      SELECT
        COALESCE(
          STRING_AGG(CONCAT(IFNULL(utm_camp, '(no_utm_campaign)'), ' (£', CAST(rev AS STRING), ')'), ', ' ORDER BY rev DESC LIMIT 5),
          'None'
        ) AS top_unmatched
      FROM (
        SELECT
          NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '') AS utm_camp,
          ROUND(SUM(t.revenue_gbp), 2) AS rev
        FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
        JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
          ON t.transaction_id = touch.transaction_id
        WHERE touch.last_touch_timestamp IS NOT NULL
          AND DATE(touch.last_touch_timestamp, 'Europe/London') BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 7 DAY)
                                                                   AND DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
          AND DATE_DIFF(DATE(t.purchase_at, 'Europe/London'), DATE(touch.last_touch_timestamp, 'Europe/London'), DAY) BETWEEN 0 AND 7
          AND REGEXP_CONTAINS(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'utm_source=(meta|fb|ig|facebook|instagram|meta_fb|meta_ig|an|th)|tw_source=(fb|ig)|fbclid')
          AND NOT EXISTS (
            SELECT 1 FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_paid_media_entity` dim
            WHERE dim.kind = 'campaign'
              AND (
                SUBSTR(dim.entity_id, 2) = NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&](?:utm_campaign|tw_campaign)=([^&#]*)'), '')
                OR LOWER(TRIM(dim.name)) = LOWER(TRIM(NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '')))
              )
          )
          AND NOT (
            REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), ''), r'(?i)\\bpr\\b|_pr|pr_|\\brmg\\b|_rmg|rmg_|\\brtg\\b|_rtg|rtg_')
            OR REGEXP_CONTAINS(COALESCE(REGEXP_EXTRACT(LOWER(REPLACE(touch.last_touch_url, '%3F', '?')), r'[?&]tw_campaign=([^&#]*)'), ''), r'(?i)\\bpr\\b|_pr|pr_|\\brmg\\b|_rmg|rmg_|\\brtg\\b|_rtg|rtg_')
          )
        GROUP BY 1
      )
    )
    SELECT
      'meta_unknown_revenue_share' AS test_name,
      'WARN' AS severity,
      IF(days_over_10pct = 0, 'PASS', 'WARN') AS result,
      10.0 AS threshold,
      max_unk_share_pct AS actual_value,
      IF(days_over_10pct = 0,
         CONCAT('ps-unk share <= 10% for all of last 7 days (max ', CAST(max_unk_share_pct AS STRING), '%). Top unmatched: ', top_unmatched),
         CONCAT('Days in last 7 with ps-unk > 10%: ', bad_days, '. Top unmatched: ', top_unmatched)
      ) AS detail
    FROM meta_unk_quality CROSS JOIN meta_unk_top_unmatched
    """
    dq_row = list(client.query(dq_test_sql).result())[0]
    print(f"\n=== DQ TEST: meta_unknown_revenue_share ===")
    print(f"Test: {dq_row.test_name} | Severity: {dq_row.severity} | Result: {dq_row.result} | Threshold: {dq_row.threshold} | Actual: {dq_row.actual_value}")
    print(f"Detail: {dq_row.detail}")

if __name__ == "__main__":
    main()
