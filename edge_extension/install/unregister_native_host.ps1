$ErrorActionPreference = "Stop"
$registryPath = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.company.patent_autofill"
if (Test-Path $registryPath) { Remove-Item -LiteralPath $registryPath -Recurse -Force }
$installDirectory = Join-Path $env:LOCALAPPDATA "PatentAutofill\NativeHost"
if (Test-Path $installDirectory) {
    $resolved = (Resolve-Path $installDirectory).Path
    $allowedRoot = (Join-Path $env:LOCALAPPDATA "PatentAutofill")
    if (-not $resolved.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove unexpected directory: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
Write-Host "Removed the current-user Patent Autofill Native Messaging registration."
