[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Assert-NetFx48ReferenceAssemblies {
    $ref = Join-Path ${env:ProgramFiles(x86)} "Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8\mscorlib.dll"
    if (-not (Test-Path $ref)) {
        throw ".NET Framework 4.8 reference assemblies not found: $ref"
    }
    Write-Host ".NET Framework 4.8 reference assemblies verified: $ref"
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vsInstaller = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vs_installer.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere.exe not found" }
if (-not (Test-Path $vsInstaller)) { throw "vs_installer.exe not found" }

$installPath = & $vswhere -latest -products * -property installationPath
if ([string]::IsNullOrWhiteSpace($installPath)) {
    throw "Visual Studio Build Tools installation path not found"
}

Write-Host "==> Adding .NET Framework 4.8 SDK/TargetingPack to Visual Studio Build Tools"
Write-Host "    VS install path: $installPath"
& $vsInstaller modify `
    --installPath "$installPath" `
    --add Microsoft.Net.Component.4.8.SDK `
    --add Microsoft.Net.Component.4.8.TargetingPack `
    --quiet --wait --norestart --nocache
if ($LASTEXITCODE -ne 0) {
    throw "vs_installer modify failed with exit code $LASTEXITCODE"
}

try {
    Assert-NetFx48ReferenceAssemblies
    exit 0
} catch {
    Write-Host "VS Installer did not materialize .NET 4.8 reference assemblies; falling back to Developer Pack offline installer."
}

$downloadUrl = "https://download.visualstudio.microsoft.com/download/pr/7afca223-55d2-470a-8edc-6a1739ae3252/c8c829444416e811be84c5765ede6148/ndp48-devpack-enu.exe"
$installer = Join-Path $env:TEMP "ndp48-devpack-enu.exe"
Write-Host "==> Downloading .NET Framework 4.8 Developer Pack"
Invoke-WebRequest -Uri $downloadUrl -OutFile $installer -UseBasicParsing
Write-Host "==> Installing .NET Framework 4.8 Developer Pack"
$p = Start-Process -FilePath $installer -ArgumentList "/install", "/quiet", "/norestart" -PassThru -Wait
if ($p.ExitCode -notin @(0, 3010)) {
    throw ".NET Framework 4.8 Developer Pack installer failed with exit code $($p.ExitCode)"
}

Assert-NetFx48ReferenceAssemblies
