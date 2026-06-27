"""
detect_existing：feature 路径专用节点。

改进版：不做预设分类，让 LLM 直接推理
"如果这个功能已存在，codebase 里应该有什么具体证据"，
然后针对性搜索，基于证据判断。

核心思路：
  搜"宾语"（缺失的那个具体东西），而不是搜"主语"（功能名词）。
"""
import json
import os
import re
import subprocess
from workflow.state import IssueState
from core.llm.client import chat
from tools.github_api import parse_issue_url


def _grep_in_path(owner: str, repo: str, pattern: str, path_hint: str = "") -> str:
    """grep 指定子路径，path_hint 为空时搜全仓库。"""
    try:
        from config import settings
        root = str(settings.clone_base / f"{owner}__{repo}")
        search_path = os.path.join(root, path_hint.strip("/")) if path_hint else root
        if not os.path.exists(search_path):
            search_path = root
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.rs",
             "--include=*.ts", "--include=*.go", "-m", "20",
             pattern, search_path],
            capture_output=True, text=True, timeout=15,
        )
        out = result.stdout.replace(root + "/", "")
        print(f"[DetectExisting] grep '{pattern}' in {owner}/{repo}/{path_hint or '.'} → {len(out.splitlines())} 行")
        return out[:2000] if out else f"未找到 '{pattern}'"
    except Exception as e:
        return f"grep 失败: {e}"


def detect_existing(state: IssueState) -> dict:
    owner, repo, _ = parse_issue_url(state["issue_url"])
    title = state["title"]
    body = state.get("body", "")[:600]

    # Step 1：LLM 推理"功能存在的证据是什么"，而不是提取关键词
    evidence_prompt = f"""Feature Request:
{title}

{body}

如果这个功能已经在代码库中完整实现了，应该能找到哪些**具体**证据？

请给出 1-3 个搜索目标。关键要求：
- term 必须是"功能本身"的标识符，而不是"功能操作对象"的名称
  错误示例：feature="给 MemoryUsage 补测试" → term="MemoryUsage"（这是对象，不是证据）
  正确示例：feature="给 MemoryUsage 补测试" → term="test_memusage" + path_hint="tests/"
  正确示例：feature="allow changing log level" → term="give_up_log_level"（新参数名）
  正确示例：feature="add httpx support" → term="HttpxDownloadHandler"（新类名）
- path_hint 要精确，测试相关找 tests/，新模块找对应子目录

只输出 JSON 数组：
[{{"what": "一句话描述需要存在的东西", "term": "grep搜索词", "path_hint": "子目录路径或空字符串"}}]
只输出数组，不要解释。"""

    targets: list[dict] = []
    try:
        raw = chat("你是代码库分析专家。只输出 JSON。", evidence_prompt, max_tokens=300)
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            targets = [t for t in parsed if isinstance(t, dict) and t.get("term")]
    except Exception:
        pass

    if not targets:
        return {"feature_exists": False, "existing_api": ""}

    # Step 2：按目标精确搜索
    grep_results = []
    for t in targets[:3]:
        term = t.get("term", "")
        path_hint = t.get("path_hint", "")
        what = t.get("what", "")
        out = _grep_in_path(owner, repo, term, path_hint)
        found = "未找到" not in out and "grep 失败" not in out
        grep_results.append(
            f"[目标] {what}\n[搜索] term='{term}' path='{path_hint or '全局'}'\n"
            f"[结果] {'找到' if found else '未找到'}\n{out[:500] if found else ''}"
        )

    # Step 3：LLM 基于"证据"而非"相关代码"做判断
    judge_prompt = f"""Feature Request: {title}
{body[:300]}

以下是针对"功能是否已实现"的精确搜索结果：

{'---'.join(grep_results)}

判断标准（严格）：
- 只有找到了"该功能核心实现"的直接证据才算已存在
- 找到"相关但不同"的代码 = 未实现（例：找到类但没找到测试 = 测试未实现）
- 找到"部分实现"= 未实现

输出 JSON: {{"exists": true/false, "usage": "若已存在，一句话描述用法；否则为空"}}
只输出 JSON。"""

    try:
        raw = chat("你是代码审查专家，判断严格。只输出 JSON。", judge_prompt, max_tokens=150)
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            exists = bool(result.get("exists", False))
            usage = result.get("usage", "") if exists else ""
            print(f"[DetectExisting] 功能{'已存在' if exists else '不存在'} | 搜索: {[t.get('term') for t in targets]}")
            return {"feature_exists": exists, "existing_api": usage}
    except Exception:
        pass

    return {"feature_exists": False, "existing_api": ""}
