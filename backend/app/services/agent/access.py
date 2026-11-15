"""Agent 写入的锁后能力：作者本人，或原邀请轮次仍有效的 accepted editor。

管理员人工 `projects.edit` 来源不并入：Agent 写入同时要求 `ai.use` 与原授权轮次（grant）。
锁序沿用 `project_access.resolve_access`：认证/User 写屏障 →（可选）发送写门禁 → 作品父行锁 → 锁后重读成员。
"""

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Project, ProjectCollaborator, Task
from app.services import authorization_service as authz
from app.services import project_access
from app.services.agent.transactions import AgentAuthorizationChangedError
from app.services.collaboration_task_access import GRANT_KEY, GrantIdentity, task_grant_identity

EMBEDDING_KEY = "_shared_embedding"

Denied = Literal["changed", "not_found"]


def task_grant(task: Task | None) -> GrantIdentity | None:
    """轮次任务绑定的协作授权；作者任务为 None。键存在但值破损时失败关闭，不能退化为作者。"""
    if task is None:
        return None
    data = task.input if isinstance(task.input, dict) else {}
    grant = task_grant_identity(data)
    if GRANT_KEY in data and grant is None:
        raise AgentAuthorizationChangedError()
    return grant


async def _owned_by_other(db: AsyncSession, user_id: int, project_id: int) -> bool:
    with db.no_autoflush:
        owner = await db.scalar(select(Project.user_id).where(Project.id == project_id))
    return owner is not None and owner != user_id


@dataclass(frozen=True, slots=True)
class AgentEntry:
    """入口锁（或只读）后取得的作品、能力与成员行；不含任何对外输出。"""

    project: Project
    access: Any
    member: ProjectCollaborator | None

    def is_owner(self, user_id: int) -> bool:
        return self.project.user_id == user_id

    def current_grant(self, user_id: int) -> GrantIdentity | None:
        """作者为 None；accepted 成员为当前 `(invitation_id, revision)`（不代表已有 Agent 写能力）。"""
        if self.is_owner(user_id) or self.member is None:
            return None
        return (self.member.invitation_id, self.member.revision)


async def _authenticate_first(db: AsyncSession, user_id: int) -> None:
    """认证门禁与 actor User 屏障是新事务的首批语句（门禁可重入）。

    PostgreSQL Agent 会话为 REPEATABLE READ，快照在首条查询冻结：若先读作品（例如为按 ID 升序预锁作者），
    并发提交的令牌撤销会让随后的 User 屏障序列化失败（409）而不是按原认证快照返回 401。作者 User 由随后
    resolve_access 锁定；此时已持全局授权门禁，任何同时锁多个 User 的事务都要先取该门禁，不取门禁的发送
    lease 只锁单个 User 且其后等待的是发送写门禁（本路径在 User 锁之后才取），因此不会成环（E4i-d 复核）。
    """
    with db.no_autoflush:
        await authz.require_authenticated(db, user_id, write=True)


def check_agent_write(entry: AgentEntry, user_id: int, grant: GrantIdentity | None, *, deny) -> None:
    """锁后能力：作者本人且 grant 为 None；或 accepted editor、`ai.use` 且 `(invitation_id, revision) == grant`。

    管理员人工 `projects.edit` 不并入；失败调用 `deny()`（应抛出）。
    """
    member, project = entry.member, entry.project
    if project.user_id == user_id:
        if grant is not None:
            deny()
        return
    if (grant is None or member is None or not entry.access.can_use_ai
            or member.user_id != user_id or member.status != "accepted" or member.role != "editor"
            or (member.invitation_id, member.revision) != grant):
        deny()


def deny_changed() -> None:
    raise AgentAuthorizationChangedError()


async def lock_agent_entry(
    db: AsyncSession, user_id: int, project_id: int, *, lock: bool = True, visibility_write: bool = False,
) -> AgentEntry:
    """HTTP 入口：作者或 accepted 成员（含 viewer）的私人会话作用域；其余（含仅管理员、已撤权）一律 404。

    `lock=True` 走与 `lock_agent_write` 相同的锁序（认证门禁 → User 升序 →（可选）发送写门禁 → 作品父行 →
    锁后重读成员）；`lock=False` 只读解析（不取写锁，供锁外初检与只读接口）。写能力另由调用方按会话 / 轮次的
    grant 调 `check_agent_write`，失败为 409 `permission_denied`（裁决 14）。
    """
    if lock:
        await _authenticate_first(db, user_id)
    with db.no_autoflush:
        project, access, member = await project_access.resolve_access(
            db, user_id, project_id, for_update=lock, visibility_write=visibility_write and lock,
        )
    if project.user_id != user_id and (member is None or member.status != "accepted" or member.user_id != user_id):
        raise NotFoundError("项目不存在")
    return AgentEntry(project, access, member)


