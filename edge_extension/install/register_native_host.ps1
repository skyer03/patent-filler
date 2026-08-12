param(
    [Parameter(Mandatory = $true)][string]$ExtensionId,
    [string]$ProjectRoot,
    [string]$InstallDirectory
)
$ErrorActionPreference = "Stop"
$scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $scriptRoot) { throw "Unable to locate the installer script directory." }
if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is not available for the current Windows user." }
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $scriptRoot "..\..")).Path
}
if ([string]::IsNullOrWhiteSpace($InstallDirectory)) {
    $InstallDirectory = Join-Path $env:LOCALAPPDATA "PatentAutofill\NativeHost"
}
if ($ExtensionId -notmatch '^[a-p]{32}$') {
    throw "ExtensionId must be the 32-character ID shown at edge://extensions."
}
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$launcherSource = Join-Path $scriptRoot "native_host_launcher.cs"
$allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "PatentAutofill"))
$resolvedInstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
if (-not $resolvedInstallDirectory.StartsWith($allowedRoot + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "InstallDirectory must be inside $allowedRoot"
}
$launcher = Join-Path $resolvedInstallDirectory "PatentAutofillNativeHost.exe"
New-Item -ItemType Directory -Force -Path $resolvedInstallDirectory | Out-Null
if (Test-Path $launcher) { Remove-Item -LiteralPath $launcher -Force }
$source = Get-Content -LiteralPath $launcherSource -Raw -Encoding UTF8
Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $launcher -OutputType ConsoleApplication
Set-Content -LiteralPath (Join-Path $resolvedInstallDirectory "project-root.txt") -Value $resolvedRoot -Encoding UTF8

$manifestPath = Join-Path $resolvedInstallDirectory "com.company.patent_autofill.json"
$manifest = @{
    name = "com.company.patent_autofill"
    description = "Local reviewed patent autofill task bridge"
    path = $launcher
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$manifestJson = $manifest | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($manifestPath, $manifestJson + [Environment]::NewLine, $utf8NoBom)
$registryPath = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.company.patent_autofill"
New-Item -Path $registryPath -Force | Out-Null
Set-Item -Path $registryPath -Value $manifestPath
Write-Host "Registered the Native Messaging host for the current user."
Write-Host "Allowed extension ID: $ExtensionId"
Write-Host "Task store: $(Join-Path $resolvedRoot '.m6\dom-bridge')"
