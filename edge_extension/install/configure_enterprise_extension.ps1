param(
    [Parameter(Mandatory = $true)][string]$PatentOrigin,
    [string[]]$IframeOrigins = @(),
    [string]$ProjectRoot,
    [string]$OutputDirectory
)
$ErrorActionPreference = "Stop"
$scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $scriptRoot) { throw "Unable to locate the installer script directory." }
if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is not available for the current Windows user." }
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $scriptRoot "..\..")).Path
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $env:LOCALAPPDATA "PatentAutofill\EdgeExtension"
}
function Get-ExactOrigin([string]$Value) {
    $uri = [Uri]$Value
    if ($uri.Scheme -notin @("http", "https") -or -not $uri.Host -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw "Each origin must be a complete HTTP/HTTPS origin, for example https://patent.example.internal"
    }
    if ($uri.AbsolutePath -and $uri.AbsolutePath -ne "/") {
        throw "Origins must not contain a path. Use only scheme, host, and optional port."
    }
    return $uri.GetLeftPart([System.UriPartial]::Authority)
}
$origins = @((Get-ExactOrigin $PatentOrigin))
foreach ($iframeOrigin in $IframeOrigins) {
    $origins += Get-ExactOrigin $iframeOrigin
}
$origins = @($origins | Sort-Object -Unique)
$source = Join-Path $ProjectRoot "edge_extension"
if (-not (Test-Path (Join-Path $source "manifest.json"))) {
    throw "edge_extension\manifest.json was not found under $source"
}
$allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "PatentAutofill"))
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
if (-not $resolvedOutput.StartsWith($allowedRoot + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must be inside $allowedRoot"
}
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
Get-ChildItem -LiteralPath $resolvedOutput -Force | Remove-Item -Recurse -Force
Copy-Item -Path (Join-Path $source "*") -Destination $resolvedOutput -Recurse -Force
$manifestPath = Join-Path $resolvedOutput "manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$manifest.permissions = @($manifest.permissions | Where-Object { $_ -ne "activeTab" })
$manifest | Add-Member -NotePropertyName host_permissions -NotePropertyValue @($origins | ForEach-Object { "$_/*" }) -Force
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$manifestJson = $manifest | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($manifestPath, $manifestJson + [Environment]::NewLine, $utf8NoBom)
Write-Host "Generated an extension restricted to $($origins -join ', ') at $resolvedOutput"
Write-Host "Load this generated directory from edge://extensions. Do not load the source directory."
