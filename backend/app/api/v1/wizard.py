from typing import Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SessionmakerDep, SettingsDep
from app.schemas.chapter import WizardRequest
from app.services import wizard_service

router = APIRouter(prefix="/projects/{project_id}/wizard", tags=["wizard"])


@router.post("/{kind}", response_class=StreamingResponse)
async def run_wizard(
    project_id: int,
    kind: Literal["outline", "characters", "world"],
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    factory: FactoryDep,
    sessionmaker: SessionmakerDep,
    body: WizardRequest | None = None,
) -> StreamingResponse:
    job = await wizard_service.prepare_wizard_job(
        db,
        user_id=user.id,
        project_id=project_id,
        kind=kind,
        req=body or WizardRequest(),
        cipher=cipher,
        factory=factory,
        ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints,
    )
    await db.close()
    return sse_response(wizard_service.run_wizard_job(job, sessionmaker))
