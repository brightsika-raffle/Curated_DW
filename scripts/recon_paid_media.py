import json
import os
import subprocess
import sys
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

def run_q(client, sql):
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels={"datacloud": "antigravity"})
    dry = client.query(sql, job_config=dry_cfg)
    if dry.total_bytes_processed > 1024**3:
        raise RuntimeError(f"Query exceeds 1 GB limit: {dry.total_bytes_processed}")
    print(f"[Dry run: {dry.total_bytes_processed:,} bytes ({dry.total_bytes_processed / (1024**2):.2f} MB)]")
    cfg = bigquery.QueryJobConfig(labels={"datacloud": "antigravity"})
    return list(client.query(sql, job_config=cfg).result())

def pct_diff(ours, his):
    if his == 0 and ours == 0:
        return "0.00%"
    if his == 0:
        return "N/A (his=0)"
    return f"{((ours - his) / his) * 100:+.2f}%"

def main():
    client = get_bq_client()
    with open("reference/jordan_pipeline/data2.json", "r", encoding="utf-8") as f:
        d2 = json.load(f)

    days = d2["days"]
    idx_18 = [i for i, d in enumerate(days) if "2026-09-01" <= d <= "2026-10-18"]
    idx_19 = [i for i, d in enumerate(days) if "2026-09-01" <= d <= "2026-09-19"]
    idx_post19 = [i for i, d in enumerate(days) if "2026-09-20" <= d <= "2026-10-18"]

    def s(arr, idx=idx_18):
        return round(sum(arr[i] for i in idx), 2)

    sql_ours = """
    SELECT
      basis,
      entity_id,
      ROUND(SUM(spend), 2) AS spend,
      ROUND(SUM(spend_inc_vat), 2) AS spend_inc_vat,
      SUM(wh_n) AS wh_n,
      SUM(wh_nt) AS wh_nt,
      ROUND(SUM(wh_rev), 2) AS wh_rev,
      SUM(new_cust) AS new_cust,
      SUM(no_n) AS no_n,
      SUM(ns_n) AS ns_n,
      SUM(er_n) AS er_n,
      SUM(es_n) AS es_n,
      ROUND(SUM(v3), 2) AS v3,
      ROUND(SUM(adj_rev), 2) AS adj_rev,
      SUM(IF(d <= DATE '2026-09-19', wh_n, 0)) AS wh_n_to_sep19,
      SUM(IF(d <= DATE '2026-09-19', wh_nt, 0)) AS wh_nt_to_sep19,
      ROUND(SUM(IF(d <= DATE '2026-09-19', wh_rev, 0)), 2) AS wh_rev_to_sep19
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_daily`
    WHERE d BETWEEN DATE '2026-09-01' AND DATE '2026-10-18'
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    our_rows = {(r["basis"], r["entity_id"]): dict(r) for r in run_q(client, sql_ours)}

    sql_status = """
    SELECT wh_to, meta_to, google_to, bing_to, affiliates_to, p99_v3, report_start, mat_from
    FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_entity_status`
    """
    st = dict(run_q(client, sql_status)[0])
    print("\n=== STATUS TABLE (rpt_paid_media_entity_status) ===")
    print(f"wh_to={st['wh_to']} | meta_to={st['meta_to']} | google_to={st['google_to']} | bing_to={st['bing_to']} | affiliates_to={st['affiliates_to']} | our_p99_v3={st['p99_v3']} | his_p99_v3={d2.get('p99')}")

    # Order of entities per prompt: ps, ps-pr, ps-rmg, ps-rtg, ps-unk, each Meta campaign, gb, each Google/Bing sub, aff
    d2_ents = {e["id"]: e for e in d2["ents"]}
    ordered_ids = [
        "ps", "ps-pr", "ps-rmg", "ps-rtg", "ps-unk",
        *[e["id"] for e in d2["ents"] if e["id"].startswith("m")],
        "gb", "g-comp", "g-gen", "g-brand", "g-dg", "g-pmax", "g-unk",
        "b-comp", "b-gen", "b-unk", "aff"
    ]

    for bs in ["F", "L"]:
        print(f"\n=========================================================================================================================================")
        print(f"RECONCILIATION: BASIS {bs} | 2026-09-01 to 2026-10-18 (ours vs data2.json)")
        print(f"=========================================================================================================================================")
        hdr = (
            f"{'entity_id':20s} | {'spend (ours/his/diff)':28s} | {'inc_vat':10s} | "
            f"{'wh_n (ours/his/diff)':22s} | {'nt (o/h)':8s} | {'wh_rev (ours/his/diff)':28s} | "
            f"{'new_cust (o/h)':14s} | {'no/ns/er/es (ours vs his)':29s} | "
            f"{'v3 (ours/his/diff)':28s} | {'adj_rev (ours/his/diff)':28s}"
        )
        print(hdr)
        print("-" * len(hdr))
        for eid in ordered_ids:
            r = our_rows.get((bs, eid))
            h_sp = s(d2["P"][eid]["sp"])
            h_n = int(s(d2["basis"][bs]["O"][eid]["n"]))
            h_nt = int(s(d2["basis"][bs]["O"][eid]["nt"]))
            h_rev = s(d2["basis"][bs]["O"][eid]["rev"])
            h_nc = int(s(d2["basis"][bs]["O"][eid]["newc"]))
            h_no = int(s(d2["basis"][bs]["W"][eid]["NO"]["n"]))
            h_ns = int(s(d2["basis"][bs]["W"][eid]["NS"]["n"]))
            h_er = int(s(d2["basis"][bs]["W"][eid]["ER"]["n"]))
            h_es = int(s(d2["basis"][bs]["W"][eid]["ES"]["n"]))
            h_v3 = s(d2["basis"][bs]["O"][eid]["v3"])
            h_adj = s(d2["basis"][bs]["ADJ"][eid])

            if r is None:
                print(
                    f"{eid:20s} | {'[DROPPED <£100 Sep-Oct]'+'/'+f'{h_sp:.2f}':28s} | {'N/A':10s} | "
                    f"{'N/A/'+str(h_n):22s} | {'N/A/'+str(h_nt):8s} | {'N/A/'+f'{h_rev:.2f}':28s} | "
                    f"{'N/A/'+str(h_nc):14s} | {'N/A vs '+f'{h_no}/{h_ns}/{h_er}/{h_es}':29s} | "
                    f"{'N/A/'+f'{h_v3:.2f}':28s} | {'N/A/'+f'{h_adj:.2f}':28s}"
                )
                continue

            o_sp = float(r["spend"])
            o_inc = float(r["spend_inc_vat"])
            o_n = int(r["wh_n"])
            o_nt = int(r["wh_nt"])
            o_rev = float(r["wh_rev"])
            o_nc = int(r["new_cust"])
            o_no, o_ns, o_er, o_es = int(r["no_n"]), int(r["ns_n"]), int(r["er_n"]), int(r["es_n"])
            o_v3 = float(r["v3"])
            o_adj = float(r["adj_rev"])

            sp_str = f"{o_sp:.2f}/{h_sp:.2f} ({pct_diff(o_sp, h_sp)})"
            n_str = f"{o_n}/{h_n} ({pct_diff(o_n, h_n)})"
            nt_str = f"{o_nt}/{h_nt}"
            rev_str = f"{o_rev:.2f}/{h_rev:.2f} ({pct_diff(o_rev, h_rev)})"
            nc_str = f"{o_nc}/{h_nc}"
            ct_str = f"{o_no}/{o_ns}/{o_er}/{o_es} vs {h_no}/{h_ns}/{h_er}/{h_es}"
            v3_str = f"{o_v3:.2f}/{h_v3:.2f} ({pct_diff(o_v3, h_v3)})"
            adj_str = f"{o_adj:.2f}/{h_adj:.2f} ({pct_diff(o_adj, h_adj)})"

            print(
                f"{eid:20s} | {sp_str:28s} | {o_inc:10.2f} | "
                f"{n_str:22s} | {nt_str:8s} | {rev_str:28s} | "
                f"{nc_str:14s} | {ct_str:29s} | "
                f"{v3_str:28s} | {adj_str:28s}"
            )

    print("\n=== FIX 2 & FIX 3 DIAGNOSTIC: PURCHASE POPULATION & SUBSCRIPTION SIGNUP TOUCHES (1 Sep - 18 Oct) ===")
    sql_fix2 = """
    WITH base AS (
      SELECT
        t.transaction_id,
        t.customer_key,
        t.revenue_gbp,
        IFNULL(t.is_free_entry, FALSE) AS is_free_entry,
        t.is_first_purchase,
        (t.is_subscription_payment OR t.order_type = 'SUBSCRIPTION') AS is_sub,
        t.order_type,
        COALESCE(t.payment_initiated_by, '') AS payment_initiated_by,
        touch.attribution_basis,
        s.created_by AS sub_created_by,
        DATE(t.purchase_at, 'Europe/London') AS pay_date_lon,
        touch.first_touch_timestamp,
        touch.last_touch_timestamp,
        COALESCE(touch.first_touch_timestamp, touch.last_touch_timestamp) AS f_ts,
        LOWER(REPLACE(COALESCE(touch.first_touch_url, touch.last_touch_url), '%3F', '?')) AS f_url,
        NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(COALESCE(touch.first_touch_url, touch.last_touch_url), '%3F', '?')), r'[?&]utm_source=([^&#]*)'), '') AS utm_source,
        NULLIF(REGEXP_EXTRACT(LOWER(REPLACE(COALESCE(touch.first_touch_url, touch.last_touch_url), '%3F', '?')), r'[?&]utm_campaign=([^&#]*)'), '') AS utm_camp
      FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` t
      JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_transaction_touch` touch
        ON t.transaction_id = touch.transaction_id
      LEFT JOIN `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` s
        ON t.subscription_key = s.subscription_key
      WHERE t.customer_key != 'UNKNOWN_DELETED'
        AND COALESCE(touch.first_touch_timestamp, touch.last_touch_timestamp) IS NOT NULL
        AND DATE(COALESCE(touch.first_touch_timestamp, touch.last_touch_timestamp), 'Europe/London') BETWEEN DATE '2026-09-01' AND DATE '2026-10-18'
        AND DATE_DIFF(DATE(t.purchase_at, 'Europe/London'), DATE(COALESCE(touch.first_touch_timestamp, touch.last_touch_timestamp), 'Europe/London'), DAY) BETWEEN 0 AND 7
    ),
    ch_labelled AS (
      SELECT
        *,
        CASE
          WHEN REGEXP_CONTAINS(f_url, r'clickref|utm_medium=[^&#]*affiliate|utm_source=(affiliate|impact|partnerize)') THEN 'Affiliates'
          WHEN REGEXP_CONTAINS(f_url, r'utm_source=klaviyo|utm_klaviyo_id|_kx=') THEN 'Email'
          WHEN REGEXP_CONTAINS(f_url, r'utm_source=attentive|utm_medium=sms') THEN 'SMS'
          WHEN REGEXP_CONTAINS(f_url, r'utm_source=mentionme') THEN 'Referral'
          WHEN REGEXP_CONTAINS(f_url, r'msclkid|utm_source=bing') THEN 'Bing'
          WHEN REGEXP_CONTAINS(f_url, r'gclid|gbraid|wbraid|gad_campaignid|utm_source=google') THEN 'Google'
          WHEN (
            REGEXP_CONTAINS(f_url, r'utm_source=(meta|fb|ig|facebook|instagram|meta_fb|meta_ig|an|th)|tw_source=(fb|ig)|fbclid')
            AND (
              utm_camp IS NOT NULL
              OR REGEXP_CONTAINS(f_url, r'utm_medium=(cpc|paid|paidsocial|cpm|video|feed|story|reels)')
              OR REGEXP_CONTAINS(f_url, r'tw_')
              OR REGEXP_CONTAINS(f_url, r'utm_id=')
              OR (REGEXP_CONTAINS(f_url, r'fbclid=') AND utm_source IS NULL)
            )
          ) THEN 'Paid Social'
          ELSE 'Other'
        END AS channel
      FROM base
    )
    SELECT
      channel,
      COUNTIF(NOT is_free_entry AND attribution_basis != 'inherited_from_subscription_signup') AS old_filter_paid_n,
      COUNTIF(NOT is_free_entry AND payment_initiated_by != 'recurring' AND attribution_basis = 'inherited_from_subscription_signup') AS manual_sub_signups_excluded_by_old_filter,
      COUNTIF(is_free_entry AND order_type = 'SUBSCRIPTION' AND sub_created_by = 'special-offer' AND payment_initiated_by != 'recurring') AS special_offer_free_trials_nt,
      COUNTIF(payment_initiated_by != 'recurring' AND (NOT is_free_entry OR (is_free_entry AND order_type = 'SUBSCRIPTION' AND sub_created_by = 'special-offer'))) AS new_filter_total_wh_n,
      COUNTIF(payment_initiated_by = 'recurring') AS recurring_renewals_excluded,
      COUNTIF(first_touch_timestamp IS NULL AND last_touch_timestamp IS NOT NULL AND payment_initiated_by != 'recurring' AND NOT is_free_entry) AS f_fallback_to_last_touch_n
    FROM ch_labelled
    WHERE channel IN ('Paid Social', 'Google', 'Bing', 'Affiliates')
    GROUP BY 1
    ORDER BY 1
    """
    for r in run_q(client, sql_fix2):
        print(
            f"{r['channel']:12s} | old_filter_paid={r['old_filter_paid_n']:5d} "
            f"| +manual_sub_signups={r['manual_sub_signups_excluded_by_old_filter']:4d} "
            f"| +free_trials(wh_nt)={r['special_offer_free_trials_nt']:2d} "
            f"| =new_wh_n={r['new_filter_total_wh_n']:5d} "
            f"| recurring_excluded={r['recurring_renewals_excluded']:5d} "
            f"| f_fallback_to_last={r['f_fallback_to_last_touch_n']:3d}"
        )

    print("\n=== FIX 1 DIAGNOSTIC: BASIS L IN data2.json (1-19 Sep vs 20 Sep-18 Oct) ===")
    for eid in ["ps", "ps-pr", "ps-rmg", "ps-rtg", "ps-unk", "gb", "g-comp", "g-gen", "g-brand", "g-dg", "g-pmax", "b-comp", "b-gen", "aff"]:
        r = our_rows.get(("L", eid))
        h_f_19 = int(s(d2["basis"]["F"]["O"][eid]["n"], idx_19))
        h_l_19 = int(s(d2["basis"]["L"]["O"][eid]["n"], idx_19))
        h_l_post19 = int(s(d2["basis"]["L"]["O"][eid]["n"], idx_post19))
        o_l_19 = int(r["wh_n_to_sep19"]) if r else 0
        o_l_full = int(r["wh_n"]) if r else 0
        print(f"{eid:10s} | data2.json 1-19 Sep: F={h_f_19:5d}, L={h_l_19:5d} | data2.json 20 Sep-18 Oct: L={h_l_post19:5d} | ours 1-19 Sep: L={o_l_19:5d} ({pct_diff(o_l_19, h_l_19)}) | ours 1 Sep-18 Oct: L={o_l_full:5d}")

    print("\n=== FIX 6 DIAGNOSTIC: 16 META CAMPAIGNS IN data2.json vs stg_meta_ads_daily ===")
    meta_cids = [e["id"][1:] for e in d2["ents"] if e["id"].startswith("m")]
    cids_sql = ", ".join(f"'{c}'" for c in meta_cids)
    sql_meta_16 = f"""
    SELECT
      campaign_id,
      ANY_VALUE(campaign_name) AS campaign_name,
      ROUND(SUM(IF(spend_date < DATE '2026-09-01', spend_gbp, 0)), 2) AS pre_sep_spend_gbp,
      ROUND(SUM(IF(spend_date BETWEEN DATE '2026-09-01' AND DATE '2026-10-18', spend_gbp, 0)), 2) AS sep1_to_oct18_spend_gbp,
      ROUND(SUM(IF(spend_date >= DATE '2026-09-01', spend_gbp, 0)), 2) AS sep1_onwards_spend_gbp
    FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_meta_ads_daily`
    WHERE campaign_id IN ({cids_sql})
    GROUP BY 1
    ORDER BY sep1_onwards_spend_gbp DESC, pre_sep_spend_gbp DESC
    """
    for r in run_q(client, sql_meta_16):
        cid = r["campaign_id"]
        eid = "m" + cid
        in_dim = ("F", eid) in our_rows
        d2_sp = s(d2["P"][eid]["sp"])
        print(f"{eid:20s} | in_our_dim={str(in_dim):5s} | pre_sep_spend=£{float(r['pre_sep_spend_gbp']):9.2f} | sep1_oct18_bq=£{float(r['sep1_to_oct18_spend_gbp']):9.2f} | data2_sep1_oct18=£{d2_sp:9.2f} | {r['campaign_name']}")

if __name__ == "__main__":
    main()

