# 设定缩略图技术设计

## 状态与依据

整体方案已获批准，本子任务已启动。需求见 `prd.md`；源码与官方图片 API 依据见父任务 `research/thumbnail-design-evidence.md`。以下契约取代调查中仍标为建议的版本号：单书备份 v11、设定卡 v7、Agent effects v7。

## 图片输入与标准化

新增专用图片处理模块，复用现有长度／魔数／base64 初检，但不改变封面原有行为与夹具。使用官方文档确认支持 Python 3.12 的 `Pillow>=13,<14`，实施时生成精确 uv 锁并验证部署环境的 WebP 编解码。

| 项目 | 契约 |
| --- | --- |
| 输入 | JPG／PNG／WebP，≤10 MiB；实际解码格式必须与魔数一致，声明 MIME 存在时一致。拒绝 SVG／GIF／HEIC。 |
| 解码上限 | 单边≤8192、总像素≤16,000,000、正尺寸、单帧；动画 PNG／WebP 明确拒绝。 |
| 输出 | 最长边512px，保持比例、不放大小图；RGB／RGBA WebP，quality85、method4，透明背景保留；输出≤512KiB。 |
| 元数据 | 应用 EXIF 方向后不写入原 EXIF／ICC／XMP、文件名等；只保留规范化图，不保存原图。 |
| 执行 | 昂贵处理在最大并发2的工作线程；处理前轻量鉴权，处理后写锁内重新鉴权和检查主体。 |
| 导入 | 完整校验标准图结构、尺寸、单帧及总量；合法标准 bytes 原样复制，避免反复有损编码。 |

流程：字节／魔数 → 限定格式的 `Image.open` → load前检查尺寸与帧数 → verify并重新打开完整load → 方向处理 → 新输出图等比缩小、编码 → 最终大小校验。保留 Pillow 炸弹检测并把相关警告当错误，不关闭其全局防护。

项目与系列 PUT 路径都纳入 ASGI 请求体限制（10MiB＋1MiB multipart开销），并保留路由 `limit+1` 读取兜底。解码异常和业务上限使用中文400；主体／权限错误保持既有404／403；无权限不得消耗解码资源。

## 存储模型

新增 `app/models/setting_images.py`，不在四个实体模型上存 bytes。

### 不可变 `setting_image_resources`

`id` UUID，`project_id`／`series_id` 恰好一个非空，分别为 CASCADE 生命周期FK；`mime_type,data,width,height,byte_size,sha256,created_at`。建立 `(project_id,id)` 和 `(series_id,id)` 唯一键。创建后内容及归属不修改，新上传创建新UUID。

### 当前 `setting_thumbnail_bindings`

自增 `id`、`image_resource_id`、`project_id`、`series_id`、四个可空实体FK及 `series_entry_id`。

- 本地分支：project非空、series与series_entry为空，四个本地主体恰好一个非空。
- 系列分支：series与series_entry非空、project和四本地主体为空。
- 每个主体列独立 UNIQUE，四类 `(project_id,entity_id)` 指向本书实体，系列 `(series_id,series_entry_id)` 指向共享条目，均 CASCADE。
- 两个资源复合FK `(project_id,image_resource_id)`／`(series_id,image_resource_id)` 验证资源同作用域，使用可延迟 NO ACTION，配合上述CHECK防止NULL绕过。
- 项目／系列生命周期FK显式CASCADE。组织、职业补 `(project_id,id)` 唯一索引，SeriesEntry补 `(series_id,id)` 唯一索引，不重建旧父表。

替换／移除只变更当前绑定；旧资源继续归属原项目／系列。删除实体级联删绑定，Agent历史可恢复原资源引用。所属项目／系列永久删除才清理资源，软回收保留。此保留规则会增加数据库及整库备份体积，首版不引入后台GC、图库或全局去重。

## API、版本和缓存

| 路径 | 行为 |
| --- | --- |
| `/api/v1/projects/{pid}/settings/{kind}/{id}/thumbnail` | GET当前有效图；PUT multipart file＋`expected_version`（无图用空值）；DELETE用期望版本。kind严格为四类，主体同项目。 |
| `/api/v1/series/{sid}/entries/{eid}/thumbnail` | GET共享当前图；PUT／DELETE同时提交现有 `expected_revision` 及图片期望版本，沿用系列编辑权限和CAS。 |

实体响应增加可空 `thumbnail: {version,mime_type,width,height}`，版本是资源UUID。角色与世界观额外投影 `thumbnail_editable` 布尔值：本地条目且当前 actor 可编辑作品时为 true，共享投影为 false，且与当前是否有图无关。仅有单书权限者的 origin 可能被隐去，因此前端不能据 origin 推断可写性；该标记不包含系列身份。组织与职业仍沿用作品编辑权限。PUT／DELETE只返回图片metadata（系列另返回新revision），不覆盖实体文本表单。

本地更新在锁内比较当前version，旧值409 `thumbnail_changed`；重复删除已无图可成功，但不能删除另一张新图。系列旧revision沿用 `stale_revision`。新条目先保存获得实体ID，再提供图片编辑。

GET始终根据当前主体和当前绑定解析，不能凭旧version读取保留的历史资源。书内共享投影的URL只包含本书kind/id；先鉴权再计算304，`Cache-Control: private,no-cache`，ETag基于当前UUID。v仅刷新缓存，不是授权依据。列表只批量查metadata列，不加载bytes，不造成逐项N+1。

