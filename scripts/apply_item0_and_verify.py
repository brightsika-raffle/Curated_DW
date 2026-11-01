"""
apply_item0_and_verify.py

Applies the Section 3 Item 0 rule change:
- Rebuilds rpt_tracking_coverage_daily so each day is judged on its own figures:
  unreliable when pct_unassigned > 0.20 or consent_granted_share < 0.50.
- Propagates ga4_reliable updates to dependent tables (rpt_visit_source_daily,
  fact_ga4_purchases, rpt_paid_media_page_daily, rpt_paid_media_page_status,
  rpt_organic_social_weekly, rpt_exec_summary).
- Updates sql/nightly_build_and_test.sql with the new rule, notes, and the
  ga4_reliable_from_break HARD test.
- Re-runs the DQ gate check and verifies all HARD tests PASS.
- Prints:
  1. 17-25 Oct from rpt_tracking_coverage_daily
  2. The Tracking health row of vw_report_suite_health
  3. The exec summary notes that mention the date
"""

import os, sys, subprocess
from google.cloud import bigquery
from google.oauth2.credentials import Credentials

PROJECT_ID = "rh-ga4-491819"
LOCATION = "europe-west2"
SERVICE_ACCOUNT = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_client():
    env = os.environ.copy()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = "datacloud.antigravity"
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SERVICE_ACCOUNT}"],
        capture_output=True, text=True, check=True, env=env
    )
    token = res.stdout.strip().splitlines()[-1].strip()
    creds = Credentials(token)
    return bigquery.Client(project=PROJECT_ID, credentials=creds, location=LOCATION)

def run_query(client, sql, description):
    print(f"\n--- {description} ---")
    job_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry = client.query(sql, job_config=job_cfg, location=LOCATION)
    bytes_scanned = dry.total_bytes_processed or 0
    mb_scanned = bytes_scanned / (1024 * 1024)
    gb_scanned = bytes_scanned / (1024 * 1024 * 1024)
    print(f"Dry-run: {bytes_scanned:,} bytes ({mb_scanned:.2f} MB / {gb_scanned:.4f} GB)")
    if gb_scanned > 15.0:
        raise ValueError(f"Aborting: scan of {gb_scanned:.2f} GB exceeds 15 GB limit!")
    
    exec_cfg = bigquery.QueryJobConfig(use_query_cache=False)
    job = client.query(sql, job_config=exec_cfg, location=LOCATION)
    job.result()
    print("Execution completed successfully.")
    return bytes_scanned

