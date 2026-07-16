param(
    [string[]]$Path = @(),
    [switch]$NoRecurse
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "protect-data.ps1 manages Windows ACLs only. On Unix, use: chmod 600 .env; chmod -R go-rwx data"
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
if ($Path.Count -eq 0) {
    $Path = @(".env", "data")
}

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $currentIdentity.User
if ($null -eq $currentSid) {
    throw "Could not determine the current Windows user SID"
}
if ($currentIdentity.Name -match '\\CodexSandbox(?:Offline|Users)?$') {
    throw "Refusing to assign private data to a Codex sandbox identity. Run this script from your normal Windows account."
}
$currentPrincipal = [System.Security.Principal.WindowsPrincipal]::new($currentIdentity)
$isAdministrator = $currentPrincipal.IsInRole(
    [System.Security.Principal.WindowsBuiltInRole]::Administrator
)

$allowedSids = @(
    $currentSid,
    [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18"),
    [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
)

function Set-PrivateAcl {
    param([System.IO.FileSystemInfo]$Item)

    if ($Item.PSIsContainer) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        foreach ($sid in $allowedSids) {
            $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
                [System.Security.AccessControl.PropagationFlags]::None,
                [System.Security.AccessControl.AccessControlType]::Allow
            )
            [void]$acl.AddAccessRule($rule)
        }
    } else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        foreach ($sid in $allowedSids) {
            $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                [System.Security.AccessControl.AccessControlType]::Allow
            )
            [void]$acl.AddAccessRule($rule)
        }
    }

    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($currentSid)
    Set-Acl -LiteralPath $Item.FullName -AclObject $acl
}

$itemsToProtect = @()
foreach ($candidate in $Path) {
    $fullPath = if ([System.IO.Path]::IsPathRooted($candidate)) {
        [System.IO.Path]::GetFullPath($candidate)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $candidate))
    }

    if (-not (Test-Path -LiteralPath $fullPath)) {
        Write-Warning "Skipping missing path: $fullPath"
        continue
    }

    $rootItem = Get-Item -LiteralPath $fullPath -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to change ACLs through a reparse point: $fullPath"
    }

    $items = @($rootItem)
    if (-not $NoRecurse -and $rootItem.PSIsContainer) {
        $descendants = @(Get-ChildItem -LiteralPath $fullPath -Force -Recurse)
        $reparsePoints = @(
            $descendants | Where-Object {
                ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            }
        )
        if ($reparsePoints.Count -ne 0) {
            throw "Refusing to skip a descendant reparse point: $($reparsePoints[0].FullName)"
        }
        $items += $descendants
    }
    $itemsToProtect += $items
}

if ($itemsToProtect.Count -eq 0) {
    throw "No existing paths were protected"
}

# Complete path, ownership and reparse-point validation before changing the
# first ACL. A non-owner needs an elevated Administrators token to replace an
# ACL; fail before any partial update when that requirement is not met.
if (-not $isAdministrator) {
    foreach ($item in $itemsToProtect) {
        $owner = (Get-Acl -LiteralPath $item.FullName).Owner
        try {
            $ownerSid = ([System.Security.Principal.NTAccount]::new($owner)).Translate(
                [System.Security.Principal.SecurityIdentifier]
            )
        } catch {
            throw "Could not resolve the owner of $($item.FullName). Run this script from an elevated PowerShell."
        }
        if ($ownerSid -ne $currentSid) {
            throw "The current user does not own $($item.FullName). Run this script from an elevated PowerShell to take ownership and restrict its ACL."
        }
    }
}

$protectedCount = 0
foreach ($item in $itemsToProtect) {
    Set-PrivateAcl -Item $item
    $protectedCount += 1
}

Write-Host "Restricted ACLs on $protectedCount item(s) to the current user, SYSTEM, and Administrators."
