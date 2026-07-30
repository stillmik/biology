from typing import NotRequired, TypedDict


class DocumentAnswerState(TypedDict):
    user_id: int
    conversation_id: int
    question: str
    documents: list[dict]
    answer_depth: NotRequired[str]
    retrieval_question: NotRequired[str]
    evidence_nodes: NotRequired[list[dict]]
    model_input: NotRequired[list[dict[str, str]]]
    raw_answer: NotRequired[str]
    validated_answer: NotRequired[str]
