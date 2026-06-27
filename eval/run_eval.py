"""
评估入口：
  python eval/run_eval.py                                      # bug eval，fastapi/fastapi，10 条
  python eval/run_eval.py --type feature                       # feature eval
  python eval/run_eval.py --owner scrapy --repo scrapy --max 15
  python eval/run_eval.py --type feature --owner pydantic --repo pydantic
  python eval/run_eval.py --report-only                        # 只生成报告
  python eval/run_eval.py --report-only --all                  # 汇总所有仓库

结果文件格式：eval/results_{type}_{owner}_{repo}.json
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from eval.dataset import (
    fetch_closed_issues_with_prs,
    fetch_feature_issues_with_prs,
    fetch_question_issues_with_answers,
)
from eval.runner import run_evaluation
from eval.report import generate_report


def _results_path(owner: str, repo: str, eval_type: str = "bug") -> str:
    return f"eval/results_{eval_type}_{owner}_{repo}.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner",       default="fastapi")
    parser.add_argument("--repo",        default="fastapi")
    parser.add_argument("--max",         type=int, default=10)
    parser.add_argument("--type",        default="bug", choices=["bug", "feature", "question"],
                        help="评估类型：bug（默认）、feature 或 question")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--all",         action="store_true",
                        help="report-only 时汇总所有仓库")
    args = parser.parse_args()

    if args.report_only:
        if args.all:
            result_files = sorted(Path("eval").glob("results_*.json"))
            if not result_files:
                print("没有找到任何评估结果文件。")
                return
            for f in result_files:
                print(f"\n{'='*60}")
                print(f"文件: {f.name}")
                print(generate_report(str(f)))
        else:
            results = _results_path(args.owner, args.repo, args.type)
            print(generate_report(results))
        return

    results = _results_path(args.owner, args.repo, args.type)

    if args.type == "feature":
        print(f"▶ 拉取 {args.owner}/{args.repo} Feature Issue（最多 {args.max} 条）...")
        dataset = fetch_feature_issues_with_prs(
            owner=args.owner,
            repo=args.repo,
            max_issues=args.max,
        )
    elif args.type == "question":
        print(f"▶ 拉取 {args.owner}/{args.repo} Question Issue（最多 {args.max} 条）...")
        dataset = fetch_question_issues_with_answers(
            owner=args.owner,
            repo=args.repo,
            max_issues=args.max,
        )
    else:
        print(f"▶ 拉取 {args.owner}/{args.repo} Bug Issue（最多 {args.max} 条）...")
        dataset = fetch_closed_issues_with_prs(
            owner=args.owner,
            repo=args.repo,
            max_issues=args.max,
        )

    print(f"  → 找到 {len(dataset)} 条有 PR 的 Issue\n")

    if not dataset:
        print("没有找到符合条件的 Issue，退出。")
        return

    print(f"▶ 开始评估（RepoMind 分析 → LLM Judge 打分，type={args.type}）...")
    run_evaluation(dataset, results_path=results, eval_type=args.type)

    print("\n▶ 生成报告...")
    report = generate_report(results)
    print(report)

    report_path = Path(results).with_suffix(".txt")
    report_path.write_text(report)
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
