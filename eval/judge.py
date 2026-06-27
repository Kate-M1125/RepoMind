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

# ── Feature Judge ─────────────────────────────────────────

_FEATURE_JUDGE_SYSTEM = """你是一名资深软件架构师，负责评估 AI 系统对 Feature Request 的分析质量。
你会收到：
1. 一个 GitHub Feature Request 的描述
2. AI 系统的分析结果（需求理解、建议实现位置、方案、是否已存在）
3. 实际合并的实现 PR（真实修改了哪些文件、怎么实现的）

你的任务是客观评估 AI 分析与实际实现的匹配程度。评分要严格，不要宽松。"""

_FEATURE_JUDGE_PROMPT = """请评估以下 Feature Request 分析结果的质量。

## Feature Request
标题: {title}
内容: {body}

## AI 分析结果
需求缺口分析: {root_cause}
建议实现文件: {affected_files}
实现方案: {fix_suggestion}
detect_existing 判断: {feature_exists}（已存在：{existing_api}）

## 实际实现 PR（Ground Truth）
PR 标题: {pr_title}
修改文件: {pr_diff_summary}
关键 Patch（前 500 字）:
{pr_patches}

## 评估要求

输出 JSON，包含以下字段：

- requirement_understanding (0.0-1.0): 是否正确理解了用户诉求
  - 1.0: 准确提炼了用户想要的功能和动机
  - 0.5: 大致理解但有偏差
  - 0.0: 理解错误

- architecture_fit (0.0-1.0): 建议的实现位置/模块是否与实际 PR 一致
  - 1.0: 指向的文件/模块与 PR 修改文件高度重叠
  - 0.5: 部分重叠
  - 0.0: 完全不同

- implementation_feasibility (0.0-1.0): 实现方案方向是否与实际 PR 一致
  - 1.0: 思路与实际实现高度一致，可直接参考
  - 0.5: 方向正确但细节偏差
  - 0.0: 方向错误

- exists_detection_accuracy (0.0-1.0): detect_existing 是否做出了正确判断
  - 1.0: 功能已存在时正确识别，功能不存在时正确判断为需要新增
  - 0.5: 判断正确但描述不准确
  - 0.0: 误判（该存在的说不存在，或不存在的说已存在）
  - 若 PR 新增了大量代码 → 功能原本不存在；若 PR 主要是测试/文档 → 功能可能已存在

- overall (0.0-1.0): 综合评分

- reasoning (str): 各项打分理由（3-5句话）

只输出 JSON。"""

_FEATURE_SCORE_KEYS = (
    "requirement_understanding",
    "architecture_fit",
    "implementation_feasibility",
    "exists_detection_accuracy",
    "overall",
)


def judge_feature(
    title: str,
    body: str,
    ai_result: dict,
    ground_truth: dict,
    n: int = 3,
) -> dict:
    """对单个 Feature Issue 的分析结果打分 n 次，返回均值。"""
    pr_patches = "\n---\n".join(
        f"[{f['filename']}]\n{f['patch']}"
        for f in ground_truth.get("pr_files", [])[:5]
        if f.get("patch")
    ) or "（无 patch 信息）"

    prompt = _FEATURE_JUDGE_PROMPT.format(
        title=title,
        body=body[:600],
        root_cause=ai_result.get("root_cause", ""),
        affected_files=", ".join(ai_result.get("affected_files", [])) or "未提供",
        fix_suggestion=ai_result.get("fix_suggestion", ""),
        feature_exists=ai_result.get("feature_exists", "未知"),
        existing_api=ai_result.get("existing_api", ""),
        pr_title=ground_truth.get("pr_title", ""),
        pr_diff_summary=ground_truth.get("pr_diff_summary", ""),
        pr_patches=pr_patches,
    )

    runs: list[dict] = []
    for i in range(n):
        raw = chat(_FEATURE_JUDGE_SYSTEM, prompt, max_tokens=800, json_mode=True)
        try:
            scores = parse_json(raw)
            for key in _FEATURE_SCORE_KEYS:
                if key in scores:
                    scores[key] = max(0.0, min(1.0, float(scores[key])))
            runs.append(scores)
        except Exception as e:
            print(f"  [judge_feature] 第{i+1}次解析失败: {e}")

    if not runs:
        return {k: 0.0 for k in _FEATURE_SCORE_KEYS} | {"reasoning": "Judge 全部解析失败"}

    averaged = {}
    for key in _FEATURE_SCORE_KEYS:
        vals = [r[key] for r in runs if key in r]
        averaged[key] = round(sum(vals) / len(vals), 3) if vals else 0.0
    averaged["reasoning"] = runs[-1].get("reasoning", "")
    averaged["judge_runs"] = len(runs)
    return averaged


