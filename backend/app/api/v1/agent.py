from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.core.errors import UnauthorizedError
from app.schemas.agent import (
    ChangeAction,
    ChangeOut,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageAccepted,
    SendMessage,
    TurnAction,
    TurnChangesOut,
)
from app.schemas.chapter_version import GenerationLogOut
from app.services.agent import service as svc
from app.services.agent import transactions
from app.services.authorization_service import AuthenticatedActor, require_authenticated


async def mutation_db(request: Request, user: CurrentUser, authenticated_db: DbDep) -> AsyncIterator[AsyncSession]:
    # 与认证 DbDep 分离；在任何业务查询前设置隔离，每次 commit 后仍沿用同一配置。
    # 认证时的原始令牌版本必须随会话传递；不能从数据库重读当前版本伪造请求凭据。
    actor = authenticated_db.info.get("authenticated_actor")
    if not isinstance(actor, AuthenticatedActor) or actor.id != user.id:
        raise UnauthorizedError("未登录或登录已过期")
    async with transactions.session(request.app.state.sessionmaker) as db:
        db.info["authenticated_actor"] = actor
        try:
            # 独立会话不能沿用认证会话可能已过期的快照。
            await require_authenticated(db, actor)
            # Agent 使用 REPEATABLE READ；认证快照不能固定后续业务事务。
            await db.rollback()
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
async def confirm_change(
    project_id: int, change_id: int, body: ChangeAction, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep,
):
    return await svc.decide_change(db, engine, user.id, project_id, change_id, body, confirm=True)


@router.post("/changes/{change_id}/reject", response_model=ChangeOut)
async def reject_change(
    project_id: int, change_id: int, body: ChangeAction, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep,
):
    return await svc.decide_change(db, engine, user.id, project_id, change_id, body, confirm=False)


@router.post("/turns/{task_id}/confirm-all", response_model=TurnChangesOut)
async def confirm_turn(
    project_id: int, task_id: int, body: TurnAction, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep,
):
    return await svc.decide_turn(db, engine, user.id, project_id, task_id, body, confirm=True)


@router.post("/turns/{task_id}/reject-all", response_model=TurnChangesOut)
async def reject_turn(
    project_id: int, task_id: int, body: TurnAction, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep,
):
    return await svc.decide_turn(db, engine, user.id, project_id, task_id, body, confirm=False)


@router.post("/changes/{change_id}/undo", response_model=ChangeOut)
async def undo_change(
    project_id: int, change_id: int, body: ChangeAction, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep,
):
    return await svc.undo_change(db, user.id, project_id, change_id, body, engine=engine)


@router.post("/turns/{task_id}/undo", response_model=TurnChangesOut)
async def undo_turn(
    project_id: int, task_id: int, body: TurnAction, user: CurrentUser, db: AgentDbDep, engine: TaskEngineDep,
):
    return await svc.undo_turn(db, engine, user.id, project_id, task_id, body)
