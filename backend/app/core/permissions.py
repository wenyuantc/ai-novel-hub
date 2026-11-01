"""唯一的后台操作目录；特殊超管身份不属于可复制/委派权限。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Permission:
    code: str
    label: str
    group: str
    dangerous: bool = False
    requires: tuple[str, ...] = ()


PERMISSIONS = (
    Permission("dashboard.read", "查看全站统计", "首页"),
    Permission("users.read", "查看账号与基础用量", "账号"),
    Permission("users.create", "创建普通账号", "账号", requires=("users.read",)),
    Permission("users.status", "启用/禁用普通账号", "账号", True, ("users.read",)),
    Permission("users.reset_password", "重置普通账号密码", "账号", True, ("users.read",)),
    Permission("users.delete", "删除普通账号", "账号", True, ("users.read",)),
    Permission("quotas.read", "查看用户配额", "配额", requires=("users.read",)),
    Permission("quotas.update", "修改普通账号配额", "配额", True, ("quotas.read",)),
    Permission("projects.read", "查看全站作品人工内容", "作品"),
    Permission("projects.edit", "人工内容维护（含子资源增删/版本恢复）", "作品", True, ("projects.read",)),
    Permission("projects.recycle", "将作品移入回收站", "作品", True, ("projects.read",)),
    Permission("recycle_bin.read", "查看作品回收站", "回收站"),
    Permission("recycle_bin.restore", "恢复作品", "回收站", True, ("recycle_bin.read",)),
    Permission("recycle_bin.purge", "彻底删除作品", "回收站", True, ("recycle_bin.read",)),
    Permission("tasks.read", "查看全站任务安全元数据", "任务"),
    Permission("tasks.cancel", "受理全站任务取消", "任务", True, ("tasks.read",)),
    Permission("audit.read", "查看操作日志摘要", "日志"),
    Permission("audit.cleanup", "清理操作日志", "日志", True, ("audit.read",)),
    Permission("settings.read", "查看系统运行设置", "系统设置"),
    Permission("settings.update", "修改系统运行设置", "系统设置", True, ("settings.read",)),
    Permission("workshop.read", "查看工坊管理与待审核内容", "工坊"),
    Permission("workshop.approve", "通过投稿", "工坊", True, ("workshop.read",)),
    Permission("workshop.reject", "拒绝投稿", "工坊", True, ("workshop.read",)),
    Permission("workshop.publish", "官方发布内容", "工坊", True, ("workshop.read",)),
    Permission("workshop.update", "修改工坊内容元数据", "工坊", True, ("workshop.read",)),
    Permission("workshop.hide", "下架工坊内容", "工坊", True, ("workshop.read",)),
    Permission("workshop.unhide", "上架工坊内容", "工坊", True, ("workshop.read",)),
    Permission("workshop.delete", "删除工坊内容", "工坊", True, ("workshop.read",)),
    Permission("global_skills.create", "创建/导入全局Skill", "全局Skill", True),
    Permission("global_skills.update", "修改全局Skill", "全局Skill", True),
    Permission("global_skills.references", "替换全局Skill参考资料", "全局Skill", True, ("global_skills.update",)),
    Permission("global_skills.delete", "删除全局Skill", "全局Skill", True),
    Permission("global_skills.restore_builtin", "恢复内置Skill", "全局Skill", True, ("global_skills.update",)),
)
PERMISSION_BY_CODE = {item.code: item for item in PERMISSIONS}
PERMISSION_CODES = tuple(PERMISSION_BY_CODE)
BUILTIN_ROLES = {
    "super_admin": ("超级管理员", "全部管理能力；角色与授权不可委派", PERMISSION_CODES),
    "content_admin": (
        "内容管理员", "全站人工作品维护、回收恢复与工坊管理",
        tuple(code for code in PERMISSION_CODES if code.startswith(("projects.", "workshop.")))
        + ("recycle_bin.read", "recycle_bin.restore"),
    ),
    "reviewer": (
        "审核员", "工坊投稿审核与上下架",
        ("workshop.read", "workshop.approve", "workshop.reject", "workshop.hide", "workshop.unhide"),
    ),
    "user": ("普通用户", "个人资源与公开功能，无后台管理能力", ()),
}


def validate_permissions(values: list[str]) -> list[str]:
    if len(values) != len(set(values)) or any(code not in PERMISSION_BY_CODE for code in values):
        raise ValueError("权限包含重复或未知操作")
    selected = set(values)
    for code in selected:
        if not set(PERMISSION_BY_CODE[code].requires) <= selected:
            raise ValueError(f"权限 {code} 缺少明确的前置读取权限")
    return [code for code in PERMISSION_CODES if code in selected]