def require_conversation_write(entry: AgentEntry, user_id: int, conversation) -> GrantIdentity | None:
    """会话级写入（改名、删除、发送）：当前轮次 Agent 写能力，且会话属于当前邀请轮次（裁决 4、7）。

    作者会话不带轮次标记；协作者会话须是当前 invitation_id 下创建的。撤权后重邀（新 invitation_id）、
    降为 viewer、或未经协作入口创建（标记缺失）的会话只读。返回本次写入使用的 grant。
    """
    grant = entry.current_grant(user_id)
    check_agent_write(entry, user_id, grant, deny=deny_changed)
    expected = None if grant is None else grant[0]
    if conversation.collaboration_invitation_id != expected:
        deny_changed()
    return grant


def conversation_writable(entry: AgentEntry, user_id: int, conversation) -> bool:
    """对外 `writable`（裁决 23）：与 `require_conversation_write` 同一判定，只返回布尔值，不输出邀请 UUID。"""
    try:
        require_conversation_write(entry, user_id, conversation)
    except AgentAuthorizationChangedError:
        return False
    return True


def conversation_writable_fact(
    *, user_id: int, owner_id: int | None, member_status: str | None, member_role: str | None,
    member_invitation_id: str | None, conversation_invitation_id: str | None,
) -> bool:
    """投影核验快照中按同一 SQL 事实重算 `writable`，与 `conversation_writable` 口径一致：
    作者会话不带轮次标记；协作者须为 accepted editor（`ai.use`）且会话属于当前邀请轮次。"""
    if owner_id is None:
        return False
    if owner_id == user_id:
        return conversation_invitation_id is None
    return (member_status == "accepted" and member_role == "editor" and conversation_invitation_id is not None
            and member_invitation_id == conversation_invitation_id)


async def lock_agent_write(
    db: AsyncSession, user_id: int, project_id: int, *, grant: GrantIdentity | None,
    visibility_write: bool = True, allow_collaborator: bool = True, on_denied: Denied = "changed",
) -> Project:
    """锁后判定 Agent 写能力，返回已加锁的作品。

    - 作者：grant 必须为 None（作者轮次不绑定协作授权）。
    - 协作者：accepted editor，`(invitation_id, revision)` 与 grant 一致，且当前具备 `ai.use`。
      管理员身份（含 `projects.edit`）既不能补足缺失的 grant，也不能替代被降级/撤销的成员身份。
    - `on_denied="changed"`（工具与确认路径的锁后复核）：授权失效抛 `AgentAuthorizationChangedError`，
      终止本轮，不能被工具层吞成普通错误；`"not_found"`：维持 404。
    - 生命周期（删除中 409 / 回收站 404）与认证失效（401）沿用原错误。

    `visibility_write=False` 仅用于调用方已在同一事务持有发送写门禁、或该入口原本不取门禁的场景；
    能力复核本身不可省略。
    """
    # 认证门禁须是新事务的首条语句（见 _authenticate_first）。
    await _authenticate_first(db, user_id)
    try:
        with db.no_autoflush:
            project, access, member = await project_access.resolve_access(
                db, user_id, project_id, for_update=True, visibility_write=visibility_write,
            )
    except NotFoundError:
        # 协作者失去读取来源（撤权、作品对其不可见）属于授权失效；作者的 404 是作品生命周期。
        if on_denied == "changed" and (grant is not None or await _owned_by_other(db, user_id, project_id)):
            raise AgentAuthorizationChangedError() from None
        raise

    def deny() -> None:
        if on_denied == "not_found":
            raise NotFoundError("项目不存在")
        raise AgentAuthorizationChangedError()

    if project.user_id != user_id and not allow_collaborator:
        deny()
    check_agent_write(AgentEntry(project, access, member), user_id, grant, deny=deny)
    return project


def frozen_shared_embedding(execution, binding):
    """协作轮次受理时冻结的作者共享向量选择（E4i-c）；未冻结为 None，破损或身份不符失败关闭。

    任务行冻结了该选择时，同一执行的任何 for_task 守卫都必须把它作为另一份绑定一并重验
    （collaboration_ai_guard 拒绝遗漏任务内绑定的 guard），检索 guard 则反过来以文本绑定作为附加绑定。
    """
    from app.core.errors import AppError
    from app.services import project_ai_config
    from app.services.collaboration_ai_guard import CollaborationSendBlocked

    data = execution.input if isinstance(execution.input, dict) else None
    if data is None:
        raise CollaborationSendBlocked()
    if EMBEDDING_KEY not in data:
        return None
    try:
        selection = project_ai_config.deserialize_shared_embedding(
            data[EMBEDDING_KEY], actor_id=binding.actor_id, project_id=binding.project_id,
            project_owner_id=binding.project_owner_id,
            task_grant=data.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        )
    except AppError:
        raise CollaborationSendBlocked() from None
    if (selection is None or selection.grant_identity is None or selection.grant_identity != binding.grant_identity
            or execution.grant_identity != binding.grant_identity):
        raise CollaborationSendBlocked()
    return selection


def text_guard_extras(execution, binding) -> tuple:
    """文本模型 for_task 守卫的附加绑定：任务冻结了共享向量选择时必须一并带上。"""
    selection = frozen_shared_embedding(execution, binding)
    return (selection,) if selection is not None else ()
