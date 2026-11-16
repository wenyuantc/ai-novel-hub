# DOCX 生成与下载方案研究

核对日期：2026-11-16。状态：规划研究，未安装新依赖、未改产品代码。

## 已验证的依赖能力

- 官方包为 `python-docx`，导入名 `docx`；PyPI 当前发布为 1.2.0，要求 Python >=3.9，兼容项目声明的 Python 3.12。
- 官方 API 支持 `Document()`、`add_heading()`、`add_paragraph()`、`add_run()` 的粗体／斜体，以及保存到二进制文件对象。可在内存中生成响应，无需服务器安装 Word。
- 建议实现时新增 `python-docx>=1.2,<2` 并更新 `backend/uv.lock`；具体版本由锁文件固定。无需引入前端 DOCX 库或把 HTML 转为 Word。

官方来源：

- [PyPI](https://pypi.org/project/python-docx/)
- [Quickstart](https://python-docx.readthedocs.io/en/latest/user/quickstart.html)
- [Working with documents](https://python-docx.readthedocs.io/en/latest/user/documents.html)
- Context7 官方仓库索引 `/python-openxml/python-docx`（检索 Document/save、paragraph/run API）。

## 项目接入约束

- `backend/pyproject.toml` 目前没有 DOCX 生成依赖。
- `backend/app/services/export_service.py:23` 的 `ExportFile` 已封装二进制／文本内容、中文文件名和编码后的提示头，可复用附件响应格式。
- `backend/app/services/rich_document_render.py:60` 的 `prose_blocks` 和 `backend/app/schemas/reader.py` 可作为正文格式的统一输入；不要解析 Markdown 来重建正文排版。
- 现有章节正文和格式通过同一 SQL 取得（`rich_document_render.py:155`），需为新场景选择保留同一快照，不可把不同查询版本的正文和坐标拼接。
- 新增重型同步渲染必须放在线程执行，先构造不含 ORM 的只读数据，再生成文件；不能在线程中共享 AsyncSession，也不能长期持有写锁。
- DOCX 使用库的文本 API，避免把作者文本直接插为 OOXML。非法 XML 控制字符需要明确的可读错误，不能静默截断正文或让内部 ValueError 变为未知 500。
- 保留中文、空行、正文顺序、粗斜体、标题、列表和引用的语义。DOCX 的段落与分页结构不承诺与纯文本字节级一致；不加载外部图片和链接资源。

## 下载接口建议

- 保留旧 GET `/projects/{pid}/export`（TXT／MD／EPUB）以保证旧调用和开放 API 契约。
- 内容组合选项采用新的站内 POST JSON 请求，避免多选 ID 过长导致 URL 限制；仅接受 `docx|md|txt`。
- 前端新请求复用 `frontend/src/api/client.ts` 的 `rawRequest`：继承 Cookie、错误包、请求发起时捕获的身份及作品失效处理。
- 在读取 blob 后、创建对象 URL 前检查本次弹窗、actor、作品和授权代次是否仍有效；失效请求不能下载或关闭新弹窗。
- 文件使用浏览器 blob 下载；已触发的用户文件不能通过后续撤权撤回。

## 必要验证

1. 后端真实 HTTP 请求下载 DOCX，使用 ZIP/XML 独立断言所选内容、顺序、标题样式与粗斜体，确认无作者区块或未选资料混入。
2. 用库重新打开样例文件验证包结构；实现后另以实际文档查看／渲染检查中文、分页、长段落和基本格式。结构测试不能替代排版验证。
3. 验证选择为空、纯资料、无摘要、摘要过期、生成中、跨作品 ID 和本人聊天隔离；与 DOCX 配套验证 TXT／Markdown 内容一致。
4. 旧 TXT／Markdown／EPUB 与开放 API 导出回归。
5. 真实浏览器验证桌面／375px、键盘操作、多选翻页和下载；覆盖弹窗关闭、项目／身份／授权变化时的晚到响应。
