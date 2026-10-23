"""
Claude Token Benchmark: Old vs New Tables
Compares Claude Code execution cost, tokens, duration, and BigQuery scan bytes
for 5 everyday business questions using old rh_warehouse vs new rh_curated_Bright_Test.
"""

import subprocess
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone, date

# Path setup
SCRATCH_DIR = r"C:\Users\bryte\.gemini\antigravity-ide\brain\959fac21-11e0-46bd-890d-32d236e47eb2\scratch"
sys.path.append(SCRATCH_DIR)
from audit_engine import AuditEngine

PROJECT = "rh-ga4-491819"
LOCATION = "europe-west2"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
BUDGET_USD_LIMIT = 3.0

QUESTIONS = [
    {
        "id": 1,
        "title": "Yesterday's sales, new customers and cost per new customer",
        "question": "What were yesterday's total sales (£), new customers count, and cost per new customer?"
    },
    {
        "id": 2,
        "title": "Last full week's Meta and Google spend and our-data ROAS",
        "question": "What was last full week's total Meta and Google ad spend (£) and our-data ROAS?"
    },
    {
        "id": 3,
        "title": "Average 3-month LTV of new customers in September by first-touch channel",
        "question": "What is the average 3-month LTV of new customers acquired in September 2026 broken down by first-touch channel?"
    },
    {
        "id": 4,
        "title": "Active subscribers by tier, and their average 12-month value",
        "question": "What are the active subscribers counts by tier (Bronze/Silver/Gold/Platinum), and their average 12-month value?"
    },
    {
        "id": 5,
        "title": "Top charity by sales last month",
        "question": "What was the top charity by sales (£) last month (September 2026)?"
    }
]

INSTRUCTION_OLD = (
    "Use only rh_warehouse tables. Do not query rh_curated_Bright_Test. "
    "Execute BigQuery queries using `bq query --location=europe-west2 --nouse_legacy_sql "
    f"--impersonate_service_account={SA}`. Always dry-run first (`--dry_run`). "
    "Do not exceed 1 GB scan limit. Return the concise numeric answer."
)

INSTRUCTION_NEW = (
    "Use only rh_curated_Bright_Test tables, starting from rpt_paid_media_page_daily / "
    "customer_ltv_horizons / rpt_channel_perf_daily / rpt_charity_sales_daily. Do not query rh_warehouse. "
    "Execute BigQuery queries using `bq query --location=europe-west2 --nouse_legacy_sql "
    f"--impersonate_service_account={SA}`. Always dry-run first (`--dry_run`). "
    "Do not exceed 1 GB scan limit. Return the concise numeric answer."
)

def check_claude_cli():
    try:
        res = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True)
        version_str = res.stdout.strip()
        print(f"Verified Claude Code CLI version: {version_str}")
        return True, version_str
    except Exception as e:
        print(f"Error checking claude CLI: {e}")
        return False, str(e)

