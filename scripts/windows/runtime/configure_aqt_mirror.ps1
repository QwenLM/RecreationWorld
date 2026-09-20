$mirror = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__MIRROR_URL_BASE64__')).TrimEnd('/')
if ($mirror) {
    $locationLine = python -m pip show aqtinstall 2>$null |
        Where-Object { $_ -match '^Location:\s*(.+)$' } |
        Select-Object -First 1
    $aqtIni = if ($locationLine -and $locationLine -match '^Location:\s*(.+)$') {
        Join-Path $Matches[1].Trim() 'aqt\settings.ini'
    }
    if ($aqtIni -and (Test-Path $aqtIni)) {
        (Get-Content $aqtIni) -replace 'baseurl\s*:.*https://download\.qt\.io', ('baseurl: ' + $mirror) | Set-Content $aqtIni
    }
}
