"""灵感模式。固定路径（/sessions/from-project/...、/scale-suggest）先于 /sessions/{session_id} 注册。"""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SessionmakerDep, SettingsDep
from app.schemas.inspiration import (
    ApplyPreviewItem,
    ApplyRequest,
    ConvertRequest,
    GenerateRequest,
    NoteCreate,
    Scale,
    SessionBrief,
    SessionCreate,
    SessionOut,
    SessionUpdate,
    StepKey,
    StepUpdate,
)
from app.schemas.project import TARGET_WORDS_MAX, TARGET_WORDS_MIN, ProjectOut
from app.services import inspiration_service as svc

router = APIRouter(prefix="/inspiration", tags=["inspiration"])


@router.get("/scale-suggest", response_model=Scale)
async def scale_suggest(
    user: CurrentUser,
    target_words: Annotated[int, Query(ge=TARGET_WORDS_MIN, le=TARGET_WORDS_MAX)],
) -> Scale:
    return svc.suggest_scale(target_words)


@router.get("/sessions", response_model=list[SessionBrief])
async def list_sessions(user: CurrentUser, db: DbDep) -> list[SessionBrief]:
    return await svc.list_sessions(db, user.id)


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreate, user: CurrentUser, db: DbDep) -> SessionOut:
    return await svc.create_session(db, user.id, body)


@router.post("/sessions/from-project/{project_id}", response_model=SessionOut)
async def from_project(project_id: int, user: CurrentUser, db: DbDep) -> SessionOut:
    return await svc.from_project(db, user.id, project_id)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: int, user: CurrentUser, db: DbDep) -> SessionOut:
    return await svc.get_session(db, user.id, session_id)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def update_session(session_id: int, body: SessionUpdate, user: CurrentUser, db: DbDep) -> SessionOut:
    return await svc.update_session(db, user.id, session_id, body)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_session(db, user.id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/sessions/{session_id}/steps/{step}", response_model=SessionOut)
async def update_step(session_id: int, step: StepKey, body: StepUpdate, user: CurrentUser, db: DbDep) -> SessionOut:
    return await svc.update_step(db, user.id, session_id, step, body)


@router.post("/sessions/{session_id}/notes", response_model=SessionOut)
async def add_note(session_id: int, body: NoteCreate, user: CurrentUser, db: DbDep) -> SessionOut:
    return await svc.add_note(db, user.id, session_id, body)


@router.delete("/sessions/{session_id}/notes/{index}", response_model=SessionOut)
async def delete_note(session_id: int, index: int, user: CurrentUser, db: DbDep) -> SessionOut:
    return await svc.delete_note(db, user.id, session_id, index)


@router.post("/sessions/{session_id}/steps/{step}/generate", response_class=StreamingResponse)
async def generate_options(
    session_id: int,
    step: StepKey,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
    sessionmaker: SessionmakerDep,
    body: GenerateRequest | None = None,
) -> StreamingResponse:
    job = await svc.prepare_generate(
        db,
        user_id=user.id,
        session_id=session_id,
        step=step,
        req=body or GenerateRequest(),
        cipher=cipher,
        factory=factory,
        ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints,
    )
    await db.close()
    return sse_response(svc.run_generate(job, sessionmaker))


@router.post("/sessions/{session_id}/convert", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def convert(session_id: int, user: CurrentUser, db: DbDep, body: ConvertRequest | None = None) -> ProjectOut:
    return await svc.convert(db, user.id, session_id, body or ConvertRequest())


@router.get("/sessions/{session_id}/apply-preview", response_model=list[ApplyPreviewItem])
async def apply_preview(
    session_id: int, user: CurrentUser, db: DbDep, project_id: Annotated[int, Query()]
) -> list[ApplyPreviewItem]:
    return await svc.apply_preview(db, user.id, session_id, project_id)


@router.post("/sessions/{session_id}/apply", response_model=ProjectOut)
async def apply(session_id: int, body: ApplyRequest, user: CurrentUser, db: DbDep) -> ProjectOut:
    return await svc.apply(db, user.id, session_id, body)
