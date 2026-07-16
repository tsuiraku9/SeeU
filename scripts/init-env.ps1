param(
    [string]$OutputPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $repositoryRoot ".env"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $repositoryRoot $OutputPath
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

if (Test-Path -LiteralPath $OutputPath) {
    throw "Refusing to overwrite existing environment file: $OutputPath"
}

function New-UrlSafeSecret([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

$templatePath = Join-Path $repositoryRoot ".env.example"
$contents = [System.IO.File]::ReadAllText($templatePath)
if ($contents -notmatch "(?m)^WEBUI_LOGIN_TOKEN=$" -or $contents -notmatch "(?m)^SESSION_SECRET=$") {
    throw ".env.example no longer contains empty WEBUI_LOGIN_TOKEN and SESSION_SECRET fields"
}
$contents = [regex]::Replace(
    $contents,
    "(?m)^SESSION_SECRET=$",
    "SESSION_SECRET=$(New-UrlSafeSecret 48)"
)

$parent = Split-Path -Parent $OutputPath
if (-not [string]::IsNullOrEmpty($parent)) {
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
}
$created = $false
try {
    # CreateNew makes the no-overwrite guarantee atomic instead of relying on
    # the earlier Test-Path check, which is only retained for a clearer error.
    $stream = [System.IO.FileStream]::new(
        $OutputPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    $created = $true
    try {
        $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($contents)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }

    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        & (Join-Path $PSScriptRoot "protect-data.ps1") -Path @($OutputPath) -NoRecurse
    } else {
        & chmod 600 -- $OutputPath
        if ($LASTEXITCODE -ne 0) {
            throw "Could not restrict permissions on $OutputPath"
        }
    }
} catch {
    if ($created) {
        [System.IO.File]::Delete($OutputPath)
    }
    throw
}

Write-Host "Created $OutputPath with a cryptographically random SESSION_SECRET. WEBUI_LOGIN_TOKEN remains empty; at startup the application will atomically write the generated token to data/state/webui-login-token.txt, never to logs."
