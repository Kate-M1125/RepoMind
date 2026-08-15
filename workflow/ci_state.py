from typing import Optional, TypedDict


class CIState(TypedDict):
    """CI 工作流共享黑板；字段由各节点分阶段写入。"""

    # 用户输入
    ci_url: str

    # fetch_ci_run：URL 身份与 Actions 元数据
    owner: str
    repo: str
    run_id: int
    run_info: dict
    failed_jobs: list[dict]
    failure_logs: str
    changed_files: list[dict]

    # parse_failure_logs：从原始日志提取的结构化故障证据
    error_signatures: list[dict]
    failing_tests: list[str]
    failure_type: str

    # build_project_map / collect_ci_context：提供给分析 LLM 的仓库上下文
    project_map: str
    code_context: str

    # analyze_ci_failure：只读诊断结果
    root_cause: str
    fix_suggestion: str
    suspected_files: list[str]
    suspicious_commit: str

    # reflect_ci：独立评审与最多两次重试控制
    confidence: float
    reflection_note: str
    iteration: int

    # 最终输出与降级错误信息
    final_report: str
    error: Optional[str]
