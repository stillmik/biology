from langgraph.graph import END, START, StateGraph

from ..states import ChatState
from .nodes import build_context_node, context_route, grok_node, load_context_node, needs_summary_node, reload_context_node, summarize_node


def create_chat_graph(include_answer_node: bool):
    builder = StateGraph(ChatState)
    builder.add_node("load_context", load_context_node)
    builder.add_node("needs_summary", needs_summary_node)
    builder.add_node("summarize", summarize_node)
    builder.add_node("reload_context", reload_context_node)
    builder.add_node("build_context", build_context_node)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "needs_summary")
    builder.add_conditional_edges("needs_summary", context_route, {"summarize": "summarize", "build_context": "build_context"})
    builder.add_edge("summarize", "reload_context")
    builder.add_edge("reload_context", "needs_summary")

    if include_answer_node:
        builder.add_node("grok", grok_node)
        builder.add_edge("build_context", "grok")
        builder.add_edge("grok", END)
    else:
        builder.add_edge("build_context", END)
    return builder.compile()


chat_graph = create_chat_graph(include_answer_node=True)
prepare_graph = create_chat_graph(include_answer_node=False)
