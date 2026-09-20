
if ($null -ne $cuaDaemon) {
    taskkill.exe /F /T /PID $($cuaDaemon.Id) 2>$null | Out-Null
    Stop-Process -Id $cuaDaemon.Id -Force -EA SilentlyContinue
}
