# 阶段 E：项目模型来源内部原语

## 已实现与边界

- 新增 `backend/app/services/project_ai_config.py`，尚无生产路由、任务引擎、发送器或索引调用它；它只准备服务端原语，不能视为 E1–E7 或 Gate E 完成。另在 `ai_config_service.py` 的配置 CRUD 增加选择屏障；旧作者 helper 和既有 AI 行为未因本模块扩权。
- `resolve_project_model(db, actor_id, project_id, model_type, *, cipher, config_id=None, allow_private=False, for_update=False) -> ResolvedProjectModel` 只接受 `text` / `image`：校验真实启用 actor 的 `ai.use`，作者或 accepted editor 可进入。显式 ID 仅允许本人同类型；否则先本人默认/最新配置，仅本人完全没有同类型配置且该 editor `allow_owner_ai` 开启时选择作者的默认/最新配置。管理员人工权限不赋予 AI 能力。
- `ResolvedProjectModel` 冻结保存 project、actor、作品作者、模型类型、`source=actor|owner`、配置 ID 和归属、邀请 UUID/revision、配置指纹；`repr` 不展示字段。密钥不进入绑定；指纹为影响 provider/端点、加密密钥、模型和输出参数的数据库字段生成的 64 个十六进制字符 SHA-256 摘要，配置改动导致旧绑定失效；改显示名称或默认标记不失效。
- `revalidate_project_model(db, binding, *, cipher, allow_private=False, for_update=False, lock_config=True)` 重新读取 actor、当前作品作者、角色与同一邀请轮次、作者授权开关、配置 ID/类型/归属和指纹，不重新选其他配置。未指定 `for_update` 不会替调用者拿项目写屏障；`for_update=True` 使用现有的认证及作品父行屏障。配置默认加锁：PostgreSQL 使用共享行锁，SQLite 用不改业务列的 no-op UPDATE，SQLite 忙时回滚并返回可重试冲突；锁释放由调用方控制。真实发送前的重验必须在认证/共享发送 lease 后执行，并持配置锁至响应头；不能将项目写事务跨模型流保留。
- 向量还必须重查作者当前默认/最新配置 ID，不能以原配置依然存在为由让旧 A 覆盖已切到 B 的共享空间。PostgreSQL 的 `ai_config_service.lock_config_owner` 用与发送门禁不同命名空间 `0x41494346`、按作者 user_id 的 advisory xact 共享/独占锁：向量重验先取共享锁再检查配置，配置 create/update/set-default/delete 在首次配置读写前取独占锁。SQLite 双方对该 User 作 no-op UPDATE，取得同一数据库物理写屏障。不存在默认配置时的新插入同样受门禁约束；不使用全表锁。未来新增配置写路径必须先加入同一门禁，否则不能保证防幻读。
- 既有调用顺序：发送/受控写先认证 User，再取得项目发送门禁/项目行（视场景），之后取作者配置 advisory 及配置行；PostgreSQL 配置 CRUD 仅取该用户配置 advisory、再读写配置，**不反向获取**认证写门禁、项目发送门禁或项目行。SQLite 的 User no-op UPDATE 是全库物理写锁，不得先持另一 SQLite 写事务再请求它。用户删除只允许已无作品的账号并有原 User 保护；直接 SQL、运维脚本或未来新配置写路径不参与该屏障，须单独协调。
- 解析及重验不提交、不请求网络，且不 autoflush 待写对象。它们做密钥解密、provider 类型/配置参数、端点格式和 IP 字面量的本地检查，但不查 DNS、不构建 provider。未来接入方仍必须做既有 DNS/端点、重定向及 provider 校验；既有端点检查与连接之间仍有 DNS 重绑定窗口。失败不能再调用解析选择另一把钥匙，作者模型的错误需安全脱敏。构建负载/提供方后必须在发送 lease 内对同一绑定及可见性戳、场景戳重新核验。
- `resolve_shared_embedding(db, actor_id, project_id, *, cipher, require_vector=False, allow_private=False, for_update=False) -> ResolvedSharedEmbedding` 单独处理共享向量：作者使用自己的向量；编辑者无作者授权返回明确 `mode=keyword` 和 `owner_embedding_not_allowed`，`require_vector=True` 返回 403 同名 code；开启授权后只选作者向量、即使编辑者已有个人向量。作者无配置时关键词模式返回 `embedding_config_missing`，强制重建报原配置缺失。不可用配置直接报错，未来检索降级须显示明确 warning，不能切换到协作者模型。`revalidate_shared_embedding` 校验原模式与授权，不自动升级关键词模式或改变配置；作者修改默认/最新向量后旧绑定失效。原有查看者关键词搜索需走人工读取路径，不应调用要求 `ai.use` 的模型解析器。

