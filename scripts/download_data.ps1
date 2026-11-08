# 下载 Kaikki 俄语词库（约 90MB 压缩），支持断点续传
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$url = "https://kaikki.org/dictionary/Russian/kaikki.org-dictionary-Russian.jsonl.gz"
$out = Join-Path $dataDir "ru.jsonl.gz"
Write-Host "下载中：$url"
& curl.exe -L --retry 5 --retry-delay 3 -C - -o $out $url
if ($LASTEXITCODE -ne 0) { throw "下载失败，curl 退出码 $LASTEXITCODE" }
Get-Item -LiteralPath $out | Select-Object FullName, Length
Write-Host "下一步：python scripts\build_db.py --input data\ru.jsonl.gz --output data\ru_dict.db"
