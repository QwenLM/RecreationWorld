[CmdletBinding()]
param(
    [switch]$SkipVerify,
    [switch]$SkipMaui,
    [switch]$InstallDotnet10,
    [switch]$InstallLazarus,
    [switch]$InstallExtraToolchains
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated Administrator PowerShell session"
}

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

function Get-JdkMajorVersion([string]$JdkHome) {
    if ([string]::IsNullOrWhiteSpace($JdkHome)) { return $null }
    $javac = Join-Path $JdkHome "bin\javac.exe"
    $jpackage = Join-Path $JdkHome "bin\jpackage.exe"
    if (-not (Test-Path $javac) -or -not (Test-Path $jpackage)) { return $null }
    try {
        $version = (& $javac -version 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $version -match "(?m)^\s*javac\s+([0-9]+)(?:\.|\s|$)") {
            return [int]$Matches[1]
        }
    } catch {}
    return $null
}

function Find-JdkHome([int]$Major) {
    $candidates = @(
        [Environment]::GetEnvironmentVariable("JAVA${Major}_HOME", "Machine"),
        [Environment]::GetEnvironmentVariable("RB_JAVA${Major}_HOME", "Machine"),
        $env:JAVA_HOME
    )
    foreach ($root in @(
        "C:\Program Files\Eclipse Adoptium",
        "C:\Program Files\Microsoft",
        "C:\Program Files\Java",
        "C:\Program Files\OpenJDK"
    )) {
        if (Test-Path $root) {
            $candidates += $root
            $candidates += @(Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName)
        }
    }
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if ((Get-JdkMajorVersion $candidate) -eq $Major) { return $candidate }
    }
    return $null
}

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found on PATH: $Name"
    }
}

function Test-PendingReboot {
    $registryMarkers = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"
    )
    foreach ($marker in $registryMarkers) {
        if (Test-Path $marker) { return $true }
    }
    $sessionManager = Get-ItemProperty `
        "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager" `
        -Name PendingFileRenameOperations -ErrorAction SilentlyContinue
    return $null -ne $sessionManager
}

