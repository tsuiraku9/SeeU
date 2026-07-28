$env:WEBUI_LOGIN_TOKEN = 'test-webui-login-token-long-enough'
$env:SEEU_ENV_FILE = 'backend/tests/.env.e2e-isolated'
$env:WEBUI_PORT = '8099'
$env:SESSION_SECRET = 'test-session-secret-that-is-longer-than-32-characters'
$e2eRoot = Join-Path '.e2e' "seeu-$PID"
$env:DATABASE_PATH = Join-Path $e2eRoot 'state\app.db'
$env:ARCHIVE_ROOT = Join-Path $e2eRoot 'archive'
$env:PROVIDER_STAGING_ROOT = Join-Path $e2eRoot 'provider-staging'
$env:PROVIDER_BASE_URL = ''
$env:PROVIDER_API_TOKEN = ''
$env:MIN_FREE_DISK_GB = '0.1'
$env:SCHEDULER_ENABLED = 'false'
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port $env:WEBUI_PORT
