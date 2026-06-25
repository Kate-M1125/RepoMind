"""
评估入口：
  python eval/run_eval.py                           # 默认评估 fastapi/fastapi，10 个 Issue
  python eval/run_eval.py --max 20                  # 最多 20 个
  python eval/run_eval.py --owner pallets --repo flask --max 10
  python eval/run_eval.py --report-only             # 只生成报告（不重新跑分析）
  python eval/run_eval.py --report-only --all       # 汇总所有仓库的报告

结果文件按仓库隔离：eval/results_fastapi_fastapi.json、eval/results_pallets_flask.json ...
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from eval.dataset import fetch_closed_issues_with_prs
from eval.runner import run_evaluation
from eval.report import generate_report


def _results_path(owner: str, repo: str) -> str:
    return f"eval/results_{owner}_{repo}.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner",       default="fastapi")
    parser.add_argument("--repo",        default="fastapi")
    parser.add_argument("--max",         type=int, default=10)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--all",         action="store_true",
                        help="report-only 时汇总所有仓库")
    args = parser.parse_args()

    if args.report_only:
        if args.all:
            # 汇总所有存在的 results_*.json
            result_files = sorted(Path("eval").glob("results_*.json"))
            if not result_files:
                print("没有找到任何评估结果文件。")
                return
            for f in result_files:
                print(f"\n{'='*60}")
                print(f"仓库: {f.stem.replace('results_', '').replace('_', '/', 1)}")
                print(generate_report(str(f)))
        else:
            results = _results_path(args.owner, args.repo)
            print(generate_report(results))
        return

    results = _results_path(args.owner, args.repo)

    print(f"▶ 拉取 {args.owner}/{args.repo} 已关闭 Bug Issue（最多 {args.max} 条）...")
    dataset = fetch_closed_issues_with_prs(
        owner=args.owner,
        repo=args.repo,
        max_issues=args.max,
    )
    print(f"  → 找到 {len(dataset)} 条有 Fix PR 的 Issue\n")

    if not dataset:
        print("没有找到符合条件的 Issue，退出。")
        return

    print("▶ 开始评估（RepoMind 分析 → LLM Judge 打分）...")
    run_evaluation(dataset, results_path=results)

    print("\n▶ 生成报告...")
    report = generate_report(results)
    print(report)

    report_path = Path(results).with_suffix(".txt")
    report_path.write_text(report)
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
