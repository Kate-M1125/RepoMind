import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from config import settings
from workflow.ci_state import CIState
from workflow.nodes.ci import (
    analyze_ci_failure,
    collect_ci_context,
    fetch_ci_run,
    generate_ci_report,
    parse_failure_logs,
    reflect_ci,
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


def build_ci_graph():
    """组装第一阶段只读 CI 诊断图。"""
    graph = StateGraph(CIState)
    graph.add_node("fetch_ci_run", _wrap("fetch_ci_run", fetch_ci_run))
    graph.add_node("parse_failure_logs", _wrap("parse_failure_logs", parse_failure_logs))
    graph.add_node("build_project_map", _wrap("build_project_map", build_project_map))
    graph.add_node("collect_ci_context", _wrap("collect_ci_context", collect_ci_context))
    graph.add_node("analyze_ci_failure", _wrap("analyze_ci_failure", analyze_ci_failure))
    graph.add_node("reflect_ci", _wrap("reflect_ci", reflect_ci))
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
        {"retry": "analyze_ci_failure", "done": "generate_ci_report"},
    )
    graph.add_edge("generate_ci_report", END)

    # CI 使用独立 checkpoint 文件，避免与 Issue/PR thread 状态相互污染。
    db_path = settings.db_path / "ci_checkpoints.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(connection))


ci_app = build_ci_graph()
