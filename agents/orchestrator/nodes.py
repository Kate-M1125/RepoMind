import os
import json
import httpx
from openai import OpenAI
from langgraph.types import Send

from agents.orchestrator.state import OrchestratorState, RepoAnalysisState
from tools.github_api import fetch_issue, resolve_repo

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

REPO_AGENT_URL = os.getenv("REPO_AGENT_URL", "http://localhost:8001")


def _chat(prompt: str, max_tokens: int = 800) -> str:
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


# ── 节点 1：拉取 Issue，检测相关依赖仓库 ──────────────────────────

def fetch_and_detect(state: OrchestratorState) -> dict:
    issue = fetch_issue(state["issue_url"])

    # 从 issue_url 解析主仓库
    # https://github.com/owner/repo/issues/123
    parts = state["issue_url"].rstrip("/").split("/")
    primary_repo = f"{parts[3]}/{parts[4]}"

    prompt = f"""分析这个 GitHub Issue，列出所有可能与根因相关的 GitHub 仓库（包括主仓库和依赖仓库）。

主仓库: {primary_repo}
Issue 标题: {issue['title']}
Issue 内容:
{issue['body'][:800]}

规则：
- 只列出真正可能包含根因代码的仓库
- 格式：每行一个，owner/repo 格式
- 最多 3 个
- 必须包含主仓库
- 如果 Issue 没有提到其他仓库，只返回主仓库

直接输出仓库列表，不要解释："""

    raw = _chat(prompt, max_tokens=100)
    repos = []
    for line in raw.strip().splitlines():
        line = line.strip().lstrip("-").strip()
        if "/" in line and len(line.split("/")) == 2:
            repos.append(line)

    # 确保主仓库在列表中且排第一
    if primary_repo in repos:
        repos.remove(primary_repo)
    repos = [primary_repo] + repos

    # 用 GitHub API 解析真实 full_name，去掉重定向导致的重复仓库
    seen = set()
    deduped = []
    for r in repos:
        canonical = resolve_repo(r)
        if canonical not in seen:
            seen.add(canonical)
            deduped.append(canonical)
    repos = deduped

    print(f"[Orchestrator] 检测到相关仓库: {repos}")

    return {
        "issue_title": issue["title"],
        "issue_body": issue["body"],
        "primary_repo": primary_repo,
        "relevant_repos": repos,
        "repo_results": [],
    }


# ── Send 分发：每个仓库派发一个并行子任务 ──────────────────────────

def dispatch_to_repo_agents(state: OrchestratorState):
    """返回 Send 列表，LangGraph 并行执行每个 repo_analysis 节点"""
    return [
        Send("repo_analysis", {
            "issue_url": state["issue_url"],
            "repo_name": repo,
            "repo_agent_url": REPO_AGENT_URL,
        })
        for repo in state["relevant_repos"]
    ]


# ── 并行子节点：调用 RepoAgent HTTP 服务 ──────────────────────────

def repo_analysis(state: RepoAnalysisState) -> dict:
    """每个 repo 独立调用 RepoAgent，结果汇入 repo_results"""
    print(f"[Orchestrator] 调用 RepoAgent: {state['repo_name']}")
    try:
        resp = httpx.post(
            f"{state['repo_agent_url']}/analyze",
            json={"issue_url": state["issue_url"], "repo_name": state["repo_name"]},
            timeout=300,
        )
        resp.raise_for_status()
        result = resp.json()
        result["status"] = "ok"
    except Exception as e:
        print(f"[Orchestrator] RepoAgent 调用失败 {state['repo_name']}: {e}")
        result = {
            "repo_name": state["repo_name"],
            "root_cause": "",
            "fix_suggestion": "",
            "confidence": 0.0,
            "final_report": "",
            "status": f"error: {e}",
        }
    # Send 子节点的返回必须用 reducer 合并到父 state
    # OrchestratorState.repo_results 用 Annotated[list, operator.add] 收集
    return {"repo_results": [result]}


# ── 节点 2：汇总所有 RepoAgent 结果，生成跨仓库报告 ──────────────

def merge_reports(state: OrchestratorState) -> dict:
    results = state["repo_results"]
    ok_results = [r for r in results if r.get("status") == "ok"]

    if not ok_results:
        return {"final_report": "所有 RepoAgent 均返回错误，无法生成报告。"}

    # 找置信度最高的
    best = max(ok_results, key=lambda r: r.get("confidence", 0))

    summaries = []
    for r in ok_results:
        summaries.append(
            f"### 仓库: {r['repo_name']} (置信度: {r.get('confidence', 0):.2f})\n"
            f"**根因**: {r.get('root_cause', '')}\n"
            f"**修复建议**: {r.get('fix_suggestion', '')}"
        )

    prompt = f"""基于以下多个仓库的分析结果，生成一份跨仓库的综合报告。

Issue: {state['issue_title']}

各仓库分析：
{'---'.join(summaries)}

要求：
1. 判断根因最可能在哪个仓库
2. 如果是上游依赖的 Bug，明确指出
3. 给出最终修复建议
4. Markdown 格式"""

    final = _chat(prompt, max_tokens=800)

    header = f"# 跨仓库分析报告\n\n**Issue**: {state['issue_title']}\n**分析仓库**: {', '.join(state['relevant_repos'])}\n**根因仓库**: {best['repo_name']}\n\n---\n\n"
    return {"final_report": header + final}
