"""后台任务受理开关。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.services.system_settings_snapshot import effective_settings


class TaskAdmissionClosed(AppError):
    status_code = 503

    def __init__(self) -> None:
        super().__init__(
            "系统正在维护，暂不受理新任务，请稍后再试",
            extra={"code": "task_admission_closed"},
        )


async def require_task_admission_open(db: AsyncSession) -> None:
    """读取持久开关。最终受理时，调用方须持有根事务的授权写门禁直到提交。"""
    if (await effective_settings(db)).task_admission_closed:
        raise TaskAdmissionClosed()
