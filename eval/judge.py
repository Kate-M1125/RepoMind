"""
LLM Judge：对比 RepoMind 的分析结果 vs 真实 Fix PR，打分。

评估维度（各 0-1）：
  root_cause_accuracy  - 根因分析是否指向 PR 实际修复的问题
  file_accuracy        - 受影响文件是否与 PR 修改文件重叠
  severity_fit         - 严重程度评级是否合理
  fix_actionability    - 修复建议方向是否与实际 PR 一致
  overall              - 综合评分（Judge 综合判断）
"""
import json
from core.llm.client import chat, parse_json

_JUDGE_SYSTEM = """你是一名资深工程师，负责评估 AI 代码分析工具的输出质量。
你会收到：
1. 一个 GitHub Bug Issue 的描述
2. AI 系统对该 Issue 的分析结果（根因、受影响文件、修复建议、严重程度）
3. 实际合并的 Fix PR 信息（真实修改了哪些文件、做了什么改动）

你的任务是客观评估 AI 分析与真实修复的匹配程度。评分要严格，不要宽松。"""

_JUDGE_PROMPT = """请评估以下 AI 分析结果的质量。

## Issue 信息
标题: {title}
内容: {body}

## AI 分析结果
根因: {root_cause}
受影响文件: {affected_files}
严重程度: {severity}
修复建议: {fix_suggestion}

## 真实 Fix PR（Ground Truth）
PR 标题: {pr_title}
修改文件: {pr_diff_summary}
关键 Patch（前 500 字）:
{pr_patches}

## 评估要求

输出 JSON，包含以下字段：

- root_cause_accuracy (0.0-1.0): AI 指出的根因是否与 PR 实际修复的问题一致
  - 1.0: 完全匹配，AI 准确指出了真正的 bug 所在
  - 0.5: 方向正确但不够精确
  - 0.0: 完全错误或无关

- file_accuracy (0.0-1.0): AI 列出的受影响文件与 PR 修改文件的重叠程度
  - 1.0: 完全覆盖 PR 修改的关键文件
  - 0.5: 部分重叠
  - 0.0: 完全没有交集

- severity_fit (0.0-1.0): 严重程度评级是否合理（参考 PR 修复的紧迫程度）
  - 1.0: 评级准确
  - 0.5: 偏高或偏低一档
  - 0.0: 严重偏差

- fix_actionability (0.0-1.0): 修复建议的方向是否与实际 PR 一致
  - 1.0: 建议与 PR 方向高度一致，可直接参考
  - 0.5: 方向大致正确但细节偏差
  - 0.0: 方向错误或无法执行

- overall (0.0-1.0): 综合评分，考虑以上四个维度的加权结果

- reasoning (str): 简要说明各项打分理由（3-5句话）

只输出 JSON。"""


_SCORE_KEYS = ("root_cause_accuracy", "file_accuracy", "severity_fit", "fix_actionability", "overall")


def judge_single(
    title: str,
    body: str,
    ai_result: dict,
    ground_truth: dict,
    n: int = 3,
) -> dict:
    """对单个 Issue 的分析结果打分 n 次，返回均值。"""
    pr_patches = "\n---\n".join(
        f"[{f['filename']}]\n{f['patch']}"
        for f in ground_truth.get("pr_files", [])[:5]
        if f.get("patch")
    ) or "（无 patch 信息）"

    prompt = _JUDGE_PROMPT.format(
        title=title,
        body=body[:600],
        root_cause=ai_result.get("root_cause", ""),
        affected_files=", ".join(ai_result.get("affected_files", [])) or "未提供",
        severity=ai_result.get("severity", ""),
        fix_suggestion=ai_result.get("fix_suggestion", ""),
        pr_title=ground_truth.get("pr_title", ""),
        pr_diff_summary=ground_truth.get("pr_diff_summary", ""),
        pr_patches=pr_patches,
    )

    runs: list[dict] = []
    for i in range(n):
        raw = chat(_JUDGE_SYSTEM, prompt, max_tokens=800, json_mode=True)
        try:
            scores = parse_json(raw)
            for key in _SCORE_KEYS:
                if key in scores:
                    scores[key] = max(0.0, min(1.0, float(scores[key])))
            runs.append(scores)
        except Exception as e:
            print(f"  [judge] 第{i+1}次解析失败: {e}")

    if not runs:
        return {k: 0.0 for k in _SCORE_KEYS} | {"reasoning": "Judge 全部解析失败"}

    # 各维度取均值，reasoning 用最后一次
    averaged = {}
    for key in _SCORE_KEYS:
        vals = [r[key] for r in runs if key in r]
        averaged[key] = round(sum(vals) / len(vals), 3) if vals else 0.0
    averaged["reasoning"] = runs[-1].get("reasoning", "")
    averaged["judge_runs"] = len(runs)
    return averaged