## 任务持久化合同

- `serialize_project_model(binding)` 和 `deserialize_project_model(payload, *, actor_id, project_id, project_owner_id, allow_legacy_owner=False)` 只用于服务端 `_project_model`，不得返回 HTTP 或写入日志；反序列化不代替数据库重验。数据为 version=1，字段固定为 project_id、actor_id、project_owner_id、model_type、source、config_id、config_owner_id、grant_identity（null 或仅 invitation_id/revision）、config_fingerprint。禁止额外字段、bool 整数、非正整数、非规范 UUID、非 64 位小写十六进制指纹、来源/归属/身份不一致以及协作者个人 embedding 绑定；错误固定文本不回显 payload。相关数据库主键和 revision 采用 PostgreSQL INTEGER 上界。
- 仅可信数据库确定当前 actor 是作者、调用方确认真正的旧作者任务且显式开启 `allow_legacy_owner=True` 时，传 `PROJECT_MODEL_ABSENT` 返回 `None`。真实 null 不等于缺席，协作者没有绑定始终失败；该结果不自动重选配置，旧作者任务兼容须由未来调用点按历史合同处理。禁止信任客户端的保留键；实际服务端受理应覆盖这些键并持久化。
- 主代理约定的独立 `_collaboration_grant: {invitation_id, revision}` 必须由未来接入方和模型内 grant 双向一致性检查。调用方对 actor/project/owner 的期望值必须来自可信数据库；任何结构合法 payload 在锁后仍可能因配置删除、权限收紧或归属变化失效。
- `serialize_shared_embedding(selection)` 与 `deserialize_shared_embedding(payload, *, actor_id, project_id, project_owner_id, task_grant, allow_legacy_owner=False)` 提供独立的服务端共享向量选择 JSON，version=1 的字段仅为 project_id、actor_id、project_owner_id、grant_identity、mode、reason、model。`keyword` 必须有已知 reason 且 model 为 null；`vector` 的 reason 为 null，model 必须是现有版本化 `embedding` 作者模型绑定；两层 grant、actor/project/owner 与可信任务中的 `_collaboration_grant` 必须一致。禁止额外字段、损坏 UUID、bool/revision 溢出、来源和角色不一致；所有错误统一安全提示。该 JSON 包含内部作者配置 ID/指纹，禁止外发或记录日志。
- 反序列化时，选择字段缺失使用 `SHARED_EMBEDDING_ABSENT`，可信任务的 `_collaboration_grant` 缺失使用 `TASK_GRANT_ABSENT`；实际 null 与缺失不同且不能通过。仅确认真实 actor 为作者并明确开启旧作者兼容时，选择缺失返回 `None`，协作者缺失始终失败。接入方必须在受理时由服务端覆盖保留键，依据可信数据库任务/项目记录传身份和原始 grant；反序列化不鉴权，发送 lease 与受控写事务内都必须按锁序调用 `revalidate_shared_embedding`。关键词选择不会因为作者新增模型自动变成 vector；旧 vector 不能切换配置或空间。

## 尚待实现

