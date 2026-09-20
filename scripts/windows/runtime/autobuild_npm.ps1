$ErrorActionPreference = 'Stop'
$srcDir = Join-Path $PSScriptRoot 'src'
$binDir = Join-Path $PSScriptRoot 'bin'
Push-Location (Join-Path $srcDir '__PACKAGE_DIR__')
npm install 2>&1 | Write-Host
npm run build 2>&1 | Write-Host
Pop-Location
New-Item -ItemType Directory -Path $binDir -Force | Out-Null
