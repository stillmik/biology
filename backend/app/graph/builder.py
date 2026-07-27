from langgraph.graph import END, START, StateGraph

from ..states import ChatState
from .nodes import context_route, force_safety_summary_node, grok_node, load_context_node, mark_post_response_summary_node, needs_summary_node, prepare_answer_context_node, reload_context_node, summarize_node


def create_answer_context_graph(include_answer_node: bool):
    builder = StateGraph(ChatState)
    builder.add_node("load_context", load_context_node)
    builder.add_node("mark_post_response_summary", mark_post_response_summary_node)
    builder.add_node("prepare_answer_context", prepare_answer_context_node)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "mark_post_response_summary")
    builder.add_edge("mark_post_response_summary", "prepare_answer_context")

    if include_answer_node:
        builder.add_node("grok", grok_node)
        builder.add_edge("prepare_answer_context", "grok")
        builder.add_edge("grok", END)
    else:
        builder.add_edge("prepare_answer_context", END)
    return builder.compile()


def create_summary_graph(include_context_node: bool):
    builder = StateGraph(ChatState)
    builder.add_node("load_context", load_context_node)
    builder.add_node("needs_summary", needs_summary_node)
    builder.add_node("summarize", summarize_node)
    builder.add_node("reload_context", reload_context_node)
    builder.add_edge(START, "load_context")
    if include_context_node:
        builder.add_node("force_safety_summary", force_safety_summary_node)
        builder.add_edge("load_context", "force_safety_summary")
        builder.add_edge("force_safety_summary", "needs_summary")
    else:
        builder.add_edge("load_context", "needs_summary")
    builder.add_conditional_edges("needs_summary", context_route, {"summarize": "summarize", "finish": "prepare_answer_context" if include_context_node else END})
    builder.add_edge("summarize", "reload_context")
    builder.add_edge("reload_context", "needs_summary")
    if include_context_node:
        builder.add_node("prepare_answer_context", prepare_answer_context_node)
        builder.add_edge("prepare_answer_context", END)
    return builder.compile()


chat_graph = create_answer_context_graph(include_answer_node=True)
prepare_graph = create_answer_context_graph(include_answer_node=False)
summary_graph = create_summary_graph(include_context_node=False)
safety_context_graph = create_summary_graph(include_context_node=True)
