-- Nightly incremental refresh for Apple-adjusted email open-rate tables
-- Project: rh-ga4-491819, Dataset: rh_curated_Bright_Test, Location: europe-west2
-- Rebuilds only send_date in the last 35 days via MERGE, then refreshes cycle summary.

DECLARE rebuild_from DATE DEFAULT DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 35 DAY);

-- Fail loudly if dim_raffle has a 2027 house draw whose close date differs from the 3-month LAST_DAY projection
ASSERT (
  SELECT COUNTIF(
    house_close_date NOT IN (
      DATE '2027-03-31',
      DATE '2027-06-30',
      DATE '2027-09-30',
      DATE '2027-12-31'
    )
  ) = 0
  FROM (
    SELECT
      DATE(DATETIME_SUB(DATETIME(ends_at, 'Europe/London'), INTERVAL 1 SECOND)) AS house_close_date
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle`
    WHERE NOT IFNULL(is_unknown_member, FALSE)
      AND ends_at IS NOT NULL
      AND duration_days >= 50
  )
  WHERE EXTRACT(YEAR FROM house_close_date) = 2027
) AS 'dim_raffle contains a 2027 house draw whose close date differs from the projected 3-month LAST_DAY schedule (2027-03-31, 2027-06-30, 2027-09-30, 2027-12-31)';

MERGE `rh-ga4-491819.rh_curated_Bright_Test.rpt_email_campaign_open_rate` T
USING (
  WITH ev AS (
    SELECT
      message_id,
      profile_id,
      ANY_VALUE(campaign_id) AS campaign_id,
      MIN(IF(metric_name = 'Received Email', event_date, NULL)) AS rec_date,
      LOGICAL_OR(metric_name = 'Received Email') AS rec,
      LOGICAL_OR(metric_name = 'Opened Email' AND NOT IFNULL(is_machine_open, FALSE)) AS hopen,
      LOGICAL_OR(metric_name = 'Opened Email' AND IFNULL(is_machine_open, FALSE)) AS mopen,
      LOGICAL_OR(metric_name = 'Clicked Email' AND NOT IFNULL(is_machine_open, FALSE)) AS clk
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_klaviyo_event`
    WHERE event_date >= rebuild_from
      AND send_type = 'campaign'
      AND metric_name IN ('Received Email', 'Opened Email', 'Clicked Email')
    GROUP BY 1, 2
  ),
  msg AS (
    SELECT
      message_id,
      ANY_VALUE(campaign_id) AS campaign_id,
      MIN(rec_date) AS send_date
    FROM ev
    GROUP BY 1
    HAVING MIN(rec_date) >= rebuild_from
  ),
  house_draws_raw AS (
    SELECT
      raffle_key,
      DATE(DATETIME_SUB(DATETIME(ends_at, 'Europe/London'), INTERVAL 1 SECOND)) AS house_close_date
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_raffle`
    WHERE NOT IFNULL(is_unknown_member, FALSE)
      AND ends_at IS NOT NULL
      AND duration_days >= 50
  ),
  max_known_house AS (
    SELECT MAX(house_close_date) AS max_hc FROM house_draws_raw
  ),
  projected_house AS (
    SELECT
      CAST(NULL AS STRING) AS raffle_key,
      hc AS house_close_date
    FROM max_known_house,
    UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(max_hc, INTERVAL 1 DAY), DATE '2028-12-31', INTERVAL 3 MONTH)) AS m_start
    CROSS JOIN UNNEST([LAST_DAY(DATE_ADD(m_start, INTERVAL 2 MONTH))]) AS hc
    WHERE hc > max_hc
  ),
  house_draws AS (
    SELECT
      raffle_key,
      house_close_date,
      DATE_ADD(LAG(house_close_date) OVER (ORDER BY house_close_date), INTERVAL 1 DAY) AS cycle_start
    FROM (
      SELECT raffle_key, house_close_date FROM house_draws_raw
      UNION ALL
      SELECT raffle_key, house_close_date FROM projected_house
    )
  ),
  mc AS (
    SELECT
      m.message_id,
      m.campaign_id,
      m.send_date,
      FORMAT_DATE('%Y-%m', h.house_close_date) AS cycle_id,
      h.raffle_key AS house_raffle_key,
      h.cycle_start,
      h.house_close_date AS cycle_end
    FROM msg m
    JOIN house_draws h
      ON h.house_close_date >= m.send_date
    QUALIFY ROW_NUMBER() OVER (PARTITION BY m.message_id ORDER BY h.house_close_date) = 1
  ),
  camp_names AS (
    SELECT
      campaign_id,
      ANY_VALUE(campaign_name) AS campaign_name
    FROM `rh-ga4-491819.rh_curated_Bright_Test.stg_klaviyo_campaign_daily`
    WHERE send_date >= DATE '2022-01-01'
    GROUP BY 1
  ),
  prof AS (
    SELECT profile_id, customer_key
    FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_klaviyo_profile`
    QUALIFY ROW_NUMBER() OVER (PARTITION BY profile_id ORDER BY loaded_at DESC) = 1
  ),
  cycles AS (
    SELECT DISTINCT cycle_id, cycle_start
    FROM mc
  ),
  tx AS (
    SELECT customer_key, purchase_date, revenue_gbp
    FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
    WHERE purchase_date >= DATE '2023-01-01'
      AND revenue_gbp > 0
      AND NOT IFNULL(is_subscription_payment, FALSE)
      AND NOT IFNULL(is_free_entry, FALSE)
      AND customer_key IS NOT NULL
  ),
  vg AS (
    SELECT
      c.cycle_id,
      t.customer_key,
      IF(
        SUM(IF(t.purchase_date >= DATE_SUB(c.cycle_start, INTERVAL 12 MONTH), t.revenue_gbp, 0)) >= 40
        AND MAX(t.purchase_date) >= DATE_SUB(c.cycle_start, INTERVAL 6 MONTH),
        'champion',
        'other_purchaser'
      ) AS value_group
    FROM cycles c
    JOIN tx t
      ON t.purchase_date < c.cycle_start
    GROUP BY c.cycle_id, c.cycle_start, t.customer_key
  ),
  pm AS (
    SELECT
      e.message_id,
      e.profile_id,
      e.hopen,
      e.mopen,
      e.clk,
      mc.campaign_id,
      mc.send_date,
      mc.cycle_id,
      mc.house_raffle_key,
      mc.cycle_start,
      mc.cycle_end
    FROM ev e
    JOIN mc USING (message_id)
    WHERE e.rec
  ),
  mailc AS (
    SELECT
      profile_id,
      cycle_id,
      CASE
        WHEN LOGICAL_OR(mopen) AND NOT LOGICAL_OR(hopen) THEN 'apple'
        WHEN LOGICAL_OR(mopen) THEN 'mixed'
        ELSE 'non_apple'
      END AS mail_client
    FROM pm
    GROUP BY 1, 2
  ),
  base AS (
    SELECT
      pm.message_id,
      pm.campaign_id,
      pm.send_date,
      pm.cycle_id,
      pm.house_raffle_key,
      pm.cycle_start,
      pm.cycle_end,
      pm.profile_id,
      pm.hopen,
      pm.mopen,
      pm.clk,
      mailc.mail_client,
      IFNULL(vg.value_group, 'never_purchased') AS value_group
    FROM pm
    JOIN mailc USING (profile_id, cycle_id)
    LEFT JOIN prof USING (profile_id)
    LEFT JOIN vg
      ON vg.cycle_id = pm.cycle_id
     AND vg.customer_key = prof.customer_key
  ),
  ctor_mg AS (
    SELECT message_id, value_group, COUNTIF(hopen) AS o, COUNTIF(clk) AS c
    FROM base
    WHERE mail_client = 'non_apple'
    GROUP BY 1, 2
  ),
  ctor_m AS (
    SELECT message_id, SUM(o) AS o, SUM(c) AS c
    FROM ctor_mg
    GROUP BY 1
  ),
  ctor_gc AS (
    SELECT cycle_id, value_group, SUM(o) AS o, SUM(c) AS c
    FROM ctor_mg
    JOIN (SELECT DISTINCT message_id, cycle_id FROM base) USING (message_id)
    GROUP BY 1, 2
  ),
  grp AS (
    SELECT
      message_id,
      ANY_VALUE(campaign_id) AS campaign_id,
      send_date,
      cycle_id,
      ANY_VALUE(house_raffle_key) AS house_raffle_key,
      cycle_start,
      cycle_end,
      value_group,
      COUNT(*) AS delivered,
      COUNTIF(hopen) AS human_openers,
      COUNTIF(mopen) AS machine_openers,
      COUNTIF(clk) AS clickers,
      COUNTIF(mail_client = 'non_apple' AND hopen) AS na_open,
      COUNTIF(mail_client = 'mixed' AND hopen) AS mx_open,
      COUNTIF((mail_client = 'apple' AND clk) OR (mail_client = 'mixed' AND clk AND NOT hopen)) AS proj_clk
    FROM base
    GROUP BY 1, 3, 4, 6, 7, 8
  ),
  g2 AS (
    SELECT
      g.*,
      CASE
        WHEN mg.o >= 200 THEN SAFE_DIVIDE(mg.c, mg.o)
        WHEN m.o >= 200 THEN SAFE_DIVIDE(m.c, m.o)
        ELSE SAFE_DIVIDE(gc.c, gc.o)
      END AS ctor,
      CASE
        WHEN mg.o >= 200 THEN 'message_group'
        WHEN m.o >= 200 THEN 'message'
        ELSE 'group_cycle'
      END AS ctor_source
    FROM grp g
    LEFT JOIN ctor_mg mg USING (message_id, value_group)
    LEFT JOIN ctor_m m USING (message_id)
    LEFT JOIN ctor_gc gc
      ON gc.cycle_id = g.cycle_id
     AND gc.value_group = g.value_group
  )
  SELECT
    g2.message_id,
    g2.campaign_id,
    cn.campaign_name,
    g2.send_date,
    g2.cycle_id,
    g2.house_raffle_key,
    g2.cycle_start,
    g2.cycle_end,
    g2.value_group,
    g2.delivered,
    g2.human_openers,
    g2.machine_openers,
    g2.clickers,
    g2.na_open,
    g2.mx_open,
    g2.proj_clk,
    g2.ctor,
    g2.ctor_source,
    LEAST(g2.delivered, g2.na_open + g2.mx_open + IF(g2.ctor > 0, g2.proj_clk / g2.ctor, IF(g2.proj_clk > 0, g2.delivered, 0))) AS est_openers,
    SAFE_DIVIDE(LEAST(g2.delivered, g2.na_open + g2.mx_open + IF(g2.ctor > 0, g2.proj_clk / g2.ctor, IF(g2.proj_clk > 0, g2.delivered, 0))), g2.delivered) AS est_open_rate,
    SAFE_DIVIDE(g2.human_openers, g2.delivered) AS human_open_rate,
    CURRENT_TIMESTAMP() AS loaded_at
  FROM g2
  LEFT JOIN camp_names cn USING (campaign_id)
) S
ON T.send_date >= rebuild_from
  AND T.message_id = S.message_id
  AND T.value_group = S.value_group
