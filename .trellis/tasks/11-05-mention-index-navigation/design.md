# 提及技术设计
完整权威协议见 ../11-05-mention-tracking/design.md；实现不得自行发明第二套DTO或匹配口径。
## 责任与落点
M-B拥有backend提及服务/纯匹配模块/schemas/路由、WorldSetting.aliases与0033、兼容/测试；M-F拥有frontend/api/mentions.ts、提及组件/页面、原有编辑器/Reader/详情/Agent/Knowledge导航集成与测试。N拥有笔记文件，先通知再交接共享注册。
建议服务分开 matcher（纯函数）、source adapters（权限/原文）、query/summary（批量与分页）、source-window（版本定位），避免超长单文件。共用EntityRef/SourceRef/MentionSpan/Occurrence，TS与Pydantic镜像。
## 索引
当前文本+稳定词典指纹为事实源，有界LRU缓存只复用匹配结果，不改变原文/权限。按来源顺序稳定分页，不静默截断；CPU出事件循环；热力图只扫chapter。缓存清理/测试隔离显式处理。
## 集成
NotesPanel的具体定位改动在N结束后由M-F接手；路由与导航始终由M-F负责。models/api/project_transfer共享文件由N先注册后交M-B；不要撤销其内容。
API paths与DTO、Unicode/歧义/哈希、深链/草稿保护、备份/剧情元数据合同均以父design为准。
