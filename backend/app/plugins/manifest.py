from dataclasses import dataclass

from fastapi import APIRouter

from app.services.task_engine.registry import TaskHandler


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    description: str
    version: str
    routers: tuple[APIRouter, ...] = ()
    task_handlers: tuple[TaskHandler, ...] = ()
