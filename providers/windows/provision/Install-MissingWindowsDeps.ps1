[CmdletBinding()]
param(
    [switch]$SkipDotnet,
    [switch]$SkipJdk17,
    [switch]$SkipMaven,
    [switch]$SkipNpm,
    [switch]$SkipPython
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

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

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Download-File([string]$Url, [string]$OutFile) {
    Write-Host "download: $Url"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutFile) | Out-Null
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
}

function Expand-ZipFresh([string]$ZipFile, [string]$Destination) {
    if (Test-Path $Destination) {
        Remove-Item $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Expand-Archive -LiteralPath $ZipFile -DestinationPath $Destination -Force
}

New-Item -ItemType Directory -Force -Path "C:\rb_pipeline\downloads", "C:\tools" | Out-Null

Add-MachinePath "C:\Qt\5.15.2\msvc2019_64\bin"
Add-MachinePath "C:\Qt\6.8.3\msvc2022_64\bin"
Add-MachinePath "C:\npm-global"
Add-MachinePath "C:\tools\nuget"
Add-MachinePath "C:\tools\apache-maven\bin"
Add-MachinePath "C:\dotnet"
Refresh-Path

if (-not $SkipPython) {
    Write-Host "==> Installing missing Python deps"
    & "C:\Python312\python.exe" -m pip install --index-url "https://pypi.org/simple" PyQt5==5.15.11 darkdetect==0.8.0
    if ($LASTEXITCODE -ne 0) { throw "pip install PyQt5/darkdetect failed" }
}

if (-not $SkipNpm) {
    Write-Host "==> Repairing npm global shims"
    npm config set prefix "C:\npm-global"
    npm config set registry "https://registry.npmjs.org/"
    npm install -g pnpm@10.6.2 yarn@1.22.22 @anthropic-ai/claude-code@2.1.177 @openai/codex@0.145.0
    if ($LASTEXITCODE -ne 0) { throw "npm global install failed" }
}

if (-not $SkipMaven) {
    Write-Host "==> Installing Maven 3.9.9 from Apache archive"
    $mavenZip = "C:\rb_pipeline\downloads\apache-maven-3.9.9-bin.zip"
    Download-File "https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.zip" $mavenZip
    Expand-ZipFresh $mavenZip "C:\tools\apache-maven-root"
    if (Test-Path "C:\tools\apache-maven") { Remove-Item "C:\tools\apache-maven" -Recurse -Force }
    Move-Item "C:\tools\apache-maven-root\apache-maven-3.9.9" "C:\tools\apache-maven"
    Remove-Item "C:\tools\apache-maven-root" -Recurse -Force -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable("MAVEN_HOME", "C:\tools\apache-maven", "Machine")
    [Environment]::SetEnvironmentVariable("MAVEN_OPTS", "-Dmaven.repo.local=C:\m2_repo", "Machine")
}

Write-Host "==> Installing NuGet CLI"
New-Item -ItemType Directory -Force -Path "C:\tools\nuget" | Out-Null
Download-File "https://dist.nuget.org/win-x86-commandline/latest/nuget.exe" "C:\tools\nuget\nuget.exe"

if (-not $SkipJdk17) {
    Write-Host "==> Installing JDK 17 from Adoptium API"
    $jdkZip = "C:\rb_pipeline\downloads\temurin-jdk17.zip"
    Download-File "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse" $jdkZip
    Expand-ZipFresh $jdkZip "C:\tools\jdk17-root"
    $jdkDir = Get-ChildItem "C:\tools\jdk17-root" -Directory | Select-Object -First 1
    if (-not $jdkDir) { throw "JDK 17 archive did not contain a directory" }
    $target = "C:\Program Files\Eclipse Adoptium\jdk-17"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Move-Item $jdkDir.FullName $target
    Remove-Item "C:\tools\jdk17-root" -Recurse -Force -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable("JAVA_HOME", $target, "Machine")
    [Environment]::SetEnvironmentVariable("JAVA17_HOME", $target, "Machine")
    [Environment]::SetEnvironmentVariable("RB_JAVA17_HOME", $target, "Machine")
    Prepend-MachinePath "$target\bin"
    $env:JAVA_HOME = $target
    $env:JAVA17_HOME = $target
    $env:RB_JAVA17_HOME = $target
}

if (-not $SkipDotnet) {
    Write-Host "==> Installing .NET 8 SDK via dotnet-install.ps1"
    $installer = "C:\rb_pipeline\downloads\dotnet-install.ps1"
    Download-File "https://dot.net/v1/dotnet-install.ps1" $installer
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Channel 8.0 -InstallDir "C:\dotnet"
    if ($LASTEXITCODE -ne 0) { throw "dotnet install failed" }
    [Environment]::SetEnvironmentVariable("DOTNET_ROOT", "C:\dotnet", "Machine")
}

Refresh-Path
Write-Host "==> Versions"
cmd.exe /d /c "qmake --version"
cmd.exe /d /c "pnpm --version"
cmd.exe /d /c "yarn --version"
cmd.exe /d /c "claude --version"
cmd.exe /d /c "codex --version"
cmd.exe /d /c "nuget help | more"
cmd.exe /d /c "mvn --version"
cmd.exe /d /c "javac -version"
cmd.exe /d /c "dotnet --list-sdks"
Write-Host "missing windows deps install completed"
