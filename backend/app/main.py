import os
from typing import TypedDict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field
from .db import add_message, create_conversation, create_user, delete_conversation, delete_message, get_conversation, get_conversation_messages, get_conversations, get_message, get_messages, get_user, get_user_by_username, init_db, rename_conversation, update_message


load_dotenv()


app = FastAPI(title="Biology Chat API", version="0.1.0")
init_db()


class ChatState(TypedDict):
    message: str
    history: list[dict[str, str]]
    reply: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: int = Field(gt=0)
    conversation_id: int = Field(gt=0)


class ChatResponse(BaseModel):
    reply: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")


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


class MessageUpdateRequest(BaseModel):
    user_id: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=4000)


def grok_node(state: ChatState) -> dict[str, str]:
    api_key = os.getenv("XAI_API_KEY")
    
    if not api_key:
        raise RuntimeError("XAI_API_KEY is not configured")

    client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    response = client.responses.create(model=os.getenv("XAI_MODEL", "grok-4.3"), input=state["history"])
    return {"reply": response.output_text}


graph_builder = StateGraph(ChatState)
graph_builder.add_node("grok", grok_node)
graph_builder.add_edge(START, "grok")
graph_builder.add_edge("grok", END)
chat_graph = graph_builder.compile()

app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:18080", "http://127.0.0.1:18080"], allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["*"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/users", response_model=UserResponse)
def register(request: RegisterRequest) -> UserResponse:
    try:
        user = create_user(request.username.strip())
    except UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Username is already taken") from error
    
    return UserResponse(id=user["id"], username=user["username"])


@app.post("/api/users/access", response_model=UserResponse)
def access_user(request: RegisterRequest) -> UserResponse:
    username = request.username.strip()
    existing_user = get_user_by_username(username)
    
    if existing_user:
        return UserResponse(id=existing_user["id"], username=existing_user["username"])
    
    user = create_user(username)
    return UserResponse(id=user["id"], username=user["username"])


@app.get("/api/users/{user_id}", response_model=UserResponse)
def user_profile(user_id: int) -> UserResponse:
    user = get_user(user_id)
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return UserResponse(id=user["id"], username=user["username"])


@app.get("/api/users/{user_id}/messages", response_model=list[HistoryMessage])
def history(user_id: int) -> list[HistoryMessage]:
    if not get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    
    return [HistoryMessage(id=row["id"], role=row["role"], content=row["content"], created_at=row["created_at"].isoformat()) for row in get_messages(user_id)]


def conversation_response(conversation: dict) -> ConversationResponse:
    return ConversationResponse(id=conversation["id"], user_id=conversation["user_id"], title=conversation["title"], created_at=conversation["created_at"].isoformat(), updated_at=conversation["updated_at"].isoformat())


@app.get("/api/users/{user_id}/conversations", response_model=list[ConversationResponse])
def conversations(user_id: int) -> list[ConversationResponse]:
    if not get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return [conversation_response(conversation) for conversation in get_conversations(user_id)]


@app.post("/api/conversations", response_model=ConversationResponse)
def new_conversation(request: ConversationRequest) -> ConversationResponse:
    if not get_user(request.user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return conversation_response(create_conversation(request.user_id, request.title.strip()))


@app.get("/api/conversations/{conversation_id}/messages", response_model=list[HistoryMessage])
def conversation_history(conversation_id: int, user_id: int) -> list[HistoryMessage]:
    if not get_conversation(conversation_id, user_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return [HistoryMessage(id=row["id"], role=row["role"], content=row["content"], created_at=row["created_at"].isoformat()) for row in get_conversation_messages(conversation_id)]


@app.patch("/api/conversations/{conversation_id}", response_model=ConversationResponse)
def rename_chat(conversation_id: int, request: ConversationRequest) -> ConversationResponse:
    conversation = rename_conversation(conversation_id, request.user_id, request.title.strip())
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation_response(conversation)


@app.patch("/api/messages/{message_id}", response_model=ChatResponse)
def edit_message(message_id: int, request: MessageUpdateRequest) -> ChatResponse:
    message = update_message(message_id, request.user_id, request.content.strip())
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    conversation_id = message["conversation_id"]
    history = [{"role": row["role"], "content": row["content"]} for row in get_conversation_messages(conversation_id)]
    result = chat_graph.invoke({"message": request.content.strip(), "history": history, "reply": ""})
    add_message(request.user_id, conversation_id, "assistant", result["reply"])
    return ChatResponse(reply=result["reply"])


@app.delete("/api/messages/{message_id}")
def remove_message(message_id: int, user_id: int) -> dict[str, bool]:
    if not delete_message(message_id, user_id):
        raise HTTPException(status_code=404, detail="Message not found")
    return {"deleted": True}


@app.delete("/api/conversations/{conversation_id}")
def remove_conversation(conversation_id: int, user_id: int) -> dict[str, bool]:
    if not delete_conversation(conversation_id, user_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    if not get_user(request.user_id):
        raise HTTPException(status_code=404, detail="User not found")
    
    if not get_conversation(request.conversation_id, request.user_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    previous_messages = [{"role": row["role"], "content": row["content"]} for row in get_conversation_messages(request.conversation_id)]
    add_message(request.user_id, request.conversation_id, "user", request.message)
    result = chat_graph.invoke({"message": request.message, "history": previous_messages + [{"role": "user", "content": request.message}], "reply": ""})
    add_message(request.user_id, request.conversation_id, "assistant", result["reply"])
    return ChatResponse(reply=result["reply"])
