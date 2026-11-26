from fastapi import APIRouter, Depends, Response

from app.core.deps import DbDep, EditorUser, require_permission
from app.schemas.publication_terms import PublicationTermsOut, PublicationTermsUpdate
from app.services import publication_terms as svc

router = APIRouter(tags=["publication-terms"])


@router.get("/publication-terms", response_model=PublicationTermsOut)
async def read_personal(user: EditorUser, db: DbDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await svc.read_terms(db, user.id)


@router.put("/publication-terms", response_model=PublicationTermsOut)
async def update_personal(body: PublicationTermsUpdate, user: EditorUser, db: DbDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await svc.replace_terms(db, user.id, body)


@router.get("/admin/publication-terms", response_model=PublicationTermsOut,
            dependencies=[Depends(require_permission("publication_terms.read"))])
async def read_global(user: EditorUser, db: DbDep):
    return await svc.read_terms(db, svc.GLOBAL)


@router.put("/admin/publication-terms", response_model=PublicationTermsOut,
            dependencies=[Depends(require_permission("publication_terms.update"))])
async def update_global(body: PublicationTermsUpdate, user: EditorUser, db: DbDep):
    return await svc.replace_terms(db, user.id, body, global_scope=True)