# ── Question Judge ────────────────────────────────────────

_QUESTION_JUDGE_SYSTEM = """你是一名资深工程师，负责评估 AI 代码助手回答技术问题的质量。
你会收到：
1. 一个 GitHub "question" Issue（用户提问）
2. AI 系统对该问题的分析与解答
3. 仓库维护者/贡献者在 Issue 中给出的实际回答（ground truth）

你的任务是客观评估 AI 回答与真实解答的匹配程度。评分要严格。"""

_QUESTION_JUDGE_PROMPT = """请评估以下技术问题的 AI 回答质量。

## Issue 信息
标题: {title}
内容: {body}

## AI 分析结果
问题理解/根因: {root_cause}
解答/修复建议: {fix_suggestion}
相关文件: {affected_files}
代码示例: {code_changes}

## 维护者/贡献者的实际回答（Ground Truth）
{key_answer}

## 评估要求

输出 JSON，包含以下字段：

- confusion_identified (0.0-1.0): AI 是否正确识别了用户困惑的根源
  - 1.0: 准确指出了用户误解/不清楚的地方，与维护者诊断一致
  - 0.5: 方向正确但不够精准
  - 0.0: 完全误判了问题所在

- answer_accuracy (0.0-1.0): AI 给出的技术解释是否正确
  - 1.0: 与维护者回答完全一致，技术细节准确
  - 0.5: 大方向正确但有细节错误或遗漏
  - 0.0: 技术解释错误或与维护者答案矛盾

- actionability (0.0-1.0): AI 提供的操作指引是否实际可用
  - 1.0: 代码示例正确、步骤明确，用户可直接按此操作
  - 0.5: 有帮助但不完整，或示例有小错误
  - 0.0: 指引无法执行或具有误导性

- overall (0.0-1.0): 综合评分

- reasoning (str): 简要说明各项打分理由（3-5句话）

只输出 JSON。"""

_QUESTION_SCORE_KEYS = ("confusion_identified", "answer_accuracy", "actionability", "overall")


def judge_question(
    title: str,
    body: str,
    ai_result: dict,
    key_answer: str,
    n: int = 3,
) -> dict:
    """对单个 Question Issue 的 AI 回答打分 n 次，返回均值。

    Args:
        title:      Issue 标题
        body:       Issue 正文（前 600 字符）
        ai_result:  RepoMind 分析输出
        key_answer: 维护者的权威回答（ground truth）
        n:          重复评分次数，取均值
    """
    code_changes = ""
    if ai_result.get("code_changes"):
        cc = ai_result["code_changes"]
        if isinstance(cc, list):
            code_changes = "\n".join(str(x) for x in cc[:3])
        else:
            code_changes = str(cc)[:500]

    prompt = _QUESTION_JUDGE_PROMPT.format(
        title=title,
        body=body[:600],
        root_cause=ai_result.get("root_cause", ""),
        fix_suggestion=ai_result.get("fix_suggestion", ""),
        affected_files=", ".join(ai_result.get("affected_files", [])) or "未提供",
        code_changes=code_changes or "（未提供）",
        key_answer=key_answer[:1500],
    )

    runs: list[dict] = []
    for i in range(n):
        raw = chat(_QUESTION_JUDGE_SYSTEM, prompt, max_tokens=800, json_mode=True)
        try:
            scores = parse_json(raw)
            for key in _QUESTION_SCORE_KEYS:
                if key in scores:
                    scores[key] = max(0.0, min(1.0, float(scores[key])))
            runs.append(scores)
        except Exception as e:
            print(f"  [judge_question] 第{i+1}次解析失败: {e}")

    if not runs:
        return {k: 0.0 for k in _QUESTION_SCORE_KEYS} | {"reasoning": "Judge 全部解析失败"}

    averaged = {}
    for key in _QUESTION_SCORE_KEYS:
        vals = [r[key] for r in runs if key in r]
        averaged[key] = round(sum(vals) / len(vals), 3) if vals else 0.0
    averaged["reasoning"] = runs[-1].get("reasoning", "")
    averaged["judge_runs"] = len(runs)
    return averaged


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
