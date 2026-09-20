$ErrorActionPreference = 'Stop'
$srcDir = Join-Path $PSScriptRoot 'src'
$binDir = Join-Path $PSScriptRoot 'bin'
New-Item -ItemType Directory -Path $binDir -Force | Out-Null
msbuild '__SOLUTION__' /p:Configuration=Release /p:OutputPath=$binDir 2>&1 | Write-Host
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
