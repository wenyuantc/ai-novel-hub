# 认证入口与登录注册视觉补充

用户在2026-10-29提供三张截图，并明确要求补齐首页登录和认证页；沿用原任务，不提交代码。

## 参考

- 弹窗：`frames/index.tsx::EntryDialog`；用户图 `/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/codex-clipboard-34bef184-3fe1-44a6-9b15-a756372a3511.png`。
- 登录：`frames/login.tsx` → `components/CoreScreens.tsx::Login`；用户图 `/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/codex-clipboard-ba832b13-e11c-448b-a384-1f05cbaa24d5.png`。
- 注册：同上，图 `/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/codex-clipboard-6c59e30a-e006-4124-bff2-14a9216438f8.png`。
- 设计根目录 `/Users/wenyuantc/.vetta/workspace/AI小说平台/zhiye-novel-full.vetd`。原稿 `assets/mountain-story.jpg` 是306×103的山海人物插画，主会话已目视确认，可复制到项目内作静态装饰；不能导入data.ts或使用其假作者/项目。

## 视觉与行为

- 弹窗宽460px、桌面top112px、32px内距、16px圆角；hero色60%遮罩。品牌/关闭 → 28px欢迎标题 → 14px、28px行高说明 → 浅色信息块 → 48px主CTA → 次级入口。用AntD Modal保证焦点/关闭/滚动锁定。
- 信息块改为通用创作空间说明，不能显示“体验账号”“林间的创作空间”“已准备好示例作品”。次入口可回公开故事方向，不能绕过私有书库鉴权。
- 登录页桌面1.05fr/1fr，左侧panel、px64/py48，右侧paper、px48；表单max380px。左侧品牌、倾斜装帧、38px标语、说明和页脚；右侧30px标题、说明、左对齐Tab、表单与实例账号帮助。
- 原有登录Form、注册Form、autoComplete与共享规则保留；提交中不可切换标签导致模式与请求不一致；错误显示服务端信息。
- `/login?tab=register` 仍由现有注册开关控制；切tab保留 `location.state.from`。认证成功回原目标，缺省 `/projects`。
- 后端Password实际是至少6字符且UTF-8至多72字节；注册提示应准确，不能因参考稿把下限误写成6字节。共享前端规则保持，服务端422继续通过原errorMessage展示。

## 验证边界

当前5174/8001隔离真实后端健康且注册开启。只用隔离测试账号验收，不读写用户原库、不触发AI。桌面两种表单与首页弹窗分别截图，检查真实登录、重复账号错误、字段校验、关闭注册、返回目标、ESC/焦点和320至1440px响应式；样式不写镜像测试。
