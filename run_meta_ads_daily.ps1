$sa = "rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com"
try {
    $env:CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT = $sa
    & "C:\Users\bryte\.venv_meridian\Scripts\python.exe" "C:\Users\bryte\Downloads\Raffle-House_DW\meta_to_bq.py" --ads-only
} finally {
    $env:CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT = $null
}
