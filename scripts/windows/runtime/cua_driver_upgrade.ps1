$ErrorActionPreference = "Continue"
Write-Host "=== CUA Driver Upgrade ==="

# 1. Stop running cua-driver processes — kill BOTH trycua and qwen variants
Write-Host "Stopping cua-driver processes..."
schtasks.exe /End /TN "cua-driver-serve" 2>$null | Out-Null
schtasks.exe /End /TN "qwen-cua-driver-serve" 2>$null | Out-Null
Start-Sleep -Milliseconds 250
taskkill.exe /F /IM "cua-driver.exe" /T 2>$null | Out-Null
taskkill.exe /F /IM "cua-driver-uia.exe" /T 2>$null | Out-Null
taskkill.exe /F /IM "qwen-cua-driver.exe" /T 2>$null | Out-Null
taskkill.exe /F /IM "qwen-cua-driver-uia.exe" /T 2>$null | Out-Null
Get-Process -Name "cua-driver","cua-driver-uia","qwen-cua-driver","qwen-cua-driver-uia" -EA SilentlyContinue | Stop-Process -Force -EA SilentlyContinue
Start-Sleep -Milliseconds 500
Write-Host "  Processes stopped"

# 2. Set install env vars
$env:CUA_DRIVER_RS_VERSION = "{version}"
$env:CUA_DRIVER_RS_INSTALL_DIR = "C:\Program Files\Cua\cua-driver\bin"
$env:CUA_DRIVER_RS_HOME = "C:\Program Files\Cua\cua-driver"
Write-Host "  Target version: $env:CUA_DRIVER_RS_VERSION"

# 3. Run official installer
Write-Host "Running official installer..."
$ErrorActionPreference = "Stop"
irm {install_url} | iex
$ErrorActionPreference = "Continue"

# 4. Kick autostart
Write-Host "Enabling autostart..."
{bin_name} autostart enable 2>&1 | Out-Null
{bin_name} autostart kick 2>&1 | Out-Null

# 5. Ensure Machine PATH contains cua-driver, clean User PATH
$machPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
if ($machPath -notlike "*Cua\cua-driver*") {
    [System.Environment]::SetEnvironmentVariable("Path", "$machPath;C:\Program Files\Cua\cua-driver\bin", "Machine")
    Write-Host "  Added cua-driver to Machine PATH"
} else {
    Write-Host "  Machine PATH already contains cua-driver"
}
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
$cleanedUserPath = ($userPath -split ";" | Where-Object { $_ -notlike "*cua-driver*" -and $_ -notlike "*qwen-cua-driver*" -and $_ -notlike "*\Cua\*" -and $_ -ne "" }) -join ";"
if ($cleanedUserPath -ne $userPath) {
    [System.Environment]::SetEnvironmentVariable("Path", $cleanedUserPath, "User")
    Write-Host "  Cleaned cua-driver from User PATH"
}

# 6. Print version for verification
$ver = {bin_name} --version 2>&1
Write-Host "INSTALLED_VERSION=$ver"
Write-Host "=== CUA Driver Upgrade Complete ==="
