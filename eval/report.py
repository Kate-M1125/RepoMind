"""
评估报告生成：读取 results.json，输出汇总指标和逐条明细。
"""
import json
from pathlib import Path


def generate_report(results_path: str = "eval/results.json") -> str:
    results = json.loads(Path(results_path).read_text())
    if not results:
        return "无评估结果。"

    n = len(results)
    dims = ["root_cause_accuracy", "file_accuracy", "severity_fit", "fix_actionability", "overall"]

    # 汇总均值
    avgs = {}
    for dim in dims:
        vals = [r["scores"].get(dim, 0) for r in results if "scores" in r]
        avgs[dim] = sum(vals) / len(vals) if vals else 0.0

    # 置信度均值
    conf_vals = [r["ai_result"].get("confidence", 0) for r in results if "ai_result" in r]
    avg_conf = sum(conf_vals) / len(conf_vals) if conf_vals else 0.0

    # 分数段分布
    def bucket(score: float) -> str:
        if score >= 0.8: return "优秀(≥0.8)"
        if score >= 0.6: return "良好(0.6-0.8)"
        if score >= 0.4: return "一般(0.4-0.6)"
        return "较差(<0.4)"

    overall_scores = [r["scores"].get("overall", 0) for r in results if "scores" in r]
    buckets = {"优秀(≥0.8)": 0, "良好(0.6-0.8)": 0, "一般(0.4-0.6)": 0, "较差(<0.4)": 0}
    for s in overall_scores:
        buckets[bucket(s)] += 1

    lines = [
        "=" * 60,
        "RepoMind LLM Judge 评估报告",
        "=" * 60,
        f"评估样本数: {n}",
        f"平均置信度: {avg_conf:.2f}",
        "",
        "── 各维度平均分 ──────────────────────────",
        f"  根因准确度    (root_cause_accuracy):  {avgs['root_cause_accuracy']:.2f}",
        f"  文件命中率    (file_accuracy):         {avgs['file_accuracy']:.2f}",
        f"  严重程度合理性 (severity_fit):          {avgs['severity_fit']:.2f}",
        f"  修复可执行性  (fix_actionability):     {avgs['fix_actionability']:.2f}",
        f"  综合评分      (overall):               {avgs['overall']:.2f}",
        "",
        "── 综合评分分布 ─────────────────────────",
    ]
    for label, count in buckets.items():
        bar = "█" * count
        lines.append(f"  {label}: {bar} ({count})")

    lines += [
        "",
        "── 逐条明细 ─────────────────────────────",
    ]
    for r in sorted(results, key=lambda x: x["scores"].get("overall", 0), reverse=True):
        scores = r["scores"]
        lines.append(
            f"\n  Issue #{r['issue_number']}: {r['title'][:50]}"
        )
        lines.append(
            f"  Overall={scores.get('overall',0):.2f} | "
            f"根因={scores.get('root_cause_accuracy',0):.2f} | "
            f"文件={scores.get('file_accuracy',0):.2f} | "
            f"严重={scores.get('severity_fit',0):.2f} | "
            f"修复={scores.get('fix_actionability',0):.2f}"
        )
        if scores.get("reasoning"):
            lines.append(f"  Judge说明: {scores['reasoning'][:150]}")
        lines.append(f"  Fix PR: {r.get('pr_url','')}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_report())
