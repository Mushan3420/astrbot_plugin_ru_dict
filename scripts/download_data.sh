#!/usr/bin/env bash
# 下载 Kaikki 俄语词库（约 90MB 压缩），支持断点续传
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/data"
URL="https://kaikki.org/dictionary/Russian/kaikki.org-dictionary-Russian.jsonl.gz"
echo "下载 $URL"
curl -L --retry 5 --retry-delay 3 -C - -o "$ROOT/data/ru.jsonl.gz" "$URL"
ls -lh "$ROOT/data/ru.jsonl.gz"
echo "下一步：python3 scripts/build_db.py --input data/ru.jsonl.gz --output data/ru_dict.db"