- 全部生成、改写、向导、分析、Agent、封面、任务重试/续跑、AI 发送 lease、流、工具/采纳及索引批次尚未调用这些原语；前端、路由、后台任务归属及实际费用提示仍需阶段 E/G 完成。
- 当前没有真实 provider 与撤权同时发生的端到端发送测试，也没有向量索引实际修改/关键词搜索的端到端接入测试；本模块的 PostgreSQL 屏障测试不能代替这些 Gate E 项目。最后新增的用户级 advisory 与配置 CRUD 竞争测试因系统负载按主代理要求尚未执行，不能报告已验证 PostgreSQL 最终实现。日后 `IndexJob` 必须记录服务端共享选择、真实 actor 和 grant。
- `generation_logs.ai_config_id`、Agent effects、task/SSE 输出中作者回退配置的投影和错误脱敏由并行隐私工作或后续完整接入负责，原语本身不输出 HTTP。

## 验证记录

本目录命令均在 `backend/` 下执行，显式设置 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`：

```sh
uv run ruff check app/services/project_ai_config.py tests/test_collaboration_ai_config.py
# All checks passed!（共享选择序列化）

uv run pytest -q tests/test_collaboration_ai_config.py::test_shared_embedding_serialization_roundtrips_all_sources tests/test_collaboration_ai_config.py::test_shared_embedding_deserializer_rejects_tampered_fields_and_grants tests/test_collaboration_ai_config.py::test_deserialized_shared_vector_never_switches_to_new_default tests/test_collaboration_ai_config.py::test_shared_embedding_keyword_payload_cannot_change_mode tests/test_collaboration_ai_config.py::test_shared_embedding_absent_legacy_is_owner_only_and_never_null tests/test_collaboration_ai_config.py::test_binding_roundtrip_owner_and_editor_sources tests/test_collaboration_ai_config.py::test_binding_deserializer_rejects_tampering_and_wrong_identity --tb=short
# 7 passed in 1.93s（共享选择序列化及既有模型绑定聚焦回归）

uv run ruff check app/services/ai_config_service.py app/services/project_ai_config.py tests/test_collaboration_ai_config.py tests/test_collaboration_ai_config_postgresql.py
# All checks passed!

uv run pytest -q tests/test_collaboration_ai_config.py::test_shared_vector_binding_expires_when_default_or_latest_changes tests/test_collaboration_ai_config.py::test_text_binding_does_not_switch_when_default_changes tests/test_collaboration_ai_config.py::test_sqlite_shared_vector_guard_blocks_owner_config_create tests/test_collaboration_ai_config.py::test_binding_roundtrip_owner_and_editor_sources --tb=short
# 4 passed in 1.22s，最终用户级屏障代码

uv run pytest -q tests/test_ai_configs.py::test_default_unique_per_user tests/test_ai_configs.py::test_ai_config_isolation tests/test_ai_config_model_type.py::test_default_unique_per_model_type tests/test_ai_config_model_type.py::test_change_type_moves_default --tb=short
# 4 passed in 2.99s，最终用户级屏障代码

uv run pytest -q tests/test_collaboration_ai_config.py::test_sqlite_config_mutators_respect_shared_vector_guard --tb=short
# 3 passed in 1.10s，update / set-default / delete 均受 SQLite 写屏障保护
```

完整 SQLite 解析、持久化测试在加入 CRUD 用户级屏障前为 `uv run pytest -q tests/test_collaboration_ai_config.py --tb=short`：22 passed in 13.99s；目前新增的完整 SQLite 集合尚未在最终代码上整组重跑。更早的 SQLite/PG/既有配置组合在并行任务流改造接口中间状态得到 60 passed、3 failed（均为 `TaskEngine.subscribe` 缺少 `actor_id`）；该接口同步后同一组合在旧代码上重新得到 64 passed in 92.36s。两次均不是最终用户级屏障的通过证明。

先前 PG 使用 `collab_test` 时确认服务版本 16.15。最终 PG 命令计划使用优先的模型专用库或回退既有标准库：

```sh
AINOVEL_COLLAB_MODEL_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_models' uv run pytest -q tests/test_collaboration_ai_config_postgresql.py --tb=short
# 或仅设置 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test'
```

fixture 分别校验专用库和标准库的本机地址/用户名/库名，使用随机 schema；新增无默认时插入、设置默认、删除，分别验证共享门禁先取与配置修改先提交，以及另一用户配置不受阻塞。由于系统负载，主代理要求中断进行中的 PG 任务并延后错峰验证；相关中断不计通过，最终新增 PG 用例尚未运行。
