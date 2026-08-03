from langgraph.graph import END, START, StateGraph

from ...schemas.documents import DocumentAnalysisState
from .nodes import *


def create_document_analysis_graph():
    builder = StateGraph(DocumentAnalysisState)
    builder.add_node("load_document_info", load_document_info_node)
    builder.add_node("extract_document_pages_and_tables_in_db_node", extract_document_pages_and_tables_in_db_node)
    builder.add_node("process_doc_into_basic_chunks", process_doc_into_basic_chunks)
    builder.add_node("route_analysis", route_analysis_node)
    builder.add_node("build_basic_document", build_basic_document_node)
    builder.add_node("build_deep_document", build_deep_document_node)
    builder.add_node("verify_document", verify_document_node)

    builder.add_edge(START, "load_document_info")
    builder.add_conditional_edges("load_document_info", choose_resume_route, {"extract": "extract_document_pages_and_tables_in_db_node", "index": "process_doc_into_basic_chunks", "analyze": "route_analysis", "verify": "verify_document"})
    builder.add_edge("extract_document_pages_and_tables_in_db_node", "process_doc_into_basic_chunks")
    builder.add_edge("process_doc_into_basic_chunks", "route_analysis")
    builder.add_conditional_edges("route_analysis", choose_analysis_route, {"basic": "build_basic_document", "deep": "build_deep_document"})
    builder.add_edge("build_basic_document", "verify_document")
    builder.add_edge("build_deep_document", "verify_document")
    builder.add_edge("verify_document", END)
    return builder.compile()


document_analysis_graph = create_document_analysis_graph()
