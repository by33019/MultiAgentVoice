"""图的组装。

supervisor 模式的结构：

                    ┌──────────────┐
        ┌──────────►│  supervisor  │◄──────────┐
        │           └──────┬───────┘           │
        │                  │ 条件边：看 next_worker
        │      ┌───────────┼───────────┐       │
        │      ▼           ▼           ▼       │
        │  researcher    writer      voice ────┘
        │      │           │           │
        └──────┴───────────┴───────────┘
                           │
                           ▼（next_worker == finish）
                        finish → END

为什么选 supervisor 而不是 pipeline / network（录屏②要讲的）：
- 本任务有**明确的阶段依赖**（没资料写不了稿，没稿配不了音），
  pipeline 也能跑，但出错时无法重试、也无法在中途改变策略；
- network 让 worker 自由转交，调试成本高、链路难讲清楚；
- supervisor 把"下一步该谁"收敛到一个可观测的决策点（日志里有 `supervisor/decision`），
  同时保留了重试与跳过的能力（配音失败两次就降级交付文字稿）。
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import Nodes
from .state import AgentState

WORKERS = ("researcher", "writer", "voice")


def build_graph(nodes: Nodes):
    graph = StateGraph(AgentState)

    graph.add_node("supervisor", nodes.supervisor)
    graph.add_node("researcher", nodes.researcher)
    graph.add_node("writer", nodes.writer)
    graph.add_node("voice", nodes.voice)
    graph.add_node("finish", nodes.finish)

    graph.set_entry_point("supervisor")

    # 条件边：调度中心决定去哪
    graph.add_conditional_edges(
        "supervisor",
        lambda state: state.get("next_worker") or "finish",
        {
            "researcher": "researcher",
            "writer": "writer",
            "voice": "voice",
            "finish": "finish",
        },
    )

    # 每个 worker 干完都回到调度中心，由它决定下一步
    for worker in WORKERS:
        graph.add_edge(worker, "supervisor")

    graph.add_edge("finish", END)
    return graph.compile()
