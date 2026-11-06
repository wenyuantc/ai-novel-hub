# 隔离验证环境

更新：2026-11-06。仅测试资料；未请求用户8000服务、未操作真实数据库／模型。

## 当前分类环境

- ROOT：`/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/ainovel-categories-browser-1onr9vwt`，指针`/tmp/ainovel-categories-browser-root.txt`。
- server.py仅使用本ROOT/test.db、FakeLLM与测试密钥，不读产品.env。四实体AUTOINCREMENT最终发布后才新建库，未沿用旧0035库。所有seed先验证health中的ROOT及model=fake。
- 8014曾为读取失败验收准备停止；浏览器动作被权限策略拒绝，请求未发出。随后以最终代码恢复，当前统一exec session81291（旧82656进程已停止）。前端5179为session51809，仅代理8014。两服务暂保留供用户检查，不能据此声称剩余UI已通过。
- seed.py创建项目1“字段与分类隔离验收”、四实体／四类型字段、5／20／21章；seed_categories.py创建ALL／ANY及221个跨批分页地点；真实API与界面修改均只在此库。
- seed_cards.py追加无字段值合成源角色／组织／关联职业，以及项目2“卡片v5标签隔离目标”的同名目标。浏览器实际v5 skip／rename导入与三类标签保留已验证；fixture.json／cards_fixture.json记录实际ID。
- fields_author用于编辑；fields_reader仅projects.read，创建／授权已获用户明确批准，仅存在临时库。分类只读界面尚未登录验收；字段阶段只读UI已在旧隔离库验收，不能混作本次分类证据。
- 浏览器仅本任务IAB；临时viewport已reset。5179保存权限／管理策略仍拒绝，用户重新授权并确认解除后工具仍拒绝，已停止CUA且不绕过。未验项及截图见category-browser-verification.md。

## 旧字段环境与最终证据

- 首轮ROOT ainovel-fields-browser.4xw0zav2、最终字段ROOT ainovel-fields-final-oq79dj9d均在同一系统临时目录；旧test.db保留，服务与旧IAB标签已停止／关闭。
- 字段最终2146通过／旧0031可选PG一项skip，前端129文件914通过。间距、四类型／四实体／早晚章、实际FakeLLM消息、只读、引用分页及v4显式卡片映射见implementation-log和字段check报告。
- 当前分类最终完整后端2408通过／1旧skip／1既有SDK警告，实际exit0；前端136文件986通过，lint／build／diff为0。父级新增v5导入后相同时点集成独立1项及直接相关40项通过，含实际PG且无skip／warning；不把旧全量结果冒称包含后加测试。
- 字段截图：`/Users/wenyuantc/.codex/visualizations/2026/11/05/01a1ab26-4f64-7ac3-96ab-08ad636d04db/setting-fields/`；分类截图位于同根setting-categories/。

## PostgreSQL

- 容器ainovel-fields-verify-20261105，PostgreSQL16，本机32814，数据库fields_verify、用户field_test，仍保留本任务专用容器。
- 凭据仅在0600文件`/tmp/ainovel-fields-01a1ab26-postgres.env`，不记录URL／密码到仓库。每测试随机schema并finally清理；本任务全部PG在最终完整轮实跑。
- 不设置旧AINOVEL_POSTGRES_TEST_URL冒充ainovel_test；旧0031仅此一项可选skip原样保留。PG建库用独立MetaData和三组原预置，不污染后续SQLite。
