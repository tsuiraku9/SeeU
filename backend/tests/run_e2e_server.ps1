$env:WEBUI_LOGIN_TOKEN = 'test-webui-login-token-long-enough'
$env:WEBUI_PORT = '8099'
$env:SESSION_SECRET = 'test-session-secret-that-is-longer-than-32-characters'
$env:DATABASE_PATH = "data/state/e2e-$PID.db"
$env:ARCHIVE_ROOT = "data/archive/e2e-$PID"
$env:PROVIDER_STAGING_ROOT = "data/provider-staging/e2e-$PID"
$env:PROVIDER_BASE_URL = ''
$env:PROVIDER_API_TOKEN = ''
$env:MIN_FREE_DISK_GB = '0.1'
$env:SCHEDULER_ENABLED = 'false'
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port $env:WEBUI_PORT