WHEN MATCHED THEN UPDATE SET
  campaign_id = S.campaign_id,
  campaign_name = S.campaign_name,
  send_date = S.send_date,
  cycle_id = S.cycle_id,
  house_raffle_key = S.house_raffle_key,
  cycle_start = S.cycle_start,
  cycle_end = S.cycle_end,
  delivered = S.delivered,
  human_openers = S.human_openers,
  machine_openers = S.machine_openers,
  clickers = S.clickers,
  na_open = S.na_open,
  mx_open = S.mx_open,
  proj_clk = S.proj_clk,
  ctor = S.ctor,
  ctor_source = S.ctor_source,
  est_openers = S.est_openers,
  est_open_rate = S.est_open_rate,
  human_open_rate = S.human_open_rate,
  loaded_at = S.loaded_at
WHEN NOT MATCHED BY TARGET THEN INSERT (
  message_id, campaign_id, campaign_name, send_date, cycle_id, house_raffle_key,
  cycle_start, cycle_end, value_group, delivered, human_openers, machine_openers,
  clickers, na_open, mx_open, proj_clk, ctor, ctor_source, est_openers,
  est_open_rate, human_open_rate, loaded_at
) VALUES (
  S.message_id, S.campaign_id, S.campaign_name, S.send_date, S.cycle_id, S.house_raffle_key,
  S.cycle_start, S.cycle_end, S.value_group, S.delivered, S.human_openers, S.machine_openers,
  S.clickers, S.na_open, S.mx_open, S.proj_clk, S.ctor, S.ctor_source, S.est_openers,
  S.est_open_rate, S.human_open_rate, S.loaded_at
)
WHEN NOT MATCHED BY SOURCE AND T.send_date >= rebuild_from THEN DELETE;