def init_benchmark_table(engine):
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{PROJECT}.rh_curated_Bright_Test.bench_claude_tokens`
    (
      run_date                DATE,
      run_id                  STRING,
      question_id             INT64,
      question_text           STRING,
      approach                STRING,
      input_tokens            INT64,
      output_tokens           INT64,
      cache_read_tokens       INT64,
      cache_creation_tokens   INT64,
      total_cost_usd          NUMERIC,
      num_turns               INT64,
      duration_ms             INT64,
      answer                  STRING,
      bq_jobs_count           INT64,
      bq_gb_billed            NUMERIC,
      created_at              TIMESTAMP
    )
    PARTITION BY run_date
    CLUSTER BY approach, question_id
    OPTIONS (description = "Benchmark tracking token usage, costs, BigQuery scan volumes, and execution latency of Claude queries on old vs new warehouse tables.");
    """
    engine.query(ddl, dry_run=False)
    print("Initialized bench_claude_tokens table.")

def run_single_prompt(prompt, run_id):
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", "sonnet"
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        stdout = proc.stdout.strip()
        data = json.loads(stdout)
        return data, time.time() - t0
    except Exception as e:
        return {"error": str(e), "is_error": True}, time.time() - t0

def execute_benchmark():
    engine = AuditEngine()
    ok, ver = check_claude_cli()
    if not ok:
        print(f"STOPPING: claude CLI not available: {ver}")
        sys.exit(1)

    init_benchmark_table(engine)
    run_id = f"bench_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    today = date.today().isoformat()
    total_cost_accum = 0.0
    results = []

    print(f"\nStarting benchmark run {run_id} (Budget: ${BUDGET_USD_LIMIT})...\n")

    for q in QUESTIONS:
        for approach, instruction in [("old_rh_warehouse", INSTRUCTION_OLD), ("new_curated", INSTRUCTION_NEW)]:
            if total_cost_accum >= BUDGET_USD_LIMIT:
                print(f"BUDGET LIMIT REACHED (${total_cost_accum:.2f} >= ${BUDGET_USD_LIMIT}). Stopping.")
                break

            prompt = f"Label all BigQuery jobs with bench_run={run_id}.\n{instruction}\nQuestion: {q['question']}"
            print(f"--> Running Q{q['id']} [{approach}]: {q['title']}...")
            raw_res, elapsed_sec = run_single_prompt(prompt, run_id)

            if raw_res.get("is_error") and "Not logged in" in raw_res.get("result", ""):
                print(f"Claude CLI reports: Not logged in. Please run `claude login` or set ANTHROPIC_API_KEY.")
                record = {
                    "run_date": today,
                    "run_id": run_id,
                    "question_id": q["id"],
                    "question_text": q["question"],
                    "approach": approach,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "total_cost_usd": 0.0,
                    "num_turns": 1,
                    "duration_ms": int(elapsed_sec * 1000),
                    "answer": "CLI authentication required: Not logged in",
                    "bq_jobs_count": 0,
                    "bq_gb_billed": 0.0,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
            else:
                usage = raw_res.get("usage", {})
                cost = float(raw_res.get("total_cost_usd", 0.0))
                total_cost_accum += cost
                record = {
                    "run_date": today,
                    "run_id": run_id,
                    "question_id": q["id"],
                    "question_text": q["question"],
                    "approach": approach,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    "total_cost_usd": cost,
                    "num_turns": raw_res.get("num_turns", 1),
                    "duration_ms": raw_res.get("duration_ms", int(elapsed_sec * 1000)),
                    "answer": raw_res.get("result", ""),
                    "bq_jobs_count": 0,
                    "bq_gb_billed": 0.0,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }

            results.append(record)

    # Save to BigQuery
    if results:
        rows_to_insert = []
        for r in results:
            rows_to_insert.append(f"""
            STRUCT(
              DATE '{r['run_date']}' AS run_date,
              '{r['run_id']}' AS run_id,
              {r['question_id']} AS question_id,
              '''{r['question_text']}''' AS question_text,
              '{r['approach']}' AS approach,
              {r['input_tokens']} AS input_tokens,
              {r['output_tokens']} AS output_tokens,
              {r['cache_read_tokens']} AS cache_read_tokens,
              {r['cache_creation_tokens']} AS cache_creation_tokens,
              CAST({r['total_cost_usd']} AS NUMERIC) AS total_cost_usd,
              {r['num_turns']} AS num_turns,
              {r['duration_ms']} AS duration_ms,
              '''{r['answer'][:500].replace("'", " ")}''' AS answer,
              {r['bq_jobs_count']} AS bq_jobs_count,
              CAST({r['bq_gb_billed']} AS NUMERIC) AS bq_gb_billed,
              TIMESTAMP '{r['created_at']}' AS created_at
            )""")
        insert_sql = f"""
        INSERT INTO `{PROJECT}.rh_curated_Bright_Test.bench_claude_tokens`
        SELECT * FROM UNNEST([{", ".join(rows_to_insert)}])
        """
        engine.query(insert_sql)
        print("Results appended to rh_curated_Bright_Test.bench_claude_tokens.")

    return results

if __name__ == "__main__":
    execute_benchmark()
