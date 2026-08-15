import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from config import settings
from workflow.ci_state import CIState
from workflow.nodes.ci import (
    analyze_ci_failure,
    collect_ci_context,
    fetch_ci_run,
    generate_patch,
    generate_ci_report,
    parse_failure_logs,
    publish_patch,
    reflect_ci,
    revise_patch,
    review_patch,
    validate_generated_patch,
    verify_patch_in_sandbox,
)
from workflow.nodes.project_map import build_project_map


def _wrap(name, fn):
    """统一打印节点输入后的关键输出预览，便于 CLI 调试。"""
    def wrapped(state):
        print(f"\n{'=' * 50}\n▶ 节点: {name}")
        result = fn(state)
        for key, value in (result or {}).items():
            print(f"  {key}: {str(value)[:120].replace(chr(10), ' ')}")
        return result
    return wrapped


def decide_reflection(state: CIState) -> str:
    """置信度低于 0.7 时最多重新分析一次；iteration 由 reflect 节点递增。"""
    if state.get("confidence", 0) < 0.7 and state.get("iteration", 0) < 2:
        return "retry"
    return "done"


def decide_after_validation(state: CIState) -> str:
    """无效补丁优先修订；合法补丁按用户授权进入测试或直接审查。"""
    # patch 为空表示生成失败或检测到重复版本，此时继续修订没有新的输入依据。
    if state.get("patch_status") != "valid":
        if state.get("patch_locked"):
            return "report"
        return "revise" if state.get("patch_attempt", 0) < 3 and state.get("patch") else "report"
    return "execute" if state.get("execute_patch") else "review"


def decide_after_verification(state: CIState) -> str:
    """测试失败最多修订至第三版；通过或无法执行时进入补丁审查。"""
    status = state.get("verification_status", "")
    # blocked 通常代表缺少隔离器/测试命令，不是补丁本身失败，因此不盲目改代码。
    if (status in {"failed", "timeout"} and not state.get("patch_locked")
            and state.get("patch_attempt", 0) < 3):
        return "revise"
    return "review"


def decide_publish(state: CIState) -> str:
    """只有用户显式提供 --create-pr 才进入包含 GitHub 写操作的节点。"""
    # 审查 blocked 仍可路由到节点，但节点内部硬门禁会拒绝写入并生成原因报告。
    return "publish" if state.get("create_pr") else "report"


def build_ci_graph():
    """组装 CI 诊断与候选补丁生成图；整个图仍不应用补丁。"""
    graph = StateGraph(CIState)
    graph.add_node("fetch_ci_run", _wrap("fetch_ci_run", fetch_ci_run))
    graph.add_node("parse_failure_logs", _wrap("parse_failure_logs", parse_failure_logs))
    graph.add_node("build_project_map", _wrap("build_project_map", build_project_map))
    graph.add_node("collect_ci_context", _wrap("collect_ci_context", collect_ci_context))
    graph.add_node("analyze_ci_failure", _wrap("analyze_ci_failure", analyze_ci_failure))
    graph.add_node("reflect_ci", _wrap("reflect_ci", reflect_ci))
    graph.add_node("generate_patch", _wrap("generate_patch", generate_patch))
    graph.add_node("validate_patch", _wrap("validate_patch", validate_generated_patch))
    graph.add_node("verify_patch", _wrap("verify_patch", verify_patch_in_sandbox))
    # revise_patch 被静态校验和动态测试两条失败路径共同复用。
    graph.add_node("revise_patch", _wrap("revise_patch", revise_patch))
    # review_patch 是发布前最后一道质量门，不负责修改补丁。
    graph.add_node("review_patch", _wrap("review_patch", review_patch))
    graph.add_node("publish_patch", _wrap("publish_patch", publish_patch))
    graph.add_node("generate_ci_report", _wrap("generate_ci_report", generate_ci_report))

    # 主干：拉取 → 规则解析 → 仓库上下文 → LLM 分析 → 独立评审 → 报告。
    graph.add_edge(START, "fetch_ci_run")
    graph.add_edge("fetch_ci_run", "parse_failure_logs")
    graph.add_edge("parse_failure_logs", "build_project_map")
    graph.add_edge("build_project_map", "collect_ci_context")
    graph.add_edge("collect_ci_context", "analyze_ci_failure")
    graph.add_edge("analyze_ci_failure", "reflect_ci")
    # 重试只回到分析节点，不重复下载日志和建立仓库索引。
    graph.add_conditional_edges(
        "reflect_ci", decide_reflection,
        {"retry": "analyze_ci_failure", "done": "generate_patch"},
    )
    # 默认只生成和验证；显式 --execute-patch 才进入临时 worktree。
    graph.add_edge("generate_patch", "validate_patch")
    # 静态校验失败和运行测试失败最终汇入同一个 revise_patch 节点。
    graph.add_conditional_edges(
        "validate_patch", decide_after_validation,
        {"revise": "revise_patch", "execute": "verify_patch",
         "review": "review_patch", "report": "generate_ci_report"},
    )
    # 已授权执行时，测试结果会成为审查证据；未授权时仍可进行静态补丁审查。
    graph.add_conditional_edges(
        "verify_patch", decide_after_verification,
        {"revise": "revise_patch", "review": "review_patch"},
    )
    # 每个修订版本必须从静态校验重新开始，不能直接复用上一版的 valid 状态。
    graph.add_edge("revise_patch", "validate_patch")
    # 未授权直接报告；已授权则让 publish_patch 再执行一次完整门禁检查。
    graph.add_conditional_edges(
        "review_patch", decide_publish,
        {"publish": "publish_patch", "report": "generate_ci_report"},
    )
    graph.add_edge("publish_patch", "generate_ci_report")
    graph.add_edge("generate_ci_report", END)

    # CI 使用独立 checkpoint 文件，避免与 Issue/PR thread 状态相互污染。
    db_path = settings.db_path / "ci_checkpoints.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(connection))


ci_app = build_ci_graph()
