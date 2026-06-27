#!/bin/bash
# 综合多仓库评估：bug + 多域 + 专业/非专业用户
set -e
cd /Users/briangao/CodeSpace/RepoMind
source .venv/bin/activate

repos=(
  # 数据验证，专业用户，含跨 Rust dep
  "pydantic pydantic 8"
  # Web 框架，混合用户
  "fastapi fastapi 8"
  "pallets flask 8"
  # 网络库，大量非专业 reporter
  "psf requests 8"
  # 爬虫，中级用户
  "scrapy scrapy 8"
  # 异步 HTTP
  "aio-libs aiohttp 8"
  # 任务队列，专业用户
  "celery celery 8"
  # 类型检查，高度专业
  "python mypy 6"
  # 代码格式化
  "psf black 6"
)

for entry in "${repos[@]}"; do
  owner=$(echo $entry | awk '{print $1}')
  repo=$(echo $entry | awk '{print $2}')
  max=$(echo $entry | awk '{print $3}')
  echo ""
  echo "========================================"
  echo "▶ 评估: $owner/$repo (max=$max)"
  echo "========================================"
  python eval/run_eval.py --owner "$owner" --repo "$repo" --max "$max" || {
    echo "⚠ $owner/$repo 评估失败，继续下一个"
    continue
  }
done

echo ""
echo "========================================"
echo "✅ 全部评估完成，汇总报告："
echo "========================================"
python eval/run_eval.py --report-only --all
