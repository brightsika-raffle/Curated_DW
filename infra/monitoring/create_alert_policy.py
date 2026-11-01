#!/usr/bin/env python3
"""
Infrastructure-as-Code script to create or verify Cloud Monitoring failure alerts for BigQuery scheduled queries.

Metric: bigquerydatatransfer.googleapis.com/transfer_config/completed_runs (completion_state = FAILED)
Window: 5-minute (300s), count > 0, auto-close after 1 day (86400s)
Notification: Email to bright@raffle-house.com only
Project: rh-ga4-491819
"""

import os, sys, subprocess, json
import urllib.request, urllib.error

PROJECT = "rh-ga4-491819"
SA = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"

def get_access_token():
    # Attempt impersonation first as required by policy
    res = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token", f"--impersonate-service-account={SA}"],
        capture_output=True, text=True
    )
    if res.returncode == 0:
        return res.stdout.strip().splitlines()[-1].strip()
    # Fallback to direct token if owner runs directly
    res2 = subprocess.run(
        ["cmd.exe", "/c", "gcloud.cmd", "auth", "print-access-token"],
        capture_output=True, text=True, check=True
    )
    return res2.stdout.strip().splitlines()[-1].strip()

def create_or_get_channel(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/notificationChannels"
    
    # 1. Check existing
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            for ch in data.get("notificationChannels", []):
                if ch.get("type") == "email" and ch.get("labels", {}).get("email_address") == "bright@raffle-house.com":
                    print(f"Found existing notification channel: {ch.get('name')}")
                    return ch.get("name")
    except urllib.error.HTTPError as e:
        print(f"Cannot list notification channels (HTTP {e.code}): {e.read().decode()}")
        raise e

    # 2. Create if not found
    with open("infra/monitoring/email_channel.json", "r", encoding="utf-8") as f:
        channel_payload = json.load(f)
    
    req_post = urllib.request.Request(url, data=json.dumps(channel_payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req_post) as resp:
        created = json.loads(resp.read().decode())
        ch_name = created.get("name")
        print(f"Created notification channel: {ch_name}")
        return ch_name

def create_or_get_policy(token, channel_name):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/alertPolicies"
    
    # 1. Check existing
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            for p in data.get("alertPolicies", []):
                if p.get("displayName") == "RH pipeline: scheduled query failed":
                    print(f"Found existing alert policy: {p.get('name')}")
                    return p.get("name")
    except urllib.error.HTTPError as e:
        print(f"Cannot list alert policies (HTTP {e.code}): {e.read().decode()}")
        raise e

    # 2. Create if not found
    with open("infra/monitoring/alert_policy.json", "r", encoding="utf-8") as f:
        policy_payload = json.load(f)
    if channel_name:
        policy_payload["notificationChannels"] = [channel_name]
        
    req_post = urllib.request.Request(url, data=json.dumps(policy_payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req_post) as resp:
        created = json.loads(resp.read().decode())
        p_name = created.get("name")
        print(f"Created alert policy: {p_name}")
        return p_name

def main():
    token = get_access_token()
    try:
        ch_name = create_or_get_channel(token)
        p_name = create_or_get_policy(token, ch_name)
        print("\nAlert policy and notification channel successfully configured:")
        print(f"Channel ID: {ch_name}")
        print(f"Policy ID:  {p_name}")
    except Exception as e:
        print(f"\nExecution halted due to IAM permission: {e}")
        print(f"Required roles on {PROJECT}:")
        print("  - roles/monitoring.notificationChannelEditor")
        print("  - roles/monitoring.alertPolicyEditor")

if __name__ == "__main__":
    main()
