# 手机远程 AI 开发：暂缓记录

- 状态：用户决定暂时搁置，后续不要自动继续安装或开发远程入口。
- 日期：2026-11-03
- 适用项目：Pacer (`xiao/`)

## 决定

本次试用的 OpenHands Agent Canvas + Tailscale 方案操作负担过高，暂不采用。手机和电脑都要安装、登录并保持 Tailscale 连接，手机还要输入电脑上的 OpenHands API Key；电脑需开机联网。它不是普通浏览器直连，也不适合当前希望简单使用的需求。

Tailscale Serve 在当前 Tailnet 未启用，启用需要账户侧操作。本次没有更改 Tailnet 管理配置，最后尝试的入口是 Tailscale 内网地址 `100.76.90.54:8000`。不使用 Tailscale 时，该地址无法访问。

## 已完成与未完成

- 已下载 OpenHands Agent Canvas `1.34.0`、Node.js `24.22.2`、uv `0.13.7`；本机后端依赖也已下载。
- 本机主页、`/ready`、`/health` 返回 200；API 未认证返回 401，使用本机 API Key 返回 200，密钥没有注入网页。
- 手机实机连接、Codex/Gemini ACP 对话和真实开发任务均未验收，因此不能记录为已可用的手机开发方案。
- 当前 Windows 用户登录自启和桌面快捷方式已停用；原快捷方式以 `.paused` 后缀保存在安装目录。OpenHands 服务已停止。没有卸载软件、删除密钥或改动 Pacer 业务代码。

## 本机遗留文件

- 安装与启动脚本：`%LOCALAPPDATA%\Pacer\OpenHands\`
- 停用的快捷方式：`%LOCALAPPDATA%\Pacer\OpenHands\OpenHands-startup.lnk.paused` 和 `OpenHands-desktop.lnk.paused`
- 手动启动：`powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Pacer\OpenHands\Start-OpenHands.ps1"`
- OpenHands 设置、会话和 API Key：`%USERPROFILE%\.openhands\agent-canvas\`
- OpenHands 日志：`%LOCALAPPDATA%\Pacer\OpenHands\logs\`
- Tailscale Serve：没有配置。

如果以后用户明确要求恢复，先确认是否接受手机和电脑同时运行 Tailscale 及手动输入 API Key；若不接受，先比较现成工具能否省掉这些步骤，不要重做自研 PWA，也不要把任何 `git reset --hard` 自动回滚加到现有工作树。

## Gemini 方案判断

规约、真实执行结果和有限重试等想法可以作为开发流程参考，但文档示例不能直接投入使用：模型返回值是写死的，补丁写入和修复请求都没有实现；置信分数不是可行性证明；只读权限不能隔离同一 Windows 用户下的 Agent；`git reset --hard` 会丢失未提交改动。相邻的 [Pacer 手机远程开发设计草案](pacer_mobile_remote_dev_design.md) 仅为提案，未实施、未采纳。
