$ErrorActionPreference = 'Stop'
$srcDir = Join-Path $PSScriptRoot 'src'
$binDir = Join-Path $PSScriptRoot 'bin'
New-Item -ItemType Directory -Path $binDir -Force | Out-Null
dotnet publish '__PROJECT__' -c Release -o $binDir --self-contained false -p:PublishSingleFile=false 2>&1 | Write-Host
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
