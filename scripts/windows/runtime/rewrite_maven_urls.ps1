$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__BUILD_PATH_BASE64__'))
$mirror = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__MIRROR_URL_BASE64__')).TrimEnd('/')
$text = Get-Content $path -Raw
$text = $text.Replace('https://repo1.maven.org/maven2', $mirror)
$text = $text.Replace('https://repo.maven.apache.org/maven2', $mirror)
[System.IO.File]::WriteAllText($path, $text)
