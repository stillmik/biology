from typing import NotRequired, TypedDict

from pydantic import BaseModel, Field


class DocumentAnalysisState(TypedDict):
    job_id: int
    document_id: str
    resume_stage: NotRequired[str]
    storage_name: NotRequired[str]
    extracted_token_count: NotRequired[int]
    page_count: NotRequired[int]
    analysis_mode: NotRequired[str]
    document_evidence_chunk_ids: NotRequired[list[int]]
    root_summary: NotRequired[str]


class DocumentResponse(BaseModel):
    id: str
    user_id: int
    filename: str
    status: str
    analysis_mode: str | None
    progress_percent: int
    page_count: int | None
    extracted_token_count: int | None
    summary: str
    last_error: str
    created_at: str
    updated_at: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]


class DocumentUploadResponse(BaseModel):
    document: DocumentResponse
    reused: bool


class ConversationDocumentRequest(BaseModel):
    user_id: int = Field(gt=0)


class AnswerJobResponse(BaseModel):
    id: int
    conversation_id: int
    user_message_id: int
    assistant_message_id: int | None
    status: str
    last_error: str
    created_at: str
    updated_at: str
