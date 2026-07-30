from langgraph.graph import END, START, StateGraph

from ...schemas.documents import DocumentAnalysisState
from .nodes import (
    build_basic_document_node,
    build_deep_document_node,
    choose_analysis_route,
    extract_document_node,
    index_evidence_node,
    load_document_node,
    verify_document_node,
)


def create_document_analysis_graph():
    builder = StateGraph(DocumentAnalysisState)
    builder.add_node("load_document", load_document_node)
    builder.add_node("extract_document", extract_document_node)
    builder.add_node("index_evidence", index_evidence_node)
    builder.add_node("build_basic_document", build_basic_document_node)
    builder.add_node("build_deep_document", build_deep_document_node)
    builder.add_node("verify_document", verify_document_node)
    builder.add_edge(START, "load_document")
    builder.add_edge("load_document", "extract_document")
    builder.add_edge("extract_document", "index_evidence")
    builder.add_conditional_edges(
        "index_evidence",
        choose_analysis_route,
        {
            "basic": "build_basic_document",
            "deep": "build_deep_document",
        },
    )
    builder.add_edge("build_basic_document", "verify_document")
    builder.add_edge("build_deep_document", "verify_document")
    builder.add_edge("verify_document", END)
    return builder.compile()


document_analysis_graph = create_document_analysis_graph()
