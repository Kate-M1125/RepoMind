import sqlite3
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from config import settings
from workflow.pr_state import PRState
from workflow.nodes.fetch_pr import fetch_pr, describe_pr_images
from workflow.nodes.project_map import build_project_map
from workflow.nodes.analyze_pr import (
    run_review, dispatch_reviews,
    reflect_pr, generate_pr_report,
    memory_retrieve_pr, memory_save_pr,
)


def _wrap(name: str, fn):
    def wrapped(state):
        print(f"\n{'='*50}")
        print(f"▶ 节点: {name}")
        result = fn(state)
        for k, v in (result or {}).items():
            preview = str(v)[:120].replace("\n", " ")
            print(f"  {k}: {preview}")
        return result
    return wrapped


def decide_reflection(state: PRState) -> str:
    confidence = state.get("confidence", 1.0)
    iteration = state.get("iteration", 0)
    if confidence < 0.7 and iteration < 2:
        print(f"\n  → Reflect: 置信度 {confidence:.2f} < 0.7，第 {iteration} 次重试")
        return "retry"
    print(f"\n  → Reflect: 置信度 {confidence:.2f}，通过")
    return "done"


def build_pr_graph():
    graph = StateGraph(PRState)

    # ── 常规节点 ──────────────────────────────────────────────────────
    graph.add_node("fetch_pr",           _wrap("fetch_pr",           fetch_pr))
    graph.add_node("build_project_map",  _wrap("build_project_map",  build_project_map))
    graph.add_node("describe_pr_images", _wrap("describe_pr_images", describe_pr_images))
    graph.add_node("memory_retrieve_pr", _wrap("memory_retrieve_pr", memory_retrieve_pr))

    # prepare_reviews：轻量占位节点，用于触发 Send API 条件边
    # 初次分析和 retry 都汇入这里，避免 retry 时重跑 memory 查询
    graph.add_node("prepare_reviews", lambda state: {})

    # run_review：三路并行子节点，每路写回父 state 不同字段
    graph.add_node("run_review",         _wrap("run_review",         run_review))

    graph.add_node("reflect_pr",         _wrap("reflect_pr",         reflect_pr))
    graph.add_node("generate_pr_report", _wrap("generate_pr_report", generate_pr_report))
    graph.add_node("memory_save_pr",     _wrap("memory_save_pr",     memory_save_pr))

    # ── 主干边 ────────────────────────────────────────────────────────
    graph.add_edge(START,                "fetch_pr")
    graph.add_edge("fetch_pr",           "build_project_map")
    graph.add_edge("build_project_map",  "describe_pr_images")
    graph.add_edge("describe_pr_images", "memory_retrieve_pr")
    graph.add_edge("memory_retrieve_pr", "prepare_reviews")

    # Send API：dispatch_reviews 返回 [Send("run_review", ...) × 3]
    # LangGraph 并行执行三个 run_review，全部完成后才继续
    graph.add_conditional_edges("prepare_reviews", dispatch_reviews)

    # 三路并行全部完成 → reflect
    graph.add_edge("run_review", "reflect_pr")

    # reflect 决策：retry 回 prepare_reviews（跳过 memory），done 生成报告
    graph.add_conditional_edges(
        "reflect_pr",
        decide_reflection,
        {
            "retry": "prepare_reviews",
            "done":  "generate_pr_report",
        },
    )

    graph.add_edge("generate_pr_report", "memory_save_pr")
    graph.add_edge("memory_save_pr",     END)

    db_path = settings.db_path / "pr_checkpoints.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(conn))


pr_app = build_pr_graph()
