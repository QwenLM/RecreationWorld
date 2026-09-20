$mirror = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__MIRROR_URL_BASE64__')).TrimEnd('/')
$files = Get-ChildItem -Path '__REPO_DIR__' -Recurse -File -Filter 'gradle-wrapper.properties' -EA SilentlyContinue
foreach ($file in $files) {
    $text = Get-Content $file.FullName -Raw
    if ($mirror) {
        $text = $text.Replace('https\://services.gradle.org/distributions/', ($mirror + '/'))
        $text = $text.Replace('https://services.gradle.org/distributions/', ($mirror + '/'))
    }
    if ($text -match '(?m)^networkTimeout=') {
        $text = $text -replace '(?m)^networkTimeout=.*$', 'networkTimeout=600000'
    } else {
        $text = $text.TrimEnd() + [Environment]::NewLine + 'networkTimeout=600000' + [Environment]::NewLine
    }
    [System.IO.File]::WriteAllText($file.FullName, $text)
}
