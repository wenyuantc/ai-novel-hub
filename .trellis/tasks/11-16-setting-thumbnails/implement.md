# 缩略图实施与验证计划

## 前置门禁

- [x] 总体方案获用户明确批准；笔名子任务已完成独立复核、定向测试与浏览器检查，全量后端回归在独立源码、依赖及数据库快照继续，结果未出前不宣称通过；本任务文档与 manifests有效。
- [x] 核对真实迁移head及备份v10；不得与其他子任务并行修改导入器／exporter／models注册。
- [x] 主会话启动本子任务并派发 `trellis-implement`，完成后派发 `trellis-check`；每次prompt首行使用本任务路径。

- [x] 实施／检查代理已按父任务 `research/implementation-context.md` 加载本领域研究和适用规范；长文分段读取，截断／索引不能算已读。

## 有序实施

1. [x] 新增资源／binding模型和迁移、真实复合FK；先证明同作品／同系列归属、四主体分支和硬删级联。
2. [x] 新增Pillow依赖、标准图codec及有界线程／请求体限制；真实图片测试覆盖解码、方向、透明、帧数及上限。
3. [x] 完成本地与系列图片读写、CAS、缓存metadata、权限复验；接入提升／采用／移回／退出与source_stamp。
4. [x] 把本地binding接入effects-only规则、恢复验证、共享保护与公共投影；确认快照不读取图片bytes且删除／撤销仍可用。
5. [x] 升级单书v11、卡v7和估算／总量校验，完成旧格式与原子恢复；提供供模板使用的无提交资源复制原语。
6. [x] 接入四类列表／详情和系列库的共用UI，完成局部合并、异步身份守卫和375px布局。
7. [x] 定向检查和真实PG／浏览器通过后运行质量门禁，`trellis-check`逐项核对TH-AC1–TH-AC8，写 `verification.md`。
8. [x] 同步README、图片／系列／备份／Agent spec及TODOS本项，交模板子任务。
9. [ ] 按项目收口流程，在确认提交后归档和记录。

## 验证命令

以下新模块在实施时建立；不能将当前不存在的测试写成已通过。

```sh
cd backend
uv run pytest tests/test_setting_thumbnails.py tests/test_setting_image_codec.py tests/test_setting_thumbnails_migration.py tests/test_setting_thumbnails_postgresql.py
uv run pytest tests/test_series.py tests/test_series_projection_guards.py tests/test_series_transfer.py tests/test_collaboration_series.py tests/test_agent_effects.py tests/test_project_transfer.py tests/test_cards.py tests/test_backup_contracts.py tests/test_backup_adapters.py tests/test_migrations.py
uv run ruff check .
uv run pytest
```

```sh
cd frontend
npm test -- src/api/settingThumbnails.test.ts src/utils/settingThumbnail.test.ts src/api/cards.test.ts
npm run lint
npm run build
npm test
```

PG必须真实执行：上传与提升／移回／取消采用、换图与删除／撤销、共享图更新与撤权、硬删与上传、备份与换图的双向顺序；复用现有隔离库守卫和schema helper，不在同一个PG库并行跑互相影响advisory lock的pytest进程。未配置所需环境则记录未执行，不用skip充当成功。

浏览器使用真实服务和隔离账号：四类条目全流程、所有世界观分类、系列库与采用书同步、只读／单书编辑／系列编辑边界、无图与上传失败、保存文字草稿后单独换图、快速切换及账号切换。桌面与375px都留证据。

## 风险与回滚检查点

- 先迁移／codec，再资源CRUD，再系列，随后effects、备份／卡片和UI；任何中间阶段不对外宣称全功能完成。
- effects.py、series_service.py、project_transfer、card_service、公共schema/types及上传中间件需要交叉审查；图片改变不能绕过现有锁序或把二进制塞进通用JSON快照。
- 真实测试图片应由测试代码生成，不能仅用封面魔数夹具冒充可解码图片。
- 有历史资源禁止无条件降级。上传和资源复制失败同事务回滚，旧绑定仍可读；不得用先删旧图后尝试写新图的补偿流程。
- 必须证明新增依赖可在目标Python／容器中编码与解码WebP，并比较无图旧数据行为。
- 新文件与已跟踪文件均做空白门禁；最终只按授权范围暂存并检查cached diff。
