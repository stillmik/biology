import logging

from fastapi import APIRouter, HTTPException
from psycopg.errors import UniqueViolation

from ..core.config import ENVIRONMENT
from ..core.observability import APP_OPERATIONS, hash_identifier, log_event, observe_operation
from ..infrastructure.database import create_user_from_db, get_user_by_username_from_db, get_user_from_db
from ..schemas.chat import RegisterRequest, UserResponse


router = APIRouter(prefix="/api/users", tags=["users"])
logger = logging.getLogger(__name__)


@router.post("", response_model=UserResponse)
def create_user(request: RegisterRequest) -> UserResponse:
    if ENVIRONMENT == "production":
        APP_OPERATIONS.labels(operation="register", result="rejected").inc()
        raise HTTPException(status_code=404, detail="Endpoint is unavailable")

    with observe_operation("register"):
        try:
            user = create_user_from_db(request.username)
        except UniqueViolation as error:
            APP_OPERATIONS.labels(operation="register_conflict", result="rejected").inc()
            raise HTTPException(status_code=409, detail="Username is already taken") from error
    log_event(logger, logging.INFO, "user_registered", user_id_hash=hash_identifier(user["id"]))
    return UserResponse(id=user["id"], username=user["username"])


@router.post("/access", response_model=UserResponse)
def get_user_by_username(request: RegisterRequest) -> UserResponse:
    if ENVIRONMENT == "production":
        APP_OPERATIONS.labels(operation="access_user", result="rejected").inc()
        raise HTTPException(status_code=404, detail="Endpoint is unavailable")

    with observe_operation("access_user"):
        existing_user = get_user_by_username_from_db(request.username)

        if existing_user is None:
            raise HTTPException(status_code=404, detail="User was not found")
        log_event(logger, logging.INFO, "user_accessed", user_id_hash=hash_identifier(existing_user["id"]), existing=True)
    return UserResponse(id=existing_user["id"], username=existing_user["username"])


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int) -> UserResponse:
    with observe_operation("get_user"):
        user = get_user_from_db(user_id)

        if not user:
            raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(id=user["id"], username=user["username"])
