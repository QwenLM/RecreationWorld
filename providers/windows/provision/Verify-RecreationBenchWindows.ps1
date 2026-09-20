$ErrorActionPreference = "Stop"

$persistedPath = @(
    [Environment]::GetEnvironmentVariable("Path", "Machine")
    [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path
) -join ";"
$env:Path = (($persistedPath -split ";" | Where-Object { $_ }) | Select-Object -Unique) -join ";"

$commands = @("python", "node", "npm", "pnpm", "yarn", "git", "dotnet", "cmake", "ninja", "nuget", "mvn", "ffmpeg", "java", "javac", "qmake", "claude", "codex", "qwen-cua-driver")
$versionArguments = @{
    "nuget"  = "help"
    "ffmpeg" = "-version"
}
if ($env:RB_VERIFY_LAZARUS -eq "1") {
    $commands += @("lazbuild")
}
if ($env:RB_VERIFY_EXTRA_TOOLCHAINS -eq "1") {
    $commands += @("flutter", "go", "rustc", "cargo")
}
foreach ($command in $commands) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "missing command: $command" }
    Write-Host "[$command]"
    $versionArgument = if ($versionArguments.ContainsKey($command)) { $versionArguments[$command] } else { "--version" }
    $LASTEXITCODE = 0
    & $command $versionArgument
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "$command version check failed with exit code $exitCode" }
}

$qt5Dir = "C:\Qt\5.15.2\msvc2019_64"
$qt5Qmake = Join-Path $qt5Dir "bin\qmake.exe"
$qt5WebEngineWidgetsConfig = Join-Path $qt5Dir "lib\cmake\Qt5WebEngineWidgets\Qt5WebEngineWidgetsConfig.cmake"
if (-not (Test-Path $qt5Qmake)) { throw "Qt 5.15.2 qmake is missing: $qt5Qmake" }
if (-not (Test-Path $qt5WebEngineWidgetsConfig)) {
    throw "Qt 5.15.2 WebEngineWidgets is missing: $qt5WebEngineWidgetsConfig"
}
Write-Host "Qt 5.15.2 WebEngineWidgets: ok"

$modules = @'
import importlib.util
names = ["pywinauto", "psutil", "PIL", "pyperclip", "yaml", "openai", "PyInstaller", "PyQt5", "darkdetect", "aqt", "wx", "numpy", "OpenGL", "pyautogui", "pynput", "requests", "pytest"]
missing = [name for name in names if importlib.util.find_spec(name) is None]
if missing: raise SystemExit(f"missing modules: {missing}")
print("python modules: ok")
'@
$modules | & "C:\Python312\python.exe" -
if ($LASTEXITCODE -ne 0) { throw "Python module check failed with exit code $LASTEXITCODE" }

$javaVersion = (& javac -version 2>&1 | Out-String)
if ($javaVersion -notmatch "(?m)^\s*javac\s+17(?:\.|\s|$)") {
    throw "JDK 17 is required on PATH, got: $javaVersion"
}
$jdk17Home = $env:JAVA17_HOME
if (-not $jdk17Home) {
    $jdk17Home = [Environment]::GetEnvironmentVariable("JAVA17_HOME", "Machine")
}
$jdk17Javac = if ($jdk17Home) { Join-Path $jdk17Home "bin\javac.exe" } else { "" }
$jdk17Jpackage = if ($jdk17Home) { Join-Path $jdk17Home "bin\jpackage.exe" } else { "" }
if (-not $jdk17Javac -or -not (Test-Path $jdk17Javac) -or -not (Test-Path $jdk17Jpackage)) {
    throw "JDK 17 with jpackage is required; JAVA17_HOME is missing or invalid: $jdk17Home"
}
$jdk17Version = (& $jdk17Javac -version 2>&1 | Out-String)
if ($jdk17Version -notmatch "(?m)^\s*javac\s+17(?:\.|\s|$)") {
    throw "JAVA17_HOME must point to JDK 17, got: $jdk17Version"
}
Write-Host "JDK 17: $jdk17Home"
Write-Host "JDK 17 default: $env:JAVA_HOME"
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere.exe missing" }
$msbuild = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" | Select-Object -First 1
if (-not $msbuild -or -not (Test-Path $msbuild)) { throw "MSBuild missing from Visual Studio Build Tools" }
Write-Host "MSBuild: $msbuild"

Get-Service sshd | Format-Table -AutoSize Name, Status, StartType
if (-not (Get-Process -Name "qwen-cua-driver", "cua-driver" -ErrorAction SilentlyContinue)) {
    qwen-cua-driver autostart kick | Out-Null
    Start-Sleep -Seconds 2
}
if (-not (Get-Process -Name "qwen-cua-driver", "cua-driver" -ErrorAction SilentlyContinue)) { throw "CUA driver daemon is not running" }
Get-LocalUser -Name rbagent | Format-Table -AutoSize Name, Enabled
Write-Host "windows environment: ok"