def main():
    client = get_client()

    # Step 1: Rebuild rpt_tracking_coverage_daily
    sql_rebuild_coverage = """
    CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
    CLUSTER BY d
    OPTIONS (description = 'Daily tracking coverage tracking orders with last-touch URL vs unassigned GA4 sessions and consent. Rebuilt nightly from 2026-09-01.')
    AS
    WITH dates AS (
      SELECT d
      FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2026-09-01', DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY))) AS d
    ),
    orders_daily AS (
      SELECT
        t.purchase_date AS d,
        COUNT(*) AS orders,
        COUNTIF(touch.last_touch_url IS NOT NULL AND TRIM(touch.last_touch_url) != '') AS orders_tracked
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      WHERE t.purchase_date >= DATE '2026-09-01'
        AND NOT IFNULL(t.is_free_entry, FALSE)
        AND touch.attribution_basis != 'inherited_from_subscription_signup'
      GROUP BY 1
    ),
    ga4_daily AS (
      SELECT
        date AS d,
        SUM(sessions) AS ga4_sessions,
        SUM(IF(ga4_channel_group = 'Unassigned', sessions, 0)) AS ga4_unassigned
      FROM `rh-ga4-491819.rh_curated_Bright_Test.ga4_source_daily`
      WHERE date >= DATE '2026-09-01'
      GROUP BY 1
    ),
    visit_sources AS (
      SELECT
        d,
        SUM(landings) AS visit_landings,
        SUM(page_views) AS total_pv,
        SUM(IF(consent = 'granted', landings, 0)) AS granted_landings,
        SUM(IF(consent = 'granted', page_views, 0)) AS granted_pv
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
      WHERE d >= DATE '2026-09-01'
      GROUP BY 1
    )
    SELECT
      dt.d,
      COALESCE(o.orders, 0) AS orders,
      COALESCE(o.orders_tracked, 0) AS orders_tracked,
      ROUND(SAFE_DIVIDE(COALESCE(o.orders_tracked, 0), o.orders), 4) AS pct_tracked,
      COALESCE(g.ga4_sessions, 0) AS ga4_sessions,
      COALESCE(g.ga4_unassigned, 0) AS ga4_unassigned,
      ROUND(SAFE_DIVIDE(COALESCE(g.ga4_unassigned, 0), g.ga4_sessions), 4) AS pct_unassigned,
      ROUND(SAFE_DIVIDE(v.granted_landings, v.visit_landings), 4) AS consent_granted_share,
      IF(dt.d < DATE '2026-10-19', CAST(ROUND(SAFE_DIVIDE(
        v.total_pv,
        SAFE_DIVIDE(v.granted_pv, v.granted_landings)
      )) AS INT64), NULL) AS est_visits,
      (
        COALESCE(g.ga4_sessions, 0) > 0
        AND SAFE_DIVIDE(COALESCE(g.ga4_unassigned, 0), g.ga4_sessions) <= 0.20
        AND (SAFE_DIVIDE(v.granted_landings, v.visit_landings) IS NULL OR SAFE_DIVIDE(v.granted_landings, v.visit_landings) >= 0.50)
      ) AS ga4_sessions_reliable,
      COALESCE(g.ga4_sessions, 0) > 0 AS ga4_complete,
      CURRENT_TIMESTAMP() AS built_at
    FROM dates dt
    LEFT JOIN orders_daily o ON dt.d = o.d
    LEFT JOIN ga4_daily g ON dt.d = g.d
    LEFT JOIN visit_sources v ON dt.d = v.d;
    """
    run_query(client, sql_rebuild_coverage, "Rebuild rpt_tracking_coverage_daily with item 0 rule")

    # Step 2: Update rpt_visit_source_daily
    sql_update_visit = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily` v
    SET ga4_reliable = tc.ga4_sessions_reliable
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
    WHERE v.d = tc.d;
    """
    run_query(client, sql_update_visit, "Update rpt_visit_source_daily ga4_reliable")

    # Step 3: Update fact_ga4_purchases
    sql_update_purch = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases` f
    SET ga4_reliable = tc.ga4_sessions_reliable
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
    WHERE f.event_date = tc.d;
    """
    run_query(client, sql_update_purch, "Update fact_ga4_purchases ga4_reliable")

    # Step 4: Update rpt_paid_media_page_daily
    sql_update_paid = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily` p
    SET ga4_reliable = tc.ga4_sessions_reliable
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
    WHERE p.d = tc.d;
    """
    run_query(client, sql_update_paid, "Update rpt_paid_media_page_daily ga4_reliable")

    # Step 5: Update rpt_paid_media_page_status
    sql_update_paid_status = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`
    SET ga4_reliable = (
      SELECT ga4_sessions_reliable
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
      WHERE d = DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 1 DAY)
    )
    WHERE TRUE;
    """
    run_query(client, sql_update_paid_status, "Update rpt_paid_media_page_status ga4_reliable")

    # Step 6: Update rpt_organic_social_weekly
    sql_update_org = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly` w
    SET ga4_reliable = COALESCE((
      SELECT LOGICAL_AND(ga4_sessions_reliable)
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily` tc
      WHERE tc.d BETWEEN w.week_commencing AND DATE_ADD(w.week_commencing, INTERVAL 6 DAY)
    ), FALSE)
    WHERE TRUE;
    """
    run_query(client, sql_update_org, "Update rpt_organic_social_weekly ga4_reliable")

    # Step 7: Update rpt_exec_summary notes
    sql_update_exec_notes = """
    UPDATE `rh-ga4-491819.rh_curated_Bright_Test.rpt_exec_summary`
    SET note = 'GA4 sessions unreliable since 2026-10-19; site CVR understated'
    WHERE note = 'GA4 sessions unreliable since 2026-10-24; site CVR understated';
    """
    run_query(client, sql_update_exec_notes, "Update rpt_exec_summary notes to 2026-10-19")

    # Step 8: Update sql/nightly_build_and_test.sql
    with open("sql/nightly_build_and_test.sql", "r", encoding="utf-8") as f:
        sql_content = f.read()

    # 1. Update ga4_sessions_reliable definition in Section 15
    old_rule1 = "  dt.d < DATE '2026-10-24' AS ga4_sessions_reliable,"
    old_rule2 = """  (
    COALESCE(SAFE_DIVIDE(COALESCE(g.ga4_unassigned, 0), g.ga4_sessions), 1.0) <= 0.20
    AND COALESCE(SAFE_DIVIDE(v.granted_landings, v.visit_landings), 0.0) >= 0.50
  ) AS ga4_sessions_reliable,"""
    new_rule = """  (
    COALESCE(g.ga4_sessions, 0) > 0
    AND SAFE_DIVIDE(COALESCE(g.ga4_unassigned, 0), g.ga4_sessions) <= 0.20
    AND (SAFE_DIVIDE(v.granted_landings, v.visit_landings) IS NULL OR SAFE_DIVIDE(v.granted_landings, v.visit_landings) >= 0.50)
  ) AS ga4_sessions_reliable,"""
    if old_rule1 in sql_content:
        sql_content = sql_content.replace(old_rule1, new_rule, 1)
        print("Replaced ga4_sessions_reliable formula in sql/nightly_build_and_test.sql")
    elif old_rule2 in sql_content:
        sql_content = sql_content.replace(old_rule2, new_rule, 1)
        print("Replaced ga4_sessions_reliable formula (v2) in sql/nightly_build_and_test.sql")

    # 2. Update exec summary notes
    old_note = "'GA4 sessions unreliable since 2026-10-24; site CVR understated'"
    new_note = "'GA4 sessions unreliable since 2026-10-19; site CVR understated'"
    if old_note in sql_content:
        sql_content = sql_content.replace(old_note, new_note)
        print("Replaced exec summary notes in sql/nightly_build_and_test.sql")

    # 3. Update fact_ga4_purchases ga4_reliable default if 2026-10-24
    old_purch_rel = "  event_date < DATE '2026-10-24' AS ga4_reliable,"
    new_purch_rel = "  event_date < DATE '2026-10-19' AS ga4_reliable,"
    if old_purch_rel in sql_content:
        sql_content = sql_content.replace(old_purch_rel, new_purch_rel, 1)
        print("Replaced fact_ga4_purchases ga4_reliable date in sql/nightly_build_and_test.sql")

    # 4. Update rpt_visit_source_daily ga4_reliable default if 2026-10-24
    old_visit_rel = "  COALESCE(l.d, p.d) < DATE '2026-10-24' AS ga4_reliable,"
    new_visit_rel = "  COALESCE(l.d, p.d) < DATE '2026-10-19' AS ga4_reliable,"
    if old_visit_rel in sql_content:
        sql_content = sql_content.replace(old_visit_rel, new_visit_rel, 1)
        print("Replaced rpt_visit_source_daily ga4_reliable date in sql/nightly_build_and_test.sql")

    # 5. Update rpt_paid_media_page_daily ga4_reliable fallback if 2026-10-24
    old_paid_rel = "COALESCE(tc.ga4_sessions_reliable, COALESCE(s.d, w.d) < DATE '2026-10-24') AS ga4_reliable"
    new_paid_rel = "COALESCE(tc.ga4_sessions_reliable, COALESCE(s.d, w.d) < DATE '2026-10-19') AS ga4_reliable"
    if old_paid_rel in sql_content:
        sql_content = sql_content.replace(old_paid_rel, new_paid_rel, 1)
        print("Replaced rpt_paid_media_page_daily ga4_reliable fallback in sql/nightly_build_and_test.sql")

    # 6. Add ga4_break_quality CTE and ga4_reliable_from_break test if not present
    if "ga4_reliable_from_break" not in sql_content:
        target_cte = "ga4_flags AS (\n  SELECT\n    (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`)"
        replacement_cte = """ga4_break_quality AS (
  SELECT COUNTIF(d >= DATE '2026-10-19' AND ga4_sessions_reliable) AS true_after_break_count
  FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
),
ga4_flags AS (
  SELECT
    (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`)"""
        sql_content = sql_content.replace(target_cte, replacement_cte, 1)

        target_test = "UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_flag_populated', 'HARD',\n               IF(null_flags_count = 0, 'PASS', 'FAIL'), 0, null_flags_count,\n               'Every table reading GA4 has non-null ga4_reliable flag' FROM ga4_flags"
        replacement_test = """UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_flag_populated', 'HARD',
               IF(null_flags_count = 0, 'PASS', 'FAIL'), 0, null_flags_count,
               'Every table reading GA4 has non-null ga4_reliable flag' FROM ga4_flags
UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_from_break', 'HARD',
               IF(true_after_break_count = 0, 'PASS', 'FAIL'), 0, true_after_break_count,
               'Zero days from 2026-10-19 onwards have ga4_sessions_reliable = true' FROM ga4_break_quality"""
        sql_content = sql_content.replace(target_test, replacement_test, 1)
        print("Added ga4_reliable_from_break HARD test to sql/nightly_build_and_test.sql")

    with open("sql/nightly_build_and_test.sql", "w", encoding="utf-8") as f:
        f.write(sql_content)
    print("Saved updated sql/nightly_build_and_test.sql")

    # Step 9: Re-run DQ gate test
    sql_gate = """
    DECLARE v_run_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
    DECLARE v_stat_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();

    INSERT INTO `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
      (run_ts, run_date, test, severity, result, source_value, curated_value, detail)
    WITH ga4_break_quality AS (
      SELECT COUNTIF(d >= DATE '2026-10-19' AND ga4_sessions_reliable) AS true_after_break_count
      FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
    ),
    ga4_flags AS (
      SELECT
        (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_organic_social_weekly`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_ga4_purchases`)
        + (SELECT COUNTIF(ga4_reliable IS NULL) FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_exec_summary`) AS null_flags_count
    )
    SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_flag_populated', 'HARD',
           IF(null_flags_count = 0, 'PASS', 'FAIL'), 0, null_flags_count,
           'Every table reading GA4 has non-null ga4_reliable flag' FROM ga4_flags
    UNION ALL SELECT v_run_ts, DATE(v_run_ts), 'ga4_reliable_from_break', 'HARD',
           IF(true_after_break_count = 0, 'PASS', 'FAIL'), 0, true_after_break_count,
           'Zero days from 2026-10-19 onwards have ga4_sessions_reliable = true' FROM ga4_break_quality;
    """
    run_query(client, sql_gate, "Insert updated DQ gate results for GA4 reliability")

    # Step 10: Show output 1: 17-25 Oct from rpt_tracking_coverage_daily
    print("\n=======================================================")
    print("OUTPUT 1: 17-25 Oct from rpt_tracking_coverage_daily")
    print("=======================================================")
    sql_out1 = """
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
    WHERE d BETWEEN DATE '2026-10-17' AND DATE '2026-10-25'
    ORDER BY d ASC;
    """
    job_out1 = client.query(sql_out1)
    rows1 = list(job_out1.result())
    headers1 = ["d", "orders", "orders_tracked", "pct_tracked", "ga4_sessions", "ga4_unassigned", "pct_unassigned", "consent_granted_share", "est_visits", "ga4_sessions_reliable", "ga4_complete"]
    print("\t".join(headers1))
    for r in rows1:
        print("\t".join(str(r[h]) for h in headers1))

    # Step 11: Show output 2: the Tracking health row of vw_report_suite_health
    print("\n=======================================================")
    print("OUTPUT 2: Tracking health row of vw_report_suite_health")
    print("=======================================================")
    sql_out2 = """
    SELECT
      report,
      tables_read,
      built_at,
      latest_data_date,
      status,
      reason
    FROM `rh-ga4-491819.rh_curated_Bright_Test.vw_report_suite_health`
    WHERE report = 'Tracking health';
    """
    job_out2 = client.query(sql_out2)
    rows2 = list(job_out2.result())
    headers2 = ["report", "tables_read", "built_at", "latest_data_date", "status", "reason"]
    print("\t".join(headers2))
    for r in rows2:
        print("\t".join(str(r[h]) for h in headers2))

    # Step 12: Show output 3: the exec summary notes that mention the date
    print("\n=======================================================")
    print("OUTPUT 3: Exec summary notes that mention the date")
    print("=======================================================")
    sql_out3 = """
    SELECT
      kpi_id,
      section,
      label,
      period,
      value,
      unit,
      as_of_date,
      health_status,
      note
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_exec_summary`
    WHERE note LIKE '%2026-10-%'
    ORDER BY kpi_id;
    """
    job_out3 = client.query(sql_out3)
    rows3 = list(job_out3.result())
    headers3 = ["kpi_id", "section", "label", "period", "value", "unit", "as_of_date", "health_status", "note"]
    print("\t".join(headers3))
    for r in rows3:
        print("\t".join(str(r[h]) for h in headers3))

    # Step 13: Check ga4_reliable_from_break DQ gate result
    print("\n=======================================================")
    print("DQ GATE RESULT: ga4_reliable_from_break")
    print("=======================================================")
    sql_gate_res = """
    SELECT run_ts, test, severity, result, source_value, curated_value, detail
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dq_gate_results`
    WHERE test IN ('ga4_reliable_from_break', 'ga4_reliable_flag_populated')
    ORDER BY run_ts DESC, test ASC
    LIMIT 4;
    """
    job_gate = client.query(sql_gate_res)
    for r in job_gate.result():
        print(f"[{r.run_ts}] {r.test} ({r.severity}): {r.result} (source={r.source_value}, curated={r.curated_value}) - {r.detail}")

if __name__ == "__main__":
    main()
