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

    # generate_patch / validate_patch：第二阶段只生成并检查，不应用补丁
    patch: str
    patch_files: list[str]
    patch_explanation: str
    patch_risk: str
    patch_status: str
    patch_errors: list[str]
    patch_attempt: int
    # 保存每一版补丁的 SHA-256，防止模型重复生成相同方案。
    patch_history: list[str]
    # API 分阶段执行时锁定用户看到的补丁；后续验证/发布不得静默换成新版本。
    patch_locked: bool
    expected_patch_sha256: str
    patch_sha256: str

    # verify_patch_in_sandbox：第三阶段仅在用户显式授权时执行
    execute_patch: bool
    verification_status: str
    test_command: list[str]
    targeted_test_result: dict
    regression_test_result: dict

    # revise_patch / review_patch：第四阶段修订循环与发布前审查
    # issues 使用统一结构：category/severity/file/line/description/suggestion。
    patch_review_issues: list[dict]
    # approved / warning / blocked，供下一阶段创建 PR 前做硬性门禁。
    patch_review_status: str
    # 仅表示审查结论可信度，不会降低确定性规则产生的 high 风险。
    patch_review_confidence: float

    # publish_patch：第五阶段显式授权后的 GitHub 写入结果
    create_pr: bool
    publish_status: str
    published_branch: str
    published_commit: str
    draft_pr_url: str
    publish_error: str

    # 最终输出与降级错误信息
    final_report: str
    error: Optional[str]
