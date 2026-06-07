# Visual Agent 测试审核报告

> 日期：2026-06-05  
> 范围：当前工作区整体回归、VS Code 扩展打包、真实 browser-smoke 链路、生成 workflow 回归链路、敏感信息脱敏检查  
> 结论：主回归通过，真实 DOM 浏览器链路可用；VLM 视觉兜底未配置，是当前最大能力缺口。

---

## 1. 总体结论

当前版本已经通过完整 Python 测试、VS Code 扩展编译打包、真实本地页面 browser-smoke、生成 workflow 严格校验，以及生成 workflow 的二次执行验证。

本轮重点确认了以下能力：

- 可以真实打开本地前端页面。
- 可以通过 Playwright 观察 DOM、截图、保存 HTML、保存可见文本。
- 可以填表、点击、等待 URL/text 变化。
- 可以把一次 browser-smoke 保存为可复用 workflow。
- 可以生成对应的 `.inputs.example.json` 输入模板。
- 可以用生成的 workflow 再次执行真实回归。
- browser-smoke 输出、run-workflow 输出、落盘 JSON 报告均未发现测试账号密码明文泄露。

当前不应继续盲目扩功能，建议先提交一个稳定版本，再单独规划 VLM/真实视觉场景增强。

---

## 2. 已执行检查

### 2.1 Python 全量测试

命令：

```powershell
python -m pytest tests/ -q --tb=short
```

结果：

```text
778 passed, 6 skipped
```

结论：通过。

### 2.2 VS Code 扩展打包

命令：

```powershell
npm run package
```

目录：

```text
vscode-extension
```

结果：

```text
Packaged: D:\longxia agent\vscode-extension\visual-agent-0.1.0.vsix
```

结论：通过。

### 2.3 Release 检查计划

命令：

```powershell
python -m visual_agent.cli release-check --format json
```

结果：

- 成功输出 release 检查计划。
- 共 12 项检查。
- 状态为 `planned`。

注意：该命令当前是生成检查计划，不是自动执行全部 release gate。

### 2.4 安装检查计划

命令：

```powershell
python -m visual_agent.cli install-check --format json
```

结果：

- 成功输出安装检查计划。
- 共 6 项检查。
- 状态为 `planned`。

注意：Playwright Chromium 安装被列为可选项，但真实浏览器自动化需要它。

### 2.5 Doctor 环境检查

命令：

```powershell
python -m visual_agent.cli doctor
```

结果摘要：

- `ok: true`
- `available_count: 52`
- `missing_count: 0`
- DOM browser：可用
- Windows UIA：可用
- OCR：可用
- VLM：不可用

结论：基础自动化环境可用；VLM 视觉理解未配置。

---

## 3. 真实 Browser Smoke 验证

验证目标：本地 HTML 登录页。

命令摘要：

```powershell
python -m visual_agent.cli browser-smoke `
  --url "file:///D:/longxia%20agent/examples/web/login_demo.html" `
  --output-dir .runs `
  --fill "用户名=demo_user" `
  --fill-selector "#password=demo_password" `
  --click-text "登录" `
  --require-change-after-click `
  --wait-for-url-contains-after "username=demo_user" `
  --expect-url-contains-after "password=demo_password" `
  --save-workflow ".runs/audit_login_smoke.yaml" `
  --overwrite-workflow `
  --format json
```

结果：

- `status: success`
- 成功打开页面。
- 成功识别输入框和登录按钮。
- 成功填入用户名和密码。
- 成功点击登录。
- 成功等待 URL 变化。
- 成功保存 workflow。
- 成功生成 inputs 示例文件。

生成文件：

```text
.runs/audit_login_smoke.yaml
.runs/audit_login_smoke.inputs.example.json
```

敏感信息检查：

- browser-smoke stdout 中未发现 `demo_user` / `demo_password` 明文。
- `after_click.url` 已脱敏为：

```text
username=[REDACTED]&password=[REDACTED]
```

---

## 4. 生成 Workflow 验证

### 4.1 严格校验

命令：

```powershell
python -m visual_agent.cli validate-workflow --file .runs\audit_login_smoke.yaml --strict
```

结果：

```json
{
  "valid": true,
  "workflow_name": "audit_login_smoke",
  "issues": []
}
```

结论：生成 workflow 通过严格校验。

### 4.2 二次执行

使用本地输入副本：

```text
.runs/audit_login_smoke.inputs.local.json
```

执行命令摘要：

```powershell
python -m visual_agent.cli run-workflow `
  --file .runs\audit_login_smoke.yaml `
  --inputs-file .runs\audit_login_smoke.inputs.local.json `
  --sensitive-fields password `
  --output-dir .runs `
  --run-profile supervised `
  --allow-click