## 权限与系列转换

本地读取用 `content.read`，写用 `content.edit` 并持活跃父项目锁。锁内再次检查共享绑定；已共享的角色／世界观只能从系列入口改图。只有书内访问权者能看该书已采用条目的权威图，不能取得系列库能力或系列身份。

系列更新沿现有 `_edit`：真实actor门禁 → 系列锁 → 排序的各书门禁／父锁 → 图片绑定；推进series.revision。不采用项目锁后再等待系列锁的反向顺序，也不为图片推进AI可见性或剧情事件版本。

| 操作 | 图片结果 |
| --- | --- |
| 本地提升到系列 | 复制当前标准图到系列资源，建立canonical绑定，移除本地图绑定；原本地资源保留供历史撤销。source_stamp增加图片版本／hash。 |
| 采用共享条目 | 只增加既有采用关系，图片读取canonical，不复制每书绑定。 |
| 取消采用／退出系列 | 保持现有删除投影语义；canonical图不变，不能改成自动留下本地副本。 |
| 移回某书 | 在删除共享条目前，复制当前图到目标项目新资源并绑定本地实体；不修改原资源归属。 |
| 删共享条目 | 依现有依赖预检处理投影，删当前绑定，资源随系列保留至系列永久删除。 |

## 删除、Agent效果与撤销

两表均在单书 registry 的 NOT_EXPORTED；资源不进入 Agent effects。仿照 effects 的 tasks 专用规则，把本地binding小行单独加入 EFFECT_TABLES／_RULES，scope限定当前project且series为空，恢复顺序在四类父实体之后。

- 提升effects格式6→7，兼容无图片的旧效果；不能因此让旧文本恢复清空后来上传的图。
- before／after校验仅本地分支、正确项目／唯一主体、资源仍属当前项目，父实体存在或由本次恢复；数据库FK作为第二道保证。
- 带图设定删除后撤销恢复原实体和绑定；后来新加图导致旧新建实体撤销冲突，保留 `_check_dependents` 行为。
- `_preflight_series_effects` 加入binding指向的角色／世界观，阻止旧撤销在提升后插入私图覆盖canonical。
- effects仅存小行，snapshot不查询资源bytes。对ChangeOut、模型预览和面板作专门公共投影，不暴露内部资源归属／引用，也不把base64或图片内容送模型；可显示“缩略图随条目删除／恢复”的文字摘要。

## 备份、卡片与模板

| 消费方 | 契约 |
| --- | --- |
| 单书备份 v11 | 继承笔名v10，兼容v1–v10；顶层 `thumbnails:[{ref:{kind,id},mime_type,data}]` 仅本书当前有效图，包括已采用共享图；不带资源UUID、series身份、旧图、未采用条目。 |
| 一致性 | 开始记录有效 `ref→UUID` 清单，结束重新解析比对并沿用原授权／生命周期复验；图替换、采用关系变化使本次导出失败，不拼混合版本。 |
| 恢复 | 先校验图片、重复／缺失ref和总大小，再用导入后的实体映射创建本书新资源／binding；一次事务。两表不进通用RULES，避免未导出资源FK被清空。 |
| 体积 | 现有100MiB备份总上限包含图片base64；估算增加当前图大小，超限明确拒绝。 |
| 设定卡 v7 | 仅扩展原已支持的角色／组织／职业卡，兼容v1–v6；可选当前图片、显式导入选择。卡紧凑JSON总上限统一为16MiB，前后端预检和请求体上限同步。所选图片只写新建／重命名新建条目；原有同名角色／组织跳过及职业复用不修改已有图。无图片字段／未选导入图片不清空目标已有图；不新增世界观卡协议。 |
| 模板 | 读取当前标准图并复制到模板独立资源；复用时复制到新项目，不引用可删除的来源资源。 |
| 整库备份 | 原生数据库备份保留当前及历史图片；恢复需验证二进制与FK，新增表无需手工白名单。 |

开放设定API首版不增加图片字段或管理／裸资源接口。EPUB、DOCX等不新增设定图嵌入。模板资源不成为本表的第三个作用域。

## 前端与实现边界

抽出共用 `SettingThumbnail` 展示与图片编辑组件，接入角色卡片／抽屉、世界观列表／详情、组织树／详情、职业卡／抽屉及系列库／编辑器。固定尺寸、lazy图片、名称alt、失败占位；只读用户看图而无操作。

每次请求捕获actor、project scope／series授权代次、kind/id及组件请求代次；切换、撤权、卸载后失效。成功只合并目标图片metadata，保留文字草稿；blob URL及时释放，不用整页reload代替局部同步。共享图改变使已加载相关资源版本失效，重新进入／刷新采用书得到新图。

## 迁移与回滚

笔名完成后，按真实Alembic head新增图片迁移，新增表和必要唯一索引；不动态引用未来ORM创建旧结构。同步APP_SCHEMA、注册表、effects版本与卡片版本。

有图片资源或绑定时拒绝有损downgrade；优先退应用入口并保留资源。v11／卡v7由新应用读取，旧应用必须拒绝未知版本，不能宣称向旧写入器兼容。Pillow／WebP打包和真实PG外键级联的实际验证记录见 `verification.md`。
