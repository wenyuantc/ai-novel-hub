from dataclasses import dataclass, field

from fastapi import APIRouter

from app.services.project_transfer.rules import TableRule, TransferSnapshot, TransferValidator
from app.services.task_engine.registry import TaskHandler


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    description: str
    version: str
    routers: tuple[APIRouter, ...] = ()
    task_handlers: tuple[TaskHandler, ...] = ()
    transfer_rules: tuple[TableRule, ...] = ()
    not_exported: dict[str, str] = field(default_factory=dict)
    transfer_snapshot: TransferSnapshot | None = None
    transfer_validator: TransferValidator | None = None
