from typing import Any, NotRequired, TypedDict

from pydantic import BaseModel, Field, field_validator


class ChatMessage(TypedDict):
    id: int
    role: str
    content: str


class ChatState(TypedDict):
    conversation_id: int
    attached_summaries: list[dict[str, Any]]
    included_summary: dict[str, Any]
    summary_cursor: int
    unsummarized_messages: list[ChatMessage]
    projected_tokens: int
    tokens_until_summarization: int
    summarization_trigger_progress: float
    should_summarize: bool
    can_summarize: bool
    summary_passes: int
    summary_decision: str
    summary_reason: str
    summarizable_message_count: int
    summary_messages_processed: int
    summary_token_reduction: int
    history: list[dict[str, str]]
    reply: NotRequired[str]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: int = Field(gt=0)
    conversation_id: int = Field(gt=0)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message cannot be blank")
        return stripped


class ChatResponse(BaseModel):
    reply: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip()


class UserResponse(BaseModel):
    id: int
    username: str


class HistoryMessage(BaseModel):
    id: int
    role: str
    content: str
    created_at: str


class ConversationResponse(BaseModel):
    id: int
    user_id: int
    title: str
    created_at: str
    updated_at: str


class ConversationRequest(BaseModel):
    user_id: int = Field(gt=0)
    title: str = Field(default="New conversation", min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Title cannot be blank")
        return stripped


class ContextBudgetError(RuntimeError):
    pass


class MessageEditRequest(BaseModel):
    user_id: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        stripped = value.strip()

        if not stripped:
            raise ValueError("Message cannot be blank")

        return stripped
