import subprocess, os
from google.cloud import bigquery_datatransfer_v1
from google.oauth2.credentials import Credentials

SA = 'rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com'
token = subprocess.check_output(f'gcloud auth print-access-token --impersonate-service-account={SA}', shell=True, text=True).strip()
client = bigquery_datatransfer_v1.DataTransferServiceClient(credentials=Credentials(token))

cfg_name = 'projects/rh-ga4-491819/locations/europe-west2/transferConfigs/6abf66d9-0000-2436-a7b2-14c14ef3c450'
cfg = client.get_transfer_config(name=cfg_name)
print(f"Display Name:  {cfg.display_name}")
print(f"Schedule:      {cfg.schedule}")
print(f"Next Run Time: {cfg.next_run_time}")

runs = list(client.list_transfer_runs(parent=cfg_name))
print(f"\nRecent Transfer Runs ({len(runs)} found):")
for r in runs[:5]:
    run_id = r.name.split('/')[-1]
    print(f"Run ID: {run_id} | Schedule Time: {r.schedule_time} | Run Time: {r.run_time} | State: {r.state.name}")
