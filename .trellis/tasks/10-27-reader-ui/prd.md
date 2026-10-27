# 阅读器与界面体验（P1-10b reader-ui）

## Goal

章节阅读器、书架式项目列表、暗色主题、移动端适配。对应差距清单 D5、A18。用户决定从 project-io-reader 拆出，紧接着做。

## 依赖

- 无硬依赖。project-io-reader 完成后，书架卡片可以接入"导出备份"和"导出 EPUB"入口。

## 已知现状（2026-10-27 核查）

- 前端主题写死在 `App.tsx` 的 `ConfigProvider` 里，只设置了一个主色，没有暗色模式。
- 全站没有响应式判断：`innerWidth` 和 `useBreakpoint` 都出现 0 次。
- 项目列表是普通卡片。
- 没有阅读器，只有编辑器。

## 已知方向（来自 MuMu 调研，规划时细化）

- 暗色主题提供 light、dark、system 三种模式，存在 localStorage，并监听系统偏好的变化。
- 用响应式 hook 统一判断屏幕宽度，并跟随窗口大小变化；不要像 MuMu 那样到处直接读 `innerWidth`。
- 阅读器支持字号、行距、背景主题，以及阅读进度。

## Requirements

- 待规划。

## Acceptance Criteria

- [ ] 待规划。