```

结果：

- workflow 成功执行。
- stdout 扫描未发现 `demo_user` / `demo_password`。
- 最新 run 目录 JSON 报告扫描未发现 `demo_user` / `demo_password`。

结论：browser-smoke -> workflow -> run-workflow 回归链路闭环通过。

---

## 5. 本轮确认的已实现能力

### 5.1 Browser smoke

已实现：

- 打开 URL。
- 等待页面加载。
- 截图。
- 保存 HTML。
- 保存可见文本。
- 检查页面非空。
- 检查可交互元素数量。
- 检查可见文本。
- 检查 URL 片段。
- 按语义 label 填表。
- 按 CSS selector 填表。
- 按文本点击。
- 按 selector 点击。
- 点击后等待文本。
- 点击后等待 URL 片段。
- 检查点击后页面是否变化。
- 输出诊断 JSON / Markdown。
- 保存 workflow。
- 生成 inputs 示例文件。
- 对 fill 输入值进行输出脱敏。

### 5.2 Workflow

已实现：

- `observe_browser`
- `assert_browser_ready`
- `paste value_from`
- `click`
- `wait_for condition=text`
- `wait_for condition=url`
- `text_from`
- `url_contains_from`
- 点击后自动 browser observation
- 运行报告落盘
- 敏感输入脱敏

### 5.3 VS Code 扩展

已确认：

- TypeScript 编译通过。
- VSIX 打包成功。
- browser smoke 命令已接入保存 workflow 路径。

---

## 6. 风险与缺口

### 6.1 VLM 未配置

`doctor` 显示 VLM 不可用。

影响：

- 对纯视觉 UI、canvas、自绘控件、复杂截图理解的能力有限。
- 当前更可靠的是 DOM / UIA / OCR 路径。
- 如果目标页面没有可用 DOM 结构，或者控件完全不可语义定位，工具仍可能无法完成真实操作。

建议：

- 后续单独做 VLM 配置和真实视觉 fallback 测试。
- 可选路径：
  - 配置云端 VLM API key。
  - 配置本地 qwen2-vl / moondream 模型路径。
  - 继续增强 OCR + UIA fallback。

### 6.2 release-check / install-check 仍是计划输出

这两个命令目前能生成检查清单，但不是完整执行器。

影响：

- 发布前仍需要人工或 CI 按清单执行。

建议：

- 后续增加 `release-check --run` 或独立 `release-gate` 命令。

### 6.3 工作区未提交改动很多

当前工作区存在大量已修改和新增文件。

影响：

- 不适合直接发布。
- 不利于回滚和审查。

建议：

- 先按功能分组提交。
- 至少拆成：
  - browser smoke / workflow 真实链路
  - 脱敏与安全
  - VS Code extension
  - licensing / roadmap / docs
  - repair / benchmark / workflow generator

### 6.4 真实外部站点尚未覆盖

本轮真实验证使用本地 HTML 页面。

影响：

- 还不能证明对复杂现代前端站点足够稳定。

建议：

- 后续增加至少 3 类真实场景：
  - React/Vite 本地应用
  - 表格/筛选/分页业务应用
  - 登录态恢复和下载/导出流程

---

## 7. 建议下一步

建议先停止继续堆功能，进入整理阶段：

1. 分组提交当前通过测试的版本。
2. 写一份 CHANGELOG 或 DEVELOPMENT_LOG 更新。
3. 增加 release gate 自动执行命令。
4. 单独规划 VLM 配置和真实视觉 fallback。
5. 准备外部真实场景测试集。

当前状态适合作为一个阶段性 checkpoint。

