# C12：旧单场景 PATCH 纯空白清空的历史保护

## 批准范围与 RED 计划

完整第二轮已结束并获main明确可写后实施。只修改 `chapter_service.py` 一处清空判定、`test_scene_versions.py` 永久HTTP回归与本记录；不改版本原语、普通10分钟合并、审计、迁移或其他测试。

根因：旧PATCH用字符串truthiness判断清空，版本服务则用 `.strip()` 判断空白。无历史非空稿改为纯空白时，前者不保存baseline、后者因latest=None不写结果版本，原稿与清空边界均缺失。正文不能trim；只统一清空判定，再沿用同事务force baseline与record_manual。

先加入永久四参数测试（空字符串／纯空白 × 无历史／有历史），用HTTP验证精确正文、Scene身份、字数、一次revision、原稿与清空结构快照、原版本不合并及恢复原稿；先在未修产品代码上运行RED。

## RED证据与最小修复

未修改产品时执行：`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_scene_versions.py::test_legacy_single_scene_clear_preserves_exact_structural_history -q --tb=short`。结果 **1 failed、3 passed，3.10s**；唯一失败为 `without_history-whitespace`，HTTP写入与revision成功，但版本列表长度为0而非应有2。原日志 `/private/tmp/ainovel-c12-red.log`。

随后仅将旧PATCH清空条件 `content_changed and not data.content` 改为 `content_changed and not data.content.strip()`。`.strip()`只用于布尔判定，原始 `data.content` 仍逐字符传给 `replace_legacy_content`；不改 `version_service`。无历史的非空原稿现在先force保存baseline，再由已有record_manual写独立清空版本；有历史保持原版本不合并，普通正文10分钟规则与同事务审计完全沿用。

## GREEN与关联门禁

- 相同四参数永久HTTP回归，修复后原命令 → **4 passed in 5.80s**。日志 `/private/tmp/ainovel-c12-green.log`。
- `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_chapters.py tests/test_chapter_versions.py tests/test_scene_versions.py tests/test_scenes.py tests/test_admin_audit_logs.py::test_chapter_cast_outlines_versions_success_and_business_failures -q --tb=short` → **44 passed in 31.54s**。日志 `/private/tmp/ainovel-c12-related.log`。包括原普通正文10分钟合并、固定合并窗口、标星／裁剪、章节和场景生成／恢复、结构与清空边界及既有管理员章节版本审计。
- `uv run ruff check app/services/chapter_service.py tests/test_scene_versions.py` → **All checks passed**。
- `git diff --check -- backend/app/services/chapter_service.py backend/tests/test_scene_versions.py` → **通过，无输出**；本新增记录另经no-index空白检查。

永久回归不仅断言版本数量：四种组合均校验原稿中的制表符／CRLF／emoji／尾空格逐字符保全，清空结果也不trim；原稿与清空各有同一稳定Scene的schema_version=1快照，metadata相同；清空字数0，内容revision恰增1；有历史时旧版本ID保持不变，无历史时原稿source为baseline；再通过真实HTTP恢复原稿，字数10、身份不变、revision再增1，同时保留精确清空版本供再次恢复。

未修改version_service、迁移、其他源码／测试、README/spec或任务状态，未提交。没有单独追加PG用例或运行全量：本修复不改DDL／SQL锁／数据库路径，第三轮含原PG规约的完整门禁由main统一执行。C12源码与永久回归已冻结。

## 冻结SHA-256

- `backend/app/services/chapter_service.py`：`f92850f87577a9222f31f13ee4e6e644295bfe937ffa5d60051a4eb2dc58c0bd`
- `backend/tests/test_scene_versions.py`：`1f0e0c153c19dc459b2fc70ec0a3435d6d918b2ec1af064d1ce6f2ccecc99362`
