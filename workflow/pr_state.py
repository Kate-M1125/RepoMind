from typing import TypedDict, Optional


class PRState(TypedDict):
    # 输入
    pr_url: str

    # fetch_pr 填入
    pr_number: int
    title: str
    body: str
    author: str
    base_branch: str
    head_branch: str
    changed_files: list[dict]   # [{filename, status, additions, deletions, patch}]
    comments: list[dict]

    # analyze_* 填入
    security_issues: list[dict]  # [{severity, file, line, description, suggestion}]
    style_issues: list[dict]
    logic_issues: list[dict]

    # 共享基础设施
    project_map: str
    memory_context: str
    image_descriptions: str

    # reflect
    confidence: float
    reflection_note: str
    iteration: int

    # 输出
    final_report: str
    error: Optional[str]