function Test-Python312 {
    $python = "C:\Python312\python.exe"
    if (-not (Test-Path $python)) { return $false }
    & $python -m pip --version *> $null
    return $LASTEXITCODE -eq 0
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

function Install-ChocoPackage([string]$Package, [int]$TimeoutSeconds = 1800, [string]$ExtraArgs = "") {
    Write-Host "==> choco install $Package"
    $installed = (& choco list --local-only --exact --limit-output $Package 2>$null | Select-String -Pattern "^$([regex]::Escape($Package))\|" -Quiet)
    if ($installed) {
        Write-Host "    $Package already installed; skipping"
        return
    }
    $logDir = "C:\rb_pipeline\windows_provision_logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stdout = Join-Path $logDir "$Package.stdout.log"
    $stderr = Join-Path $logDir "$Package.stderr.log"
    Remove-Item -Force $stdout, $stderr -EA SilentlyContinue
    $argLine = "install -y $Package --source $chocoSource --no-progress $ExtraArgs"
    $p = Start-Process -FilePath "choco.exe" -ArgumentList $argLine `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
        -PassThru -NoNewWindow
    if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
        try { Stop-Process -Id $p.Id -Force -EA SilentlyContinue } catch {}
        Write-Host "choco install $Package timed out after ${TimeoutSeconds}s"
        if (Test-Path $stdout) { Get-Content $stdout -Tail 80 | ForEach-Object { Write-Host $_ } }
        if (Test-Path $stderr) { Get-Content $stderr -Tail 80 | ForEach-Object { Write-Host $_ } }
        throw "choco install timeout: $Package"
    }
    # Refresh after WaitForExit so PowerShell 5.1 exposes the native process exit code reliably.
    # A missing exit code is not success: treating it as zero previously let a failed Python MSI
    # installation continue until the much less useful "No module named pip" error.
    $p.WaitForExit()
    $p.Refresh()
    if ($null -eq $p.ExitCode) {
        throw "choco install $Package completed without a readable exit code"
    }
    $exitCode = [int]$p.ExitCode
    if ($exitCode -ne 0) {
        Write-Host "choco install $Package failed with exit $exitCode"
        if (Test-Path $stdout) { Get-Content $stdout -Tail 120 | ForEach-Object { Write-Host $_ } }
        if (Test-Path $stderr) { Get-Content $stderr -Tail 120 | ForEach-Object { Write-Host $_ } }
        throw "choco install failed: $Package"
    }
    if (Test-Path $stdout) { Get-Content $stdout -Tail 20 | ForEach-Object { Write-Host $_ } }
}

Write-Host "==> Enabling OpenSSH Server"
$sshdCapability = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue
if (-not (Get-Service sshd -ErrorAction SilentlyContinue) -and $sshdCapability.State -ne "Installed") {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
}
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}

Write-Host "==> Installing Chocolatey and base toolchains"
if (-not (Get-Command choco.exe -ErrorAction SilentlyContinue)) {
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))
}
Refresh-Path
$chocoSource = "https://community.chocolatey.org/api/v2/"
$basePackages = @("git", "7zip", "nodejs-lts", "openjdk17")
if ($InstallLazarus) {
    $basePackages += @("lazarus")
}
if ($InstallExtraToolchains) {
    $basePackages += @("golang", "rust", "flutter")
}
foreach ($pkg in $basePackages) {
    Install-ChocoPackage $pkg 1800
    Refresh-Path
}

if (-not (Test-Python312)) {
    if (Test-PendingReboot) {
        throw "A Windows reboot is pending. Reboot before installing Python 3.12, then rerun this script."
    }
    if (Test-Path "C:\Python312\python.exe") {
        throw "C:\Python312 contains an incomplete Python 3.12 installation. Uninstall the existing per-user Python 3.12, remove or archive the residual C:\Python312 directory, reboot, and rerun this script."
    }
    Install-ChocoPackage "python312" 1800
    Refresh-Path
}
if (-not (Test-Python312)) {
    throw "Python 3.12 installation is incomplete: C:\Python312\python.exe cannot import both encodings and pip."
}

$npmPrefix = "C:\npm-global"
New-Item -ItemType Directory -Force -Path $npmPrefix, "C:\m2_repo", "C:\gradle_cache", "C:\nuget_packages", "C:\cargo", "C:\rb_pipeline" | Out-Null
Refresh-Path
npm config set prefix $npmPrefix
npm config set registry "https://registry.npmjs.org/"
foreach ($path in "C:\Python312", "C:\Python312\Scripts", "C:\Program Files\nodejs", $npmPrefix, "C:\ProgramData\chocolatey\bin", "C:\Program Files\Git\cmd", "C:\Program Files\7-Zip", "C:\Program Files\Go\bin", "C:\cargo\bin") { Add-MachinePath $path }
[Environment]::SetEnvironmentVariable("MAVEN_OPTS", "-Dmaven.repo.local=C:\m2_repo", "Machine")
[Environment]::SetEnvironmentVariable("GRADLE_USER_HOME", "C:\gradle_cache", "Machine")
[Environment]::SetEnvironmentVariable("NUGET_PACKAGES", "C:\nuget_packages", "Machine")
[Environment]::SetEnvironmentVariable("CARGO_HOME", "C:\cargo", "Machine")
Refresh-Path

Write-Host "==> Installing Visual Studio and .NET toolchains"
Install-ChocoPackage "visualstudio2022buildtools" 3600 '--package-parameters "--add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Workload.ManagedDesktopBuildTools --add Microsoft.VisualStudio.Component.VC.ATLMFC --add Microsoft.VisualStudio.Component.Windows10SDK.19041 --add Microsoft.Net.Component.4.8.SDK --add Microsoft.Net.Component.4.8.TargetingPack --includeRecommended --passive --locale en-US"'
foreach ($pkg in @("dotnet-8.0-sdk", "netfx-4.7.2-devpack", "netfx-4.8-devpack", "netfx-4.8.1-devpack", "cmake", "ninja", "nuget.commandline", "maven", "ffmpeg", "netfx-4.6.1-devpack")) {
    Install-ChocoPackage $pkg 2400
    Refresh-Path
}
$jdk17Home = Find-JdkHome 17
if (-not $jdk17Home) { throw "JDK 17 with jpackage was not found after installing openjdk17" }
[Environment]::SetEnvironmentVariable("JAVA17_HOME", $jdk17Home, "Machine")
[Environment]::SetEnvironmentVariable("RB_JAVA17_HOME", $jdk17Home, "Machine")
[Environment]::SetEnvironmentVariable("JAVA_HOME", $jdk17Home, "Machine")
Prepend-MachinePath (Join-Path $jdk17Home "bin")
$env:JAVA17_HOME = $jdk17Home
$env:RB_JAVA17_HOME = $jdk17Home
$env:JAVA_HOME = $jdk17Home
Refresh-Path
Write-Host "JDK 17 selected as default: $jdk17Home"
Assert-VsBuildTools
Assert-Command cmake
Assert-Command ninja
if (-not $SkipMaui) {
    Write-Host "==> Installing .NET MAUI workload"
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
    dotnet workload install maui --skip-manifest-update
}

Write-Host "==> Installing pnpm, yarn, and agent CLIs"
$pnpmVersion = if ($env:PNPM_VERSION) { $env:PNPM_VERSION } else { "10.6.2" }
$yarnVersion = if ($env:YARN_VERSION) { $env:YARN_VERSION } else { "1.22.22" }
$claudeVersion = if ($env:CLAUDE_CODE_VERSION) { $env:CLAUDE_CODE_VERSION } else { "2.1.177" }
$codexVersion = if ($env:CODEX_VERSION) { $env:CODEX_VERSION } else { "0.145.0" }
npm install -g "pnpm@$pnpmVersion" "yarn@$yarnVersion" "@anthropic-ai/claude-code@$claudeVersion" "@openai/codex@$codexVersion"

Write-Host "==> Installing fixed Python dependencies from public PyPI"
$python = if (Test-Path "C:\Python312\python.exe") { "C:\Python312\python.exe" } else { "python" }
& $python -m pip install --index-url "https://pypi.org/simple" --upgrade pip
& $python -m pip install --index-url "https://pypi.org/simple" -r (Join-Path $PSScriptRoot "requirements.windows.txt")
& $python -m pip install --index-url "https://pypi.org/simple" --upgrade aqtinstall
if ($LASTEXITCODE -ne 0) { throw "aqtinstall installation failed" }
New-Item -ItemType Directory -Force -Path "C:\Qt" | Out-Null
[Environment]::SetEnvironmentVariable("QT_ROOT", "C:\Qt", "Machine")
$qt5Dir = "C:\Qt\5.15.2\msvc2019_64"
$qt5Qmake = Join-Path $qt5Dir "bin\qmake.exe"
$qt5WebEngineWidgetsConfig = Join-Path $qt5Dir "lib\cmake\Qt5WebEngineWidgets\Qt5WebEngineWidgetsConfig.cmake"
Add-MachinePath (Join-Path $qt5Dir "bin")
Add-MachinePath "C:\Qt\6.8.3\msvc2022_64\bin"
Refresh-Path
if (-not (Test-Path $qt5Qmake) -or -not (Test-Path $qt5WebEngineWidgetsConfig)) {
    Write-Host "==> Installing Qt 5.15.2 msvc2019_64 + WebEngine via aqtinstall"
    & $python -m aqt install-qt windows desktop 5.15.2 win64_msvc2019_64 -m qtwebengine --outputdir "C:\Qt"
    if ($LASTEXITCODE -ne 0) { throw "Qt 5.15.2 installation failed" }
    Refresh-Path
}
if (-not (Test-Path $qt5Qmake)) {
    throw "Qt 5.15.2 qmake is missing after installation: $qt5Qmake"
}
if (-not (Test-Path $qt5WebEngineWidgetsConfig)) {
    throw "Qt 5.15.2 WebEngineWidgets is missing after installation: $qt5WebEngineWidgetsConfig"
}
if (-not (Test-Path "C:\Qt\6.8.3\msvc2022_64\bin\qmake.exe")) {
    Write-Host "==> Installing Qt 6.8.3 msvc2022_64 via aqtinstall"
    & $python -m aqt install-qt windows desktop 6.8.3 win64_msvc2022_64 --outputdir "C:\Qt"
    if ($LASTEXITCODE -ne 0) { throw "Qt 6.8.3 installation failed" }
    Refresh-Path
}

Write-Host "==> Installing Qwen CUA driver"
$cuaVersion = if ($env:QWEN_CUA_DRIVER_VERSION) { $env:QWEN_CUA_DRIVER_VERSION } else { "0.7.3" }
$env:CUA_DRIVER_RS_VERSION = $cuaVersion
$env:CUA_DRIVER_RS_INSTALL_DIR = "C:\Program Files\Cua\cua-driver\bin"
$env:CUA_DRIVER_RS_HOME = "C:\Program Files\Cua\cua-driver"
$cuaInstaller = "https://raw.githubusercontent.com/QwenLM/qwen-code/cua-driver-rs-v$cuaVersion/packages/cua-driver/scripts/install.ps1"
Invoke-RestMethod $cuaInstaller | Invoke-Expression
$driverDir = "C:\Program Files\Cua\cua-driver\bin"
Add-MachinePath $driverDir
$env:Path = "$driverDir;$env:Path"
[Environment]::SetEnvironmentVariable("CUA_DRIVER_RS_UPDATE_CHECK", "0", "Machine")
[Environment]::SetEnvironmentVariable("CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS", "86400", "Machine")
[Environment]::SetEnvironmentVariable("CUA_DRIVER_RS_COORDINATE_SPACE", "0", "Machine")
[Environment]::SetEnvironmentVariable("CUA_DRIVER_RS_COORDINATE_SCALE", "1000", "Machine")
[Environment]::SetEnvironmentVariable("RB_CUA_DRIVER_BINARY", "qwen-cua-driver", "Machine")
qwen-cua-driver autostart enable | Out-Null
qwen-cua-driver autostart kick | Out-Null
Assert-Command qwen-cua-driver

Write-Host "==> Creating restricted runtime account"
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#%+="
$password = "Aa1!" + (-join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] }))
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
if (Get-LocalUser -Name "rbagent" -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name "rbagent" -Password $securePassword
} else {
    New-LocalUser -Name "rbagent" -Password $securePassword -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
}
icacls "C:\rb_pipeline" /grant "rbagent:(OI)(CI)M" /Q | Out-Null

if (-not $SkipVerify) { & (Join-Path $PSScriptRoot "Verify-RecreationBenchWindows.ps1") }
Write-Host "RecreationBench Windows environment is ready"
