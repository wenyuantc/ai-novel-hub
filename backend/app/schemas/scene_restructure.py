"""只读建议和作者显式采纳的场景结构合同。"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.scene import (
    ContentRevision,
    SceneBlocker,
    SceneConflict,
    SceneId,
    SceneRequest,
    SceneTitle,
    SceneWrite,
)

Operation = Literal["split", "merge"]
Signature = Annotated[str, Field(strict=True, pattern="^[a-f0-9]{64}$")]
MAX_CANDIDATES = 32
CandidateId = Annotated[int, Field(strict=True, gt=0, le=MAX_CANDIDATES)]


class RestructureRequest(SceneWrite):
    operation: Operation
    scene_ids: list[SceneId] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def match_operation(self):
        if self.operation == "split" and len(self.scene_ids) != 1:
            raise ValueError("拆分建议需要选定一个场景")
        if self.operation == "merge" and len(set(self.scene_ids)) != 2:
            raise ValueError("合并建议需要选定两个按原顺序排列的相邻场景")
        return self


class RestructureStart(RestructureRequest):
    signature: Signature


class RestructureTaskRequest(RestructureStart):
    project_id: SceneId
    chapter_id: SceneId


class RestructureApply(SceneRequest):
    candidate_id: CandidateId
    signature: Signature
    right_title: SceneTitle = ""
    metadata_source: Literal["left", "right"] | None = None


class RestructureScene(SceneRequest):
    scene_id: SceneId
    title: str
    content: str


class RestructureCandidate(SceneRequest):
    candidate_id: CandidateId
    offset: Annotated[int, Field(strict=True, gt=0)] | None


class RestructureProposalChoice(SceneRequest):
    candidate_id: CandidateId
    reason: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)]


class RestructureChoices(SceneRequest):
    proposals: list[RestructureProposalChoice] = Field(max_length=3)


class RestructurePlan(SceneRequest):
    operation: Operation
    scene_ids: list[SceneId]
    content_revision: ContentRevision
    planning_revision: ContentRevision
    conflicts: list[SceneConflict]


class RestructurePreview(RestructurePlan):
    signature: Signature
    scenes: list[RestructureScene]
    candidates: list[RestructureCandidate]
    blockers: list[SceneBlocker]


class RestructureProposal(RestructureProposalChoice):
    offset: int | None
    before: str
    after: str


class RestructureResult(RestructurePlan):
    signature: Signature
    proposals: list[RestructureProposal]
