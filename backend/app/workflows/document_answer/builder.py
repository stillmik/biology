from langgraph.graph import END, START, StateGraph

from ...schemas.document_answer import DocumentAnswerState
from .nodes import *


def create_document_answer_graph():
    builder = StateGraph(DocumentAnswerState)
    builder.add_node("interpret_question", interpret_document_question_node)
    builder.add_node("load_direct_evidence", load_direct_document_evidence_node)
    builder.add_node("retrieve_hierarchical_evidence", retrieve_hierarchical_document_evidence_node)
    builder.add_node("build_model_input", build_document_answer_model_input_node)
    builder.add_node("generate_answer", generate_document_answer_node)
    builder.add_node("validate_citations", validate_document_answer_citations_node)

    builder.add_edge(START, "interpret_question")
    builder.add_conditional_edges("interpret_question", choose_document_answer_route, {"direct": "load_direct_evidence", "hierarchical": "retrieve_hierarchical_evidence"})
    builder.add_edge("load_direct_evidence", "build_model_input")
    builder.add_edge("retrieve_hierarchical_evidence", "build_model_input")
    builder.add_edge("build_model_input", "generate_answer")
    builder.add_edge("generate_answer", "validate_citations")
    builder.add_edge("validate_citations", END)
    return builder.compile()


document_answer_graph = create_document_answer_graph()