CREATE OR REPLACE TABLE `rh-ga4-491819.rh_curated_Bright_Test.rpt_email_cycle_open_rate`
CLUSTER BY cycle_id, value_group
OPTIONS (
  description = 'Cycle-level Apple-adjusted email open rates by cycle_id (next closing house draw) and customer value_group.',
  labels = [('layer', 'report'), ('owner', 'bright_sika')]
)
AS
SELECT
  cycle_id,
  ANY_VALUE(house_raffle_key) AS house_raffle_key,
  cycle_start,
  cycle_end,
  IF(cycle_start < DATE '2027-01-01', '2-month (car -> house)', '3-month (car -> car -> house)') AS cadence_rule,
  value_group,
  COUNT(DISTINCT message_id) AS messages,
  SUM(delivered) AS delivered,
  SUM(human_openers) AS human_openers,
  SUM(machine_openers) AS machine_openers,
  SUM(clickers) AS clickers,
  SUM(est_openers) AS est_openers,
  SAFE_DIVIDE(SUM(est_openers), SUM(delivered)) AS est_open_rate,
  SAFE_DIVIDE(SUM(human_openers), SUM(delivered)) AS human_open_rate,
  CURRENT_TIMESTAMP() AS loaded_at
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_email_campaign_open_rate`
WHERE send_date >= DATE '2025-01-01'
GROUP BY 1, 3, 4, 5, 6;
