from typing import TypedDict, Optional, Literal

class IssueState(TypedDict):
    # 用户输入
    issue_url: str

    # fetch_issue 节点填入
    issue_number: int
    title: str
    body: str
    labels: list[str]
    comments: list[dict]
    related_prs: list[str]

    # route 节点填入
    # Literal 限制只能是这四个字符串之一
    issue_type: Literal["bug", "feature", "question", "security"]

    # detect_regression 节点填入
    is_regression: bool

    # classify_fix 节点填入
    fix_type: str  # code_fix / dependency_upgrade / config_fix

    # detect_existing 节点填入（feature 专用）
    feature_exists: bool
    existing_api: str  # 已存在时描述用法；不存在时为空

    # analyze 节点填入
    root_cause: str
    fix_suggestion: str
    affected_files: list[str]
    severity: str                # critical / high / medium / low
    code_changes: list[dict]     # [{"file": "...", "line": 42, "change": "..."}]
    suggested_labels: list[str]

    # generate_report 节点填入
    final_report: str

    # reflect 节点填入
    confidence: float
    reflection_note: str

    # describe_images 节点填入
    image_descriptions: str

    # parse_stack_trace 节点填入
    stack_trace_context: str

    # retrieve 节点填入
    code_context: str

    # memory_retrieve 填入
    memory_context: str

    # human_gate 填入
    human_decision: str

    # build_project_map 节点填入
    project_map: str

    # 控制流用
    iteration: int
    error: Optional[str]