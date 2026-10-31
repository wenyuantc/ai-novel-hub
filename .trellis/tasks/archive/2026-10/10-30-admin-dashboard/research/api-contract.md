# 管理员首页 HTTP 契约（实施冻结版）

所有端点位于 `/api/v1/admin/dashboard`，GET且每个端点均使用AdminUser。所有时间戳为UTC ISO datetime，timezone固定 `Asia/Shanghai`；day为后端计算的北京时间 `YYYY-MM-DD`，前端不能再做本地时区换日。字段全部为snake_case，计数是非负整数，净变化允许负值；null与0不同。

## GET /overview

`{as_of, tracking_since, timezone, users, projects, chapters, total_words, pending_submissions}`。

users包含所有账号；projects/chapters/total_words包含回收站以及尚未物理删除的删除中作品。正文版本不重复计数。pending_submissions只统计工坊pending。

## GET /tasks

`{as_of, tracking_since, timezone, baseline_tasks, new_tasks, queued, running, paused, succeeded, failed, cancelled, interrupted, failed_24h, interrupted_24h, removed_unfinished}`。

queued/running/paused按实际Task表的全站现存任务计。new_tasks按独立统计记录中origin=new计，baseline_tasks为初始化存量，二者不冒充上线前完整历史。succeeded/failed/cancelled/interrupted只计上线后真实记录的终态。failed_24h/interrupted_24h为滚动24小时事件数；removed_unfinished为尚未记录终态即被移除的统计数，不伪造失败或成功。

## GET /history?days=7|30|90

默认days=30；其他值422。

```text
{
  as_of, tracking_since, timezone, days, start_date, end_date,
  points: [
    {
      day,
      captured_at: datetime|null,
      totals: {users, projects, chapters, total_words, pending_submissions}|null,
      tasks: {created, succeeded, failed, cancelled, interrupted}|null,
      partial: boolean
    }
  ],
  comparison: {from_day, to_day, users: int|null, projects: int|null, chapters: int|null, total_words: int|null}
}
```

points覆盖完整所选日期序列，含当天。规模取每天最后一次成功采样。上线前totals/tasks为空；无规模采样时totals为空；如当日有真实任务事件则tasks仍返回已记录数并标partial；既无采样又无事件时tasks为空，不补零。有采样的已观测日可返回零事件。首日/当日/未完整覆盖日标partial。

comparison严格比较所选首尾日期的有效规模采样，两端缺任一或不足两个有效点时各净变化字段为null，不擅自缩短区间或补0。图表在缺失点处断线。空值、初始化、采样时间及日内未完结都用中文说明，规模净变化不命名为新增或写作产出。

三个端点独立加载、失败与重试，不要求不同端点的as_of是同一数据库快照。API不返回用户/作品/任务内容，不新增全站任务处置入口。前端使用现有request封装，可传AbortSignal；保持公开Task类型不变。
