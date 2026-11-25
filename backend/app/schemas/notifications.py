from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool

Kind = Literal["invitation", "task", "budget", "consistency"]


class PreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class NotificationItem(BaseModel):
    id: UUID
    kind: Kind
    title: str
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationItem]
    next_before: str | None


class UnreadCount(BaseModel):
    count: int


class InvitationTarget(BaseModel):
    kind: Literal["invitation"] = "invitation"
    scope: Literal["project", "series"]
    invitation_id: str


class TaskTarget(BaseModel):
    kind: Literal["task"] = "task"
    task_id: int
    task_key: str


class BudgetTarget(BaseModel):
    kind: Literal["budget"] = "budget"
    project_id: int | None
    currency: Literal["CNY", "USD"]
    budget_key: str


class ConsistencyTarget(BaseModel):
    kind: Literal["consistency"] = "consistency"
    project_id: int
    run_id: int


NotificationTarget = InvitationTarget | TaskTarget | BudgetTarget | ConsistencyTarget
