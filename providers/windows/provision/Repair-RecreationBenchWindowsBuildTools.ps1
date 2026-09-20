[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Add-MachinePath([string]$Path) {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($machine -notlike "*$Path*") {
        [Environment]::SetEnvironmentVariable("Path", "$machine;$Path", "Machine")
    }
}

function Prepend-MachinePath([string]$Path) {
    $entries = @([Environment]::GetEnvironmentVariable("Path", "Machine") -split ";" |
        Where-Object { $_ -and $_.TrimEnd('\') -ine $Path.TrimEnd('\') })
    [Environment]::SetEnvironmentVariable("Path", ((@($Path) + $entries) -join ";"), "Machine")
}

function Find-Jdk17 {
    $candidates = @($env:JAVA17_HOME, $env:JAVA_HOME)
    foreach ($root in @(
        "C:\Program Files\Eclipse Adoptium",
        "C:\Program Files\Microsoft",
        "C:\Program Files\Java",
        "C:\Program Files\OpenJDK"
    )) {
        if (Test-Path $root) {
            $candidates += @(Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName)
        }
    }
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        $javac = Join-Path $candidate "bin\javac.exe"
        $jpackage = Join-Path $candidate "bin\jpackage.exe"
        if (-not (Test-Path $javac) -or -not (Test-Path $jpackage)) { continue }
        $version = (& $javac -version 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $version -match "(?m)^\s*javac\s+17(?:\.|\s|$)") {
            return $candidate
        }
    }
    return $null
}

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found on PATH: $Name"
    }
}

function Assert-VsBuildTools {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) {
        throw "vswhere.exe not found; Visual Studio Build Tools 2022 installation is incomplete"
    }
    $vsInstall = & $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath
    if ([string]::IsNullOrWhiteSpace($vsInstall)) {
        throw "MSVC v143 x86/x64 tools not found via vswhere"
    }
    Write-Host "Visual Studio Build Tools verified: $vsInstall"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated Administrator PowerShell session"
}

if (-not (Get-Command choco.exe -ErrorAction SilentlyContinue)) {
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))
}

Refresh-Path
$chocoSource = "https://community.chocolatey.org/api/v2/"

Write-Host "==> Installing Visual Studio Build Tools 2022 + MSVC v143"
$vsParams = "--add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.VC.ATLMFC --add Microsoft.VisualStudio.Component.Windows10SDK.19041 --add Microsoft.Net.Component.4.8.SDK --add Microsoft.Net.Component.4.8.TargetingPack --includeRecommended --passive --locale en-US"
choco install -y visualstudio2022buildtools --source $chocoSource --no-progress `
    --package-parameters $vsParams

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    Write-Host "Visual Studio Build Tools package is registered but vswhere is missing; forcing repair install"
    choco install -y visualstudio2022buildtools --force --source $chocoSource --no-progress `
        --package-parameters $vsParams
}

Write-Host "==> Installing CMake/Ninja/JDK 17"
choco install -y cmake ninja netfx-4.8-devpack openjdk17 --source $chocoSource --no-progress
$jdk17Home = Find-Jdk17
if (-not $jdk17Home) { throw "JDK 17 with jpackage was not found after installing openjdk17" }
[Environment]::SetEnvironmentVariable("JAVA_HOME", $jdk17Home, "Machine")
[Environment]::SetEnvironmentVariable("JAVA17_HOME", $jdk17Home, "Machine")
[Environment]::SetEnvironmentVariable("RB_JAVA17_HOME", $jdk17Home, "Machine")
Prepend-MachinePath (Join-Path $jdk17Home "bin")
$env:JAVA_HOME = $jdk17Home
$env:JAVA17_HOME = $jdk17Home
$env:RB_JAVA17_HOME = $jdk17Home

foreach ($path in "C:\ProgramData\chocolatey\bin", "C:\Program Files\CMake\bin") {
    Add-MachinePath $path
}
Refresh-Path

Assert-VsBuildTools
Assert-Command cmake
Assert-Command java
Assert-Command javac
Assert-Command ninja

Write-Host "Windows build tools repair complete"
