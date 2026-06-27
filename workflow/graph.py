import sqlite3
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from config import settings
from workflow.state import IssueState
from workflow.nodes import (
    fetch_issue, route_issue, detect_regression, detect_existing, classify_fix,
    describe_images, parse_stack_trace,
    build_project_map,
    human_gate,
    memory_retrieve, memory_save,
    retrieve_context,
    analyze_bug, analyze_feature, answer_question,
    reflect,
    generate_report,
)


def _wrap(name: str, fn):
    """给节点函数加日志包装"""
    def wrapped(state: IssueState):
        print(f"\n{'='*50}")
        print(f"▶ 节点: {name}")
        result = fn(state)
        for k, v in result.items():
            preview = str(v)[:120].replace("\n", " ")
            print(f"  {k}: {preview}")
        return result
    return wrapped


def decide_route(state: IssueState) -> str:
    issue_type = state["issue_type"]
    print(f"\n  → 路由决策: {issue_type}")
    return issue_type


def decide_reflection(state: IssueState) -> str:
    confidence = state["confidence"]
    iteration = state["iteration"]
    if confidence < 0.7 and iteration < 2:
        decision = state["issue_type"]
        print(f"\n  → Reflection: 置信度 {confidence:.2f} < 0.7，第 {iteration} 次重试，回到 {decision}")
    else:
        decision = "done"
        print(f"\n  → Reflection: 置信度 {confidence:.2f}，通过，进入报告生成")
    return decision


def build_graph():
    graph = StateGraph(IssueState)

    graph.add_node("fetch_issue",       _wrap("fetch_issue",       fetch_issue))
    graph.add_node("build_project_map", _wrap("build_project_map", build_project_map))
    graph.add_node("describe_images",   _wrap("describe_images",   describe_images))
    graph.add_node("parse_stack_trace", _wrap("parse_stack_trace", parse_stack_trace))
    graph.add_node("route_issue",       _wrap("route_issue",       route_issue))
    graph.add_node("detect_regression",  _wrap("detect_regression",  detect_regression))
    graph.add_node("detect_existing",    _wrap("detect_existing",    detect_existing))
    graph.add_node("classify_fix",       _wrap("classify_fix",       classify_fix))
    graph.add_node("human_gate",         _wrap("human_gate",         human_gate))
    graph.add_node("memory_retrieve",  _wrap("memory_retrieve",  memory_retrieve))
    graph.add_node("retrieve_context", _wrap("retrieve_context", retrieve_context))
    graph.add_node("analyze_bug",      _wrap("analyze_bug",      analyze_bug))
    graph.add_node("analyze_feature",  _wrap("analyze_feature",  analyze_feature))
    graph.add_node("answer_question",  _wrap("answer_question",  answer_question))
    graph.add_node("reflect",          _wrap("reflect",          reflect))
    graph.add_node("generate_report",  _wrap("generate_report",  generate_report))
    graph.add_node("memory_save",      _wrap("memory_save",      memory_save))

    graph.add_edge(START, "fetch_issue")
    graph.add_edge("fetch_issue", "build_project_map")
    graph.add_edge("build_project_map", "describe_images")
    graph.add_edge("describe_images", "parse_stack_trace")
    graph.add_edge("parse_stack_trace", "route_issue")

    # 路由后：security 先过人工审核，其他直接查历史记忆
    graph.add_conditional_edges(
        "route_issue",
        decide_route,
        {
            "bug":      "detect_regression",
            "feature":  "memory_retrieve",
            "question": "memory_retrieve",
            "security": "human_gate",
        }
    )

    graph.add_edge("detect_regression", "memory_retrieve")

    # human_gate 批准后进入正常流程
    graph.add_edge("human_gate", "memory_retrieve")

    # 查完记忆再检索代码上下文
    graph.add_edge("memory_retrieve", "retrieve_context")

    # bug 类先分类修复类型；feature 先检测是否已存在；其他直接分析
    graph.add_conditional_edges(
        "retrieve_context",
        decide_route,
        {
            "bug":      "classify_fix",
            "feature":  "detect_existing",
            "question": "answer_question",
            "security": "classify_fix",
        }
    )
    graph.add_edge("classify_fix", "analyze_bug")
    graph.add_edge("detect_existing", "analyze_feature")

    graph.add_edge("analyze_bug",     "reflect")
    graph.add_edge("analyze_feature", "reflect")
    graph.add_edge("answer_question", "reflect")

    graph.add_conditional_edges(
        "reflect",
        decide_reflection,
        {
            "bug":      "analyze_bug",
            "feature":  "analyze_feature",
            "question": "answer_question",
            "security": "analyze_bug",
            "done":     "generate_report",
        }
    )

    graph.add_edge("generate_report", "memory_save")
    graph.add_edge("memory_save", END)

    db_path = settings.db_path / "checkpoints.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return graph.compile(checkpointer=checkpointer)


app = build_graph()
