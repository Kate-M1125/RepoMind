"""
评估报告生成：读取 results.json，输出汇总指标和逐条明细。
"""
import json
from pathlib import Path


def _detect_eval_type(results: list[dict]) -> str:
    for r in results:
        t = r.get("eval_type")
        if t:
            return t
        scores = r.get("scores", {})
        if "requirement_understanding" in scores:
            return "feature"
        if "confusion_identified" in scores:
            return "question"
    return "bug"


def generate_report(results_path: str = "eval/results.json") -> str:
    results = json.loads(Path(results_path).read_text())
    if not results:
        return "无评估结果。"

    etype = _detect_eval_type(results)
    if etype == "feature":
        return _generate_feature_report(results)
    if etype == "question":
        return _generate_question_report(results)
    return _generate_bug_report(results)


def _generate_bug_report(results: list[dict]) -> str:
    n = len(results)
    dims = ["root_cause_accuracy", "file_accuracy", "severity_fit", "fix_actionability", "overall"]

    avgs = {}
    for dim in dims:
        vals = [r["scores"].get(dim, 0) for r in results if "scores" in r]
        avgs[dim] = sum(vals) / len(vals) if vals else 0.0

    conf_vals = [r["ai_result"].get("confidence", 0) for r in results if "ai_result" in r]
    avg_conf = sum(conf_vals) / len(conf_vals) if conf_vals else 0.0

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
        "RepoMind Bug 分析评估报告",
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

    lines += ["", "── 逐条明细 ─────────────────────────────"]
    for r in sorted(results, key=lambda x: x["scores"].get("overall", 0), reverse=True):
        scores = r["scores"]
        lines.append(f"\n  Issue #{r['issue_number']}: {r['title'][:50]}")
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


def _generate_feature_report(results: list[dict]) -> str:
    n = len(results)
    dims = ["requirement_understanding", "architecture_fit",
            "implementation_feasibility", "exists_detection_accuracy", "overall"]

    avgs = {}
    for dim in dims:
        vals = [r["scores"].get(dim, 0) for r in results if "scores" in r]
        avgs[dim] = sum(vals) / len(vals) if vals else 0.0

    conf_vals = [r["ai_result"].get("confidence", 0) for r in results if "ai_result" in r]
    avg_conf = sum(conf_vals) / len(conf_vals) if conf_vals else 0.0

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
        "RepoMind Feature 分析评估报告",
        "=" * 60,
        f"评估样本数: {n}",
        f"平均置信度: {avg_conf:.2f}",
        "",
        "── 各维度平均分 ──────────────────────────",
        f"  需求理解      (requirement_understanding):    {avgs['requirement_understanding']:.2f}",
        f"  架构适配      (architecture_fit):             {avgs['architecture_fit']:.2f}",
        f"  方案可行性    (implementation_feasibility):   {avgs['implementation_feasibility']:.2f}",
        f"  已存在检测    (exists_detection_accuracy):    {avgs['exists_detection_accuracy']:.2f}",
        f"  综合评分      (overall):                      {avgs['overall']:.2f}",
        "",
        "── 综合评分分布 ─────────────────────────",
    ]
    for label, count in buckets.items():
        bar = "█" * count
        lines.append(f"  {label}: {bar} ({count})")

    lines += ["", "── 逐条明细 ─────────────────────────────"]
    for r in sorted(results, key=lambda x: x["scores"].get("overall", 0), reverse=True):
        scores = r["scores"]
        ai = r.get("ai_result", {})
        lines.append(f"\n  Issue #{r['issue_number']}: {r['title'][:50]}")
        lines.append(
            f"  Overall={scores.get('overall',0):.2f} | "
            f"需求={scores.get('requirement_understanding',0):.2f} | "
            f"架构={scores.get('architecture_fit',0):.2f} | "
            f"可行性={scores.get('implementation_feasibility',0):.2f} | "
            f"已存在检测={scores.get('exists_detection_accuracy',0):.2f}"
        )
        if ai.get("feature_exists"):
            lines.append(f"  detect_existing: 已存在 → {ai.get('existing_api','')[:80]}")
        if scores.get("reasoning"):
            lines.append(f"  Judge说明: {scores['reasoning'][:150]}")
        lines.append(f"  实现 PR: {r.get('pr_url','')}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def _generate_question_report(results: list[dict]) -> str:
    n = len(results)
    dims = ["confusion_identified", "answer_accuracy", "actionability", "overall"]

    avgs = {}
    for dim in dims:
        vals = [r["scores"].get(dim, 0) for r in results if "scores" in r]
        avgs[dim] = sum(vals) / len(vals) if vals else 0.0

    conf_vals = [r["ai_result"].get("confidence", 0) for r in results if "ai_result" in r]
    avg_conf = sum(conf_vals) / len(conf_vals) if conf_vals else 0.0

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
        "RepoMind Question 回答评估报告",
        "=" * 60,
        f"评估样本数: {n}",
        f"平均置信度: {avg_conf:.2f}",
        "",
        "── 各维度平均分 ──────────────────────────",
        f"  问题识别    (confusion_identified): {avgs['confusion_identified']:.2f}",
        f"  技术准确度  (answer_accuracy):      {avgs['answer_accuracy']:.2f}",
        f"  可操作性    (actionability):        {avgs['actionability']:.2f}",
        f"  综合评分    (overall):              {avgs['overall']:.2f}",
        "",
        "── 综合评分分布 ─────────────────────────",
    ]
    for label, count in buckets.items():
        bar = "█" * count
        lines.append(f"  {label}: {bar} ({count})")

    lines += ["", "── 逐条明细 ─────────────────────────────"]
    for r in sorted(results, key=lambda x: x["scores"].get("overall", 0), reverse=True):
        scores = r["scores"]
        lines.append(f"\n  Issue #{r['issue_number']}: {r['title'][:50]}")
        lines.append(
            f"  Overall={scores.get('overall',0):.2f} | "
            f"问题识别={scores.get('confusion_identified',0):.2f} | "
            f"技术准确={scores.get('answer_accuracy',0):.2f} | "
            f"可操作={scores.get('actionability',0):.2f}"
        )
        if scores.get("reasoning"):
            lines.append(f"  Judge说明: {scores['reasoning'][:150]}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_report())
