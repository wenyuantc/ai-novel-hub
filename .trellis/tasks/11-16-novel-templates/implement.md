# 小说模板实施与验证计划

## 前置门禁

- [x] 整体方案已经明确批准，笔名和缩略图子任务已通过阶段性检查；核对实际迁移head、当前v11及各内部接口。
- [x] 本任务三份规划文档和真实 manifests有效；主会话启动本子任务后派发 `trellis-implement`，检查使用 `trellis-check`，prompt均以本任务Active task开头。

- [x] 实施／检查代理已按父任务 `research/implementation-context.md` 加载本领域研究和适用规范；长文分段读取。

## 有序实施

1. [x] 建立严格v1快照DTO、局部ref编解码和图校验；验证四类身份、同名不串联、字段／分类／AI links依赖、关系类型完整快照／有序端点和拒绝未知内容。
2. [x] 新增模板／图片／成功收据三表及迁移、registry排除；证明模板和来源生命周期独立、收据不会被模板或结果作品删除连带清空。SQLite AUTOINCREMENT／PG sequence禁止删除后身份复用，真实迁移验证旧ID不能命中新对象。
3. [x] 实现一致性读取、当前档案冻结、依赖预览、体积估算、source_stamp及owner-only保存／替换CAS。
4. [x] 提取必要无提交原语并实现原子创建，接通当前笔名选择、图片独立复制、baseline和图校验；保持原卡片／大纲／普通创建提交行为。
5. [x] 完成User屏障下的配额与成功意图重放、请求摘要冲突和结果已删分支，禁止receipt→Project锁序；补提交后响应丢失的验证。
6. [x] 实现模板库、作品保存弹窗、新建模式选择、依赖补选、冲突和网络重试、身份隔离及375px交互。
7. [x] 定向测试、真实PG与浏览器完成后运行质量门禁；`trellis-check`对NT-AC1–NT-AC10逐项验证，写 `verification.md`。
8. [x] 同步README、新模板spec和相关跨层契约，再更新TODOS本项；交总任务完成全组合验收。

## 验证命令

定向测试和真实执行结果见 `verification.md`；完整套件另按隔离数据库分组运行。

```sh
cd backend
uv run pytest tests/test_novel_templates.py tests/test_novel_template_snapshot.py tests/test_novel_templates_migration.py tests/test_novel_templates_postgresql.py
uv run pytest tests/test_cards.py tests/test_card_ai_visibility.py tests/test_outline_transfer.py tests/test_project_transfer.py tests/test_setting_fields_categories_roundtrip.py tests/test_user_quotas.py tests/test_series_transfer.py tests/test_migrations.py
uv run ruff check .
uv run pytest
```

```sh
cd frontend
npm test -- src/api/novelTemplates.test.ts src/utils/novelTemplateSelection.test.ts src/utils/novelTemplateCreation.test.ts
npm run lint
npm run build
npm test
```

最小功能矩阵：仅配置、零条目配置目录、四类设定及typed字段、同名ID、引用环、职业／阶段／父级、部分大纲、当前planned值、never／隐藏字段、独立图片、原书含正文与历史哨兵、风格被更改／删除。失败注入在图／图片最后阶段，证明所有新数据回滚；不要通过猴补commit制造原子性假象。

关系类型专门验证：全局完整一致才复用，分类／称呼／sort变化回退本书类型；项目类型与同称呼全局仍独立可编辑；父亲／子女方向不反转；全局回退撞项目唯一键时零副作用。配置未勾选仍携带实际所需类型目录，source_stamp包含完整定义。

身份专门验证：删除当前最大ID模板后再新建，旧ID＋revision的PATCH、DELETE、snapshot替换和新intent建书均不能命中新模板；同旧成功intent仍只返回原活跃结果。覆盖SQLite真实迁移和PG路径，不仅检查ORM声明字符串。

PG实际并发：预览后源条目／风格／图片变化，保存与来源回收／系列更新，模板CAS冲突，同key并发只创建一次，不同key争最后额度，同key跨用户隔离，模板删除后成功重放，结果软删／恢复／彻删后的重放，系统purge与重放无死锁。复用专用本机隔离库和schema守卫，缺环境明确记未运行。

真实浏览器：作者保存选择、查看并补齐依赖、源变化冲突后保留草稿、模板库改名／删除／替换、新建表单笔名选择、超时同key重试和新书概览、账号切换／撤权、桌面和375px。查看者及编辑者没有保存作者私人模板入口，直接请求也须拒绝。

## 风险、检查点与回滚

- snapshot whitelist和无提交bootstrap是核心风险；先证明数据语义，再接API和UI。
- 字段、分类、AI策略、时间线、系列、本地图片、quota和现有创建入口都是回归面，不能只验证新模板API的成功路径。
- 创建幂等收据与Project硬删有真实FK更新顺序，必须双事务gate验证；不能用SQLite成功推断PG无死锁。
- 任何已有模板／图片／收据时拒绝删表回退；保留成功意图去重事实。恢复生产数据或实际部署另按明确授权操作。
- README和spec以最终已实现行为更新；未通过的验收不勾选。记录命令、退出码和PG／浏览器证据，不能把当前规划文档当验收记录。
