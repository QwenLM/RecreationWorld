
$targets = @(__TARGETS__)
$victims = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $proc = $_
    if ($proc.ProcessId -eq $PID) { $false }
    else {
        $hay = [string]$proc.CommandLine + '|' + [string]$proc.ExecutablePath
        $hit = $targets | Where-Object { $hay -like ('*' + $_ + '*') } | Select-Object -First 1
        $null -ne $hit
    }
})
foreach ($p in $victims) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    & taskkill.exe /F /T /PID $p.ProcessId 2>$null | Out-Null
}
if ($victims.Count -gt 0) {
    $names = $victims | ForEach-Object { $_.Name + '(pid ' + $_.ProcessId + ')' }
    Write-Output ($names -join '; ')
}
