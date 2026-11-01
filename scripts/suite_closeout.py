#!/usr/bin/env python3
"""
Master Closeout Verification Runner for Prompt S
Runs scripts/recon_s1_organic.py, scripts/recon_s2_trial_status.py,
scripts/recon_s3_paid_media.py, and scripts/recon_s4_suite_health.py
"""

import subprocess, sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def run(script):
    print("\n" + "#" * 100)
    print(f"RUNNING: {script}")
    print("#" * 100)
    res = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if res.stdout:
        print(res.stdout)
    if res.stderr:
        print("STDERR:\n" + res.stderr)

def main():
    run("scripts/recon_s1_organic.py")
    run("scripts/recon_s2_trial_status.py")
    run("scripts/recon_s3_paid_media.py")
    run("scripts/recon_s4_suite_health.py")

if __name__ == "__main__":
    main()

