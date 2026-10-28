from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, TaskEngineDep
from app.schemas.agent import (
    ChangeOut,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageAccepted,
    SendMessage,
    TurnChangesOut,
)
from app.schemas.chapter_version import GenerationLogOut
from app.services.agent import service as svc
from app.services.agent import transactions


async def mutation_db(request: Request) -> AsyncIterator[AsyncSession]:
    # 与认证 DbDep 分离；在任何业务查询前设置隔离，每次 commit 后仍沿用同一配置。
    async with transactions.session(request.app.state.sessionmaker) as db:
        try:
            yield db
        except DBAPIError as exc:
            await transactions.rollback_conflict(db, exc)


AgentDbDep = Annotated[AsyncSession, Depends(mutation_db)]


router = APIRouter(prefix="/projects/{project_id}/agent", tags=["agent"])


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(project_id: int, user: CurrentUser, db: AgentDbDep):
    return await svc.list_conversations(db, user.id, project_id)


@router.post("/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    project_id: int, user: CurrentUser, db: AgentDbDep, body: ConversationCreate | None = None
):
    return await svc.create_conversation(db, user.id, project_id, body or ConversationCreate())


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    project_id: int, conversation_id: int, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep
):
    return await svc.get_conversation(db, engine, user.id, project_id, conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    project_id: int, conversation_id: int, body: ConversationUpdate, user: CurrentUser, db: AgentDbDep
):
    return await svc.update_conversation(db, user.id, project_id, conversation_id, body)


@router.get("/conversations/{conversation_id}/generation-logs", response_model=list[GenerationLogOut])
async def list_generation_logs(project_id: int, conversation_id: int, user: CurrentUser, db: AgentDbDep):
    return await svc.list_generation_logs(db, user.id, project_id, conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(project_id: int, conversation_id: int, user: CurrentUser, db: AgentDbDep):
    await svc.delete_conversation(db, user.id, project_id, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/conversations/{conversation_id}/messages", response_model=MessageAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def send_message(
    project_id: int, conversation_id: int, body: SendMessage, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep
):
    return await svc.send_message(db, engine, user.id, project_id, conversation_id, body)


@router.post("/changes/{change_id}/confirm", response_model=ChangeOut)
async def confirm_change(project_id: int, change_id: int, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep):
    return await svc.decide_change(db, engine, user.id, project_id, change_id, confirm=True)


@router.post("/changes/{change_id}/reject", response_model=ChangeOut)
async def reject_change(project_id: int, change_id: int, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep):
    return await svc.decide_change(db, engine, user.id, project_id, change_id, confirm=False)


@router.post("/turns/{task_id}/confirm-all", response_model=TurnChangesOut)
async def confirm_turn(project_id: int, task_id: int, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep):
    return await svc.decide_turn(db, engine, user.id, project_id, task_id, confirm=True)


@router.post("/turns/{task_id}/reject-all", response_model=TurnChangesOut)
async def reject_turn(project_id: int, task_id: int, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep):
    return await svc.decide_turn(db, engine, user.id, project_id, task_id, confirm=False)


@router.post("/changes/{change_id}/undo", response_model=ChangeOut)
async def undo_change(project_id: int, change_id: int, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep):
    return await svc.undo_change(db, user.id, project_id, change_id, engine=engine)


@router.post("/turns/{task_id}/undo", response_model=TurnChangesOut)
async def undo_turn(project_id: int, task_id: int, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep):
    return await svc.undo_turn(db, engine, user.id, project_id, task_id)
