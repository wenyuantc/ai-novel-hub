# 调研记录｜首次发布前修改定稿

- 问题：章节定稿后继续编辑时，作品现有机制能否决定库存或保存原定稿？哪些数据能还原一份发布正文？
- 范围：当前工作树的编辑、版本、阅读及导出；没有使用外部资料。
- 日期：2026-11-18

## 已有行为

| 代码位置 | 结论 |
| --- | --- |
| `TODOS.md:230`、`backend/app/schemas/chapter.py:11` | 待办明确要求作者确认定稿。现有章状态仅有 draft、generating 和 done，没有未发定稿改文后的规则。 |
| `backend/app/services/version_service.py:72`、`:86`、`:143`，`backend/app/services/chapter_version_service.py:129` | 普通历史含正文和场景快照；手动保存可在十分钟内覆盖最新手动版本，旧版可被清理或删除。标星仍可撤销。单独引用这类历史无法保证发布记录可长期还原。 |
| `backend/app/services/scene_service.py:295`，`backend/app/services/document_export/story.py:71` | 场景历史包含规划、作者区块及标记，也不包含章标题。面向读者的章节可参考既有导出的单次查询，取章标题、正文与可见排版，排除作者私有材料。章号作为导出标题一部分时也需留存当时值。 |
| `backend/app/services/scene_service.py:133`、`:557`，`backend/app/services/chapter_service.py:340`、`:443` | 内容修订号和预期标题可用来阻止过期操作。场景资料保存、修改章标题也能增加修订号，仅比较修订号不能准确判断读者看到的正文是否真的改动。 |
| `backend/app/services/rich_document_service.py:322`、`batch_revision.py:90`、`generation_service.py:1541`、`rewrite_service.py:592`、`chapter_version_service.py:191`、`agent/effects.py:1383`（均在 services 下） | 正文可经普通编辑、富文档、批量修订、AI、代理和恢复历史写入。只在编辑器按钮上处理定稿会漏掉其他路径。 |
| `backend/app/services/scene_service.py:407`，`frontend/src/components/chapter/VersionDrawer.tsx:143` | 现有手动写入口具备编辑权限、章锁、生成／批量占用检查。前端已有历史与草稿的文本对比，编辑器可明确标注对比中包含本机未保存修改。 |

相关规范见 `.trellis/spec/backend/scenes.md`、`.trellis/spec/backend/rich-documents.md:31`、`.trellis/spec/backend/document-export.md:24`。富文档正文、作者材料、普通编辑历史各有独立语义。

## 产品决策及验收差异

同一章先定稿 A，再保存草稿 B，尚未在任何平台发布：

- **保留定稿 A。** 此章仍贡献一章存稿。登记发布须展示所选定稿，默认仍为 A，直至作者明确用 B 替换；须持久化可独立读取的定稿内容。多个渠道按各次实际选定的版本留档。
- **改文后重新定稿。** 此章暂时退出存稿，无法将 B 作为定稿登记，重新确认后回到存稿。应按选定的读者可见内容变化判断，避免只改作者备注也使定稿失效。既有平台发布记录继续保留其原文。

两个方案都满足同章多平台只扣一次存稿。作者随后采纳第一种做法：保留定稿 A、改写草稿 B 后继续计入存稿，重新定稿时才替换待发版本。最终行为和验收以 [需求文档](../prd.md) 为准。本记录依据静态源码调研。
