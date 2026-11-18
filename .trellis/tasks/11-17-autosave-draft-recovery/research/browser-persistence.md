# 浏览器草稿持久化调研

日期：2026-11-17。用户已选方案 A：恢复发生在联网重新打开应用并完成身份／访问校验之后。以下为技术设计依据，不代表已经实现或通过浏览器验收。

## 推荐与适用边界

采用原生 IndexedDB 保存结构化草稿，封装为小型、可注入 `IDBFactory` 的仓库；生产运行时不新增存储库。服务端保存继续使用现有 API，不引入 Service Worker、后台同步、离线登录或离线应用资源缓存。

代码依据：`frontend/src/api/richDocument.ts:8–31` 的完整文档包括正文、格式、区块和标记；`frontend/src/components/scenes/richDocumentCapacity.ts:4–7,28–59` 允许较大的区块正文及元数据。以 `localStorage` 同步序列化整份文档不适合作为默认正文保护机制；Web Storage 的容量也不足以覆盖全部合法富文档。容量依据见 [MDN 存储配额说明](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria)。

## 官方资料约束及设计落点

| 证据 | 对设计的约束 |
| --- | --- |
| [Using IndexedDB](https://developer.mozilla.org/en-US/docs/Web/API/IndexedDB_API/Using_IndexedDB)：请求成功与事务完成不同；浏览器关闭会中止未完成事务；不能依靠卸载事件才写数据库。 | 编辑期间持续写入，只有事务 `complete` 才推进“本地已保留”的序号；替换、清理须同事务，不能先清空再另开事务写入。 |
| [IDBDatabase.transaction](https://developer.mozilla.org/en-US/docs/Web/API/IDBDatabase/transaction)：支持 `durability` 提示。 | 优先请求 `strict`，不支持时使用标准默认事务并仍等待完成；不使用非标准 `readwriteflush`，不承诺操作系统崩溃或磁盘故障下绝对零损失。 |
| [beforeunload](https://developer.mozilla.org/en-US/docs/Web/API/Window/beforeunload_event)：移动设备上并不可靠，页面可见性变化是更合适的辅助保存信号。 | `visibilitychange`／`pagehide` 仅催促已存在队列；持久化不等到关闭。未同步或本地写入未完成时保留原离开提醒。 |
| [存储配额与驱逐](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria)：写入可能因配额失败，浏览器也可能回收站点数据。 | 处理 `QuotaExceededError`、权限和关闭错误；不静默删除未同步草稿腾空间。浏览器清除站点数据无法由本功能恢复。 |
| [Broadcast Channel](https://developer.mozilla.org/en-US/docs/Web/API/Broadcast_Channel_API)：用于同存储分区页面通信。 | 只作为“草稿／服务端内容可能变化”的通知，消息不携带正文、也不能作为写入锁或版本权威；禁用该 API 时保存正确性不变。 |
| [sessionStorage](https://developer.mozilla.org/en-US/docs/Web/API/Window/sessionStorage)：新页面可能复制 opener 的会话存储。 | 不从 `sessionStorage` 复用写入者 ID；每个新页面运行实例生成独立随机 ID，刷新后将旧记录作为恢复候选，避免复制标签页共用草稿槽。 |

## 仓库边界建议

- 一条记录对应一个账号、作品、章节、目标（章标题或场景）及写入者；章标题与场景正文分开记录，避免切场景后旧标题副本回流。
- 账号和作品可附带服务端 `created_at` 帮助区分历史对象，但时间戳不是不可复用身份；标题也必须绑定该章一个真实场景 ID，后端校验目标归属。强身份细节由 `backend-save-contract.md` 决定。
- 每条记录保存稳定 `draft_id`、`writer_id`、模式版本、单调 `record_seq`、已确认服务端基线、当前草稿、富文档编辑证据，以及可能已发送的请求快照／阶段。record_seq 覆盖输入和基线／attempt 等记录变化，区别于只随输入变化的 edit_seq；不能只存最新正文字符串。
- 第一次有效编辑立即排队写入；已有本地事务执行时合并待写快照，完成后立刻写入最新序号。避免长尾防抖导致连续输入一直不持久化。同一记录单写者、事务内比较序号，旧写入不能回退新记录。
- 清理条件绑定 `draft_id + record_seq + writer_id`，在同一事务中读条件并删除／标记；不能按账号／章节批量删草稿，不能让网络旧响应清掉新序号。ACK 先匹配当前 attempt 再经控制器发布完整新快照，不直接修补持久化基线。
- 恢复另一记录时先保存当前编辑区，再原子建立所选来源的副本；默认保留来源，只有显式舍弃才按被查看序号删除，来源已变化则重新比较。刷新、多个标签页与复制标签页均不原地抢占其他写入者的记录。恢复暂停状态也须持久化。
- 未同步草稿不自动按时间淘汰；只在对应快照已确认同步或作者明确舍弃时处理。登出清理内存、订阅和定时器；持久草稿按账号隔离，下一次同账号联网且仍有访问权限才显示。
- 从 `unknown` 解码，验证模式、目标身份、数字范围、数组和富文档结构；未知版本／损坏记录不导入编辑器、不删除原记录，可明确提示并让作者导出原始数据或舍弃。
- 页面内错误通过状态显示，连续输入期间不逐字弹 toast；本地失败不禁止手动服务端保存，但不能显示“本地已保留”。

## 测试方法

新增开发依赖 `fake-indexeddb`，只用于 Node/Vitest 中的事务测试。使用独立 `new IDBFactory()` 注入仓库，避免跨测试全局污染。其官方说明明确数据只存在内存，所以不能替代真实浏览器的关闭／重开与配额行为验收。依据：[项目官方 README](https://github.com/dumbmatter/fakeIndexedDB)。实现时选择兼容现有 Node 的稳定版本并提交 lockfile，本次不安装依赖。

必须验证：事务 abort 后旧稿仍在；请求 success 后事务失败不得报已保留；乱序／并发序号；恢复副本与来源清理原子性；陈旧删除；损坏数据；blocked/versionchange/意外 close；禁用广播仍可靠。真实浏览器另外用同一 profile 关闭并重开页验证磁盘持久化，使用两个标签页和两个独立客户端验证本地隔离及服务端冲突。
