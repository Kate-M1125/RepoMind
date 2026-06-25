from typing import TypedDict, Optional


class OrchestratorState(TypedDict):
    issue_url: str
    issue_title: str
    issue_body: str
    primary_repo: str          # e.g. "fastapi/fastapi"
    relevant_repos: list[str]  # 包含主仓库 + 检测到的依赖仓库
    repo_results: list[dict]   # 每个 RepoAgent 返回的结果
    final_report: str
    error: Optional[str]


class RepoAnalysisState(TypedDict):
    """Send API 并行子任务的 state"""
    issue_url: str
    repo_name: str
    repo_agent_url: str
