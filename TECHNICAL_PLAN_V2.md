# Visual Agent 技术开发计划 V2

> 版本：2026-06-05
> 状态：执行文档
> 前置阅读：ROADMAP.md（Phase 1-3 已完成）

---

## 一、方向重定义

### 1.1 原假设的根本错误

原 ROADMAP 的隐含假设是：**人类开发者是 Visual Agent 的直接用户**，需要降低他们创建 workflow 的摩擦。

这个假设是错的。

真实的使用链条是：

```
人类 → 给 Claude Code / Codex 一个需求
Claude Code / Codex → 写代码
Claude Code / Codex → 调用 Visual Agent MCP 验证
人类 → 看结果，决定是否接受
```

**AI 工具是 Visual Agent 的甲方。** 人类只出现在链条的两端。

### 1.2 这个转变解决了质量问题

AI 工具在调用 `generate_workflow` 时，拥有任何录制或模板方式都不具备的信息：

- 它刚写了哪些路由（知道 URL）
- 它刚写了哪些表单字段（知道 label、name、type、validation）
- 提交成功后跳转哪里（知道 redirect）
- 成功状态页面显示什么字符串（知道模板变量）
- 验证失败时的错误消息（知道 error message）

因此，AI 生成的 workflow 可以包含真正的**业务断言**，而不是"结构骨架"。

### 1.3 产品定位重写

| 维度 | 旧定位 | 新定位 |
|------|--------|--------|
| 主要用户 | 人类开发者 | AI 编程助手（Claude Code、Codex、Cursor） |
| Workflow 来源 | 人写 / AI 辅助写 | AI 写代码时同步生成 |
| 质量来源 | 人工审核 | 代码语义推断，断言由代码内容决定 |
| MCP 角色 | 可选扩展 | 唯一核心接口 |
| VS Code 扩展 | 开发者操作面板 | AI 工作结果的人类可视化层 |
| Workflow 价值 | 复用资产 | AI 工作记忆 + 回归防护网 |

---

## 二、架构总览

### 2.1 数据流

```
┌─────────────────────────────────────────────────────┐
│                  AI Coding Assistant                 │
│          (Claude Code / Codex / Cursor)              │
└────────────────────┬────────────────────────────────┘
                     │ MCP calls
                     ▼
┌─────────────────────────────────────────────────────┐
│                 MCP Server (existing)                │
│  ┌─────────────────────────────────────────────┐    │
│  │  NEW: generate_workflow_from_context         │    │
│  │  NEW: verify_implementation                  │    │
│  │  NEW: score_workflow_quality                 │    │
│  │  existing: run_workflow, get_run_report, …   │    │
│  └─────────────────────────────────────────────┘    │
└────────────────────┬────────────────────────────────┘
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
┌──────────────────┐  ┌──────────────────────────────┐
│ Context Ingestion│  │   Existing Runtime            │
│ Layer (NEW)      │  │   - workflow executor         │
│                  │  │   - failure diagnosis         │
│ - diff parser    │  │   - run reports               │
│ - AST extractor  │  │   - session/context snapshot  │
│ - framework      │  └──────────────────────────────┘
│   detector       │
│ - semantic model │
│   builder        │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Workflow         │
│ Synthesis Engine │
│ (NEW)            │
│                  │
│ - step generator │
│ - assertion      │
│   inferrer       │
│ - input schema   │
│   builder        │
│ - quality scorer │
└──────────────────┘
```

### 2.2 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| 上下文摄取层 | `context_ingestion.py` | 解析代码变更，提取 UI 语义 |
| Workflow 合成引擎 | `workflow_synthesis.py` | 从语义模型生成 workflow YAML |
| 质量评分器 | `workflow_quality.py` | 评估 workflow 的断言质量 |
| MCP 新工具 | `mcp_server.py`（扩展） | `generate_workflow_from_context`、`verify_implementation` |
| CLI 新命令 | `cli.py`（扩展） | `generate-from-diff`、`verify-impl` |

### 2.3 与现有代码的关系

- `git_diff.py`：已有 `changed_files()` 和 `affected_workflows()`，上下文摄取层直接复用
- `planner_generate.py`：已有 `build_planner_draft_prompt()`，workflow 合成引擎参考其提示结构
- `workflow_generator.py`：现有的自然语言生成保留，新增代码上下文路径，两者共存
- `mcp_server.py`：在现有工具列表末尾新增，不修改现有工具签名
- `security.py`：`scrub_secrets()` 继续用于所有输出脱敏

---

## 三、核心数据模型

### 3.1 代码变更输入

```python
# context_ingestion.py

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class CodeChange:
    file_path: str                               # 相对于项目根目录
    before: str | None                           # None 表示新建文件
    after: str                                   # 变更后内容
    change_type: Literal["added", "modified", "deleted"]


@dataclass(frozen=True)
class GenerationContext:
    task_description: str                        # AI 被交付的原始任务
    code_changes: tuple[CodeChange, ...]         # 本次变更的全部文件
    base_url: str                                # 应用运行地址，如 http://localhost:3000
    project_root: str                            # 项目根目录绝对路径
    framework_hint: str | None = None            # 可选，自动检测优先
```

### 3.2 UI 语义模型

这是上下文摄取层的输出，也是 workflow 合成引擎的输入。

```python
@dataclass(frozen=True)
class FormField:
    name: str                                    # HTML name 属性或变量名
    label: str | None                            # 用户可见标签
    field_type: Literal[
        "text", "password", "email", "number",
        "select", "checkbox", "file", "hidden"
    ]
    required: bool
    validation_rules: tuple[str, ...]            # 如 ("min_length:8", "email_format")
    is_sensitive: bool                           # 自动判断：password/token/secret/key


@dataclass(frozen=True)
class SubmitAction:
    text: str                                    # 按钮文本，如"登录"、"提交"
    selector: str | None                         # CSS selector（如果有）


@dataclass(frozen=True)
class SuccessState:
    kind: Literal["url_redirect", "text_appears", "element_appears"]
    value: str                                   # redirect 目标 / 出现的文本 / selector
    source: str                                  # 从哪个文件/行提取的，用于调试


@dataclass(frozen=True)
class ErrorState:
    trigger: str                                 # 触发条件描述，如"密码少于8位"
    message: str                                 # 期望显示的错误文本
    source: str


@dataclass(frozen=True)
class DataDisplay:
    description: str                             # 如"登录后显示用户名"
    template_expr: str | None                    # 如 "{{ user.username }}" 或 "{username}"
    example_value: str | None                    # 如果能从代码推断出示例值


@dataclass(frozen=True)
class UISemanticModel:
    entry_url: str                               # 入口 URL
    page_title: str | None                       # 页面 <title> 或 h1
    form_fields: tuple[FormField, ...]
    submit_actions: tuple[SubmitAction, ...]
    success_states: tuple[SuccessState, ...]
    error_states: tuple[ErrorState, ...]
    data_displays: tuple[DataDisplay, ...]
    framework: str                               # "react" | "vue" | "html" | "django" | "flask" | "fastapi" | "unknown"
    confidence: float                            # 0.0–1.0，解析置信度
    parse_warnings: tuple[str, ...]              # 解析过程中的警告
```

### 3.3 Workflow 质量分

```python
@dataclass(frozen=True)
class WorkflowQualityScore:
    total_score: float                           # 0.0–1.0
    assertion_density: float                     # 断言步骤数 / 总步骤数
    business_assertion_count: int                # 有语义内容的断言（文本、URL 值）
    structural_assertion_count: int              # 纯结构检查（元素存在性）
    covers_success_path: bool                    # 是否覆盖成功路径
    covers_error_path: bool                      # 是否覆盖至少一条错误路径
    covers_data_display: bool                    # 是否验证了动态数据展示
    gaps: tuple[str, ...]                        # 缺失覆盖的描述
    recommendation: str                          # 如何改进质量的建议（≤ 100 字）
```

### 3.4 生成结果

```python
@dataclass(frozen=True)
class WorkflowGenerationResult:
    status: Literal["success", "partial", "error"]
    workflow_name: str | None
    workflow_path: str | None
    workflow_yaml: str | None
    quality_score: WorkflowQualityScore | None
    semantic_model: UISemanticModel | None
    message: str
    warnings: tuple[str, ...]
```

---

## 四、Phase 1：上下文摄取层

**目标：从代码变更中提取 `UISemanticModel`，不依赖 LLM，纯静态分析。**

### 4.1 框架检测

```python
# context_ingestion.py

def detect_framework(changes: tuple[CodeChange, ...]) -> str:
    """
    优先级：
    1. 显式 framework_hint
    2. 文件扩展名 + 内容特征
    3. 返回 "unknown"
    """
    all_paths = [c.file_path for c in changes]
    all_content = "\n".join(c.after for c in changes if c.change_type != "deleted")

    if any(p.endswith(".jsx") or p.endswith(".tsx") for p in all_paths):
        return "react"
    if any(p.endswith(".vue") for p in all_paths):
        return "vue"
    if "from django" in all_content or "django.urls" in all_content:
        return "django"
    if "from fastapi" in all_content or "FastAPI()" in all_content:
        return "fastapi"
    if "from flask" in all_content or "Flask(__name__)" in all_content:
        return "flask"
    if any(p.endswith(".html") for p in all_paths):
        return "html"
    return "unknown"
```

### 4.2 HTML 解析器

```python
import re
from html.parser import HTMLParser


class _FormExtractor(HTMLParser):
    """
    从 HTML 字符串提取表单结构。
    不引入第三方依赖，使用标准库 html.parser。
    """

    def __init__(self) -> None:
        super().__init__()
        self.form_action: str | None = None
        self.fields: list[dict] = []
        self.submit_texts: list[str] = []
        self._current_label: str | None = None
        self._inside_label: bool = False
        self._last_input_id: str | None = None

    def handle_starttag(self, tag: str, attrs_list: list) -> None:
        attrs = dict(attrs_list)
        if tag == "form":
            self.form_action = attrs.get("action")
        elif tag == "input":
            field = {
                "name": attrs.get("name") or attrs.get("id", ""),
                "type": attrs.get("type", "text").lower(),
                "id": attrs.get("id"),
                "required": "required" in attrs,
                "placeholder": attrs.get("placeholder"),
            }
            self.fields.append(field)
            self._last_input_id = attrs.get("id")
        elif tag == "button":
            if attrs.get("type", "submit") == "submit":
                pass  # text collected in handle_data
        elif tag == "label":
            self._inside_label = True
            self._current_label = attrs.get("for")

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._inside_label and self._current_label:
            # 把 label 文本绑定到对应 input
            for field in reversed(self.fields):
                if field.get("id") == self._current_label or field.get("name") == self._current_label:
                    field["label"] = text
                    break

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._inside_label = False


def extract_html_semantics(content: str, base_url: str) -> UISemanticModel:
    extractor = _FormExtractor()
    extractor.feed(content)

    fields = []
    for f in extractor.fields:
        if f["type"] in ("hidden", "submit", "button", "reset"):
            continue
        label = f.get("label") or f.get("placeholder") or f["name"]
        is_sensitive = f["type"] == "password" or any(
            kw in f["name"].lower() for kw in ("password", "passwd", "secret", "token", "key")
        )
        fields.append(FormField(
            name=f["name"],
            label=label,
            field_type=f["type"],
            required=f["required"],
            validation_rules=(),
            is_sensitive=is_sensitive,
        ))

    # 提取 form action 作为 success URL hint
    success_states = []
    if extractor.form_action and extractor.form_action not in ("#", ""):
        success_states.append(SuccessState(
            kind="url_redirect",
            value=extractor.form_action,
            source="html:form@action",
        ))

    return UISemanticModel(
        entry_url=base_url,
        page_title=_extract_title(content),
        form_fields=tuple(fields),
        submit_actions=tuple(
            SubmitAction(text=t, selector=None) for t in extractor.submit_texts
        ) or (SubmitAction(text="Submit", selector="[type=submit]"),),
        success_states=tuple(success_states),
        error_states=(),
        data_displays=(),
        framework="html",
        confidence=0.8 if fields else 0.3,
        parse_warnings=(),
    )


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else None
```

### 4.3 React/JSX 解析器

```python
def extract_react_semantics(content: str, base_url: str) -> UISemanticModel:
    """
    使用正则提取，不引入 AST 解析依赖。
    覆盖以下模式：
    - <input type="..." name="..." /> 或 <Input ... />
    - <label ...>文字</label>
    - useNavigate / router.push / window.location 目标
    - 条件渲染的成功/错误文本：{success && <p>xxx</p>}
    - useState 的初始值和 setter 模式
    """
    fields = _extract_jsx_inputs(content)
    success_states = _extract_react_redirects(content)
    success_states += _extract_react_success_text(content)
    error_states = _extract_react_error_text(content)
    data_displays = _extract_react_template_vars(content)

    confidence = 0.7 if fields else 0.4
    return UISemanticModel(
        entry_url=base_url,
        page_title=_extract_react_title(content),
        form_fields=tuple(fields),
        submit_actions=_extract_jsx_submit_buttons(content),
        success_states=tuple(success_states),
        error_states=tuple(error_states),
        data_displays=tuple(data_displays),
        framework="react",
        confidence=confidence,
        parse_warnings=(),
    )


def _extract_jsx_inputs(content: str) -> list[FormField]:
    """
    匹配模式：
      <input type="password" name="password" required />
      <Input label="Email" name="email" type="email" />
    """
    results = []
    pattern = re.compile(
        r'<[Ii]nput\b([^/\n>]*?)(?:/>|>)',
        re.DOTALL,
    )
    for m in pattern.finditer(content):
        attrs_str = m.group(1)
        name = _attr(attrs_str, "name") or _attr(attrs_str, "id") or ""
        label = _attr(attrs_str, "label") or _attr(attrs_str, "placeholder") or name
        ftype = (_attr(attrs_str, "type") or "text").lower()
        if ftype in ("hidden", "submit", "button", "reset", "checkbox") or not name:
            continue
        is_sensitive = ftype == "password" or any(
            kw in name.lower() for kw in ("password", "passwd", "secret", "token", "key")
        )
        required = "required" in attrs_str
        results.append(FormField(
            name=name, label=label, field_type=ftype,
            required=required, validation_rules=(), is_sensitive=is_sensitive,
        ))
    return results


def _attr(attrs_str: str, attr: str) -> str | None:
    """从 JSX 属性字符串中提取特定属性值，支持单引号/双引号/无引号。"""
    m = re.search(
        rf'\b{re.escape(attr)}\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|{{`([^`]*)`}})',
        attrs_str,
    )
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    return None


def _extract_react_redirects(content: str) -> list[SuccessState]:
    """
    匹配 navigate("/dashboard"), router.push("/success"), window.location.href = "/done"
    """
    results = []
    patterns = [
        r'navigate\(["\']([^"\']+)["\']\)',
        r'router\.push\(["\']([^"\']+)["\']\)',
        r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
        r'history\.push\(["\']([^"\']+)["\']\)',
        r'<Navigate\s+to=["\']([^"\']+)["\']',
        r'<Redirect\s+to=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, content):
            url = m.group(1)
            if url and not url.startswith("http") and len(url) > 1:
                results.append(SuccessState(
                    kind="url_redirect", value=url, source=f"react:{pat[:20]}"
                ))
    return results


def _extract_react_success_text(content: str) -> list[SuccessState]:
    """
    匹配成功提示文本模式：
    {success && <p>Login successful</p>}
    {status === 'success' && <div>Welcome back</div>}
    """
    results = []
    pattern = re.compile(
        r'\{[^}]*(?:success|done|completed|ok)\s*&&\s*<\w+[^>]*>([^<]{4,80})</\w+>',
        re.IGNORECASE,
    )
    for m in pattern.finditer(content):
        text = m.group(1).strip()
        if text:
            results.append(SuccessState(
                kind="text_appears", value=text, source="react:conditional_render"
            ))
    return results


def _extract_react_error_text(content: str) -> list[ErrorState]:
    """
    匹配错误提示文本：
    {error && <p>{error}</p>}
    {errors.email && <span>Invalid email</span>}
    """
    results = []
    pattern = re.compile(
        r'\{[^}]*error[^}]*&&\s*<\w+[^>]*>([^<{]{4,80})</\w+>',
        re.IGNORECASE,
    )
    for m in pattern.finditer(content):
        text = m.group(1).strip()
        if text and "{" not in text:
            results.append(ErrorState(
                trigger="form validation error",
                message=text,
                source="react:error_conditional",
            ))
    return results


def _extract_react_template_vars(content: str) -> list[DataDisplay]:
    """
    匹配动态数据展示：
    <p>Welcome, {user.name}</p>
    <span>{order.total}</span>
    """
    results = []
    pattern = re.compile(r'<(?:p|h[1-6]|span|div)[^>]*>\s*[^<{]*\{([^}]{2,40})\}[^<]*</\w+>')
    for m in pattern.finditer(content):
        expr = m.group(1).strip()
        if "." in expr or len(expr) < 30:
            results.append(DataDisplay(
                description=f"Displays {expr}",
                template_expr="{" + expr + "}",
                example_value=None,
            ))
    return results


def _extract_jsx_submit_buttons(content: str) -> tuple[SubmitAction, ...]:
    results = []
    # <button type="submit">登录</button>
    pattern = re.compile(r'<[Bb]utton\b[^>]*type=["\']submit["\'][^>]*>([^<]{1,40})</[Bb]utton>')
    for m in pattern.finditer(content):
        text = re.sub(r'\{[^}]*\}', '', m.group(1)).strip()
        if text:
            results.append(SubmitAction(text=text, selector=None))
    # <Button ... onClick={handleSubmit}>提交</Button>
    pattern2 = re.compile(r'<Button\b[^>]*onClick=[^>]*>([^<{]{1,40})</Button>')
    for m in pattern2.finditer(content):
        text = m.group(1).strip()
        if text:
            results.append(SubmitAction(text=text, selector=None))
    return tuple(results) or (SubmitAction(text="Submit", selector="[type=submit]"),)


def _extract_react_title(content: str) -> str | None:
    # <title>Page Title</title> 或 document.title = "..."
    m = re.search(r'<title>([^<]+)</title>', content)
    if m:
        return m.group(1).strip()
    m = re.search(r'document\.title\s*=\s*["\']([^"\']+)["\']', content)
    if m:
        return m.group(1).strip()
    return None
```

### 4.4 后端路由解析器（Django / FastAPI / Flask）

```python
def extract_backend_semantics(content: str, base_url: str, framework: str) -> UISemanticModel:
    """
    从后端路由文件中提取：
    - URL 路径（作为 entry_url）
    - Redirect 目标（作为 SuccessState）
    - Flash message / response message（作为 SuccessState text）
    - 模板变量名（作为 DataDisplay）
    """
    entry_url = _extract_backend_route(content, base_url, framework)
    success_states = _extract_backend_redirects(content, framework)
    success_states += _extract_backend_messages(content, framework)
    data_displays = _extract_template_context_vars(content, framework)

    return UISemanticModel(
        entry_url=entry_url or base_url,
        page_title=None,
        form_fields=(),          # 后端文件不包含表单字段，由前端解析器补充
        submit_actions=(),
        success_states=tuple(success_states),
        error_states=(),
        data_displays=tuple(data_displays),
        framework=framework,
        confidence=0.6 if success_states else 0.3,
        parse_warnings=(),
    )


def _extract_backend_redirects(content: str, framework: str) -> list[SuccessState]:
    results = []
    patterns = {
        "django": [
            r'return\s+redirect\(["\']([^"\']+)["\']\)',
            r'HttpResponseRedirect\(["\']([^"\']+)["\']\)',
            r'reverse\(["\']([^"\']+)["\']\)',
        ],
        "fastapi": [
            r'RedirectResponse\(url=["\']([^"\']+)["\']\)',
            r'return\s+RedirectResponse\(["\']([^"\']+)["\']\)',
        ],
        "flask": [
            r'redirect\(url_for\(["\']([^"\']+)["\']\)\)',
            r'redirect\(["\']([^"\']+)["\']\)',
        ],
    }
    for pat in patterns.get(framework, []):
        for m in re.finditer(pat, content):
            url = m.group(1)
            results.append(SuccessState(
                kind="url_redirect", value=url, source=f"{framework}:redirect"
            ))
    return results


def _extract_backend_messages(content: str, framework: str) -> list[SuccessState]:
    """提取 flash message 或 response 中的成功提示文本。"""
    results = []
    patterns = [
        r'messages\.success\([^,]+,\s*["\']([^"\']{4,100})["\']\)',  # Django
        r'flash\(["\']([^"\']{4,100})["\']\s*(?:,\s*["\']success["\'])?\)',  # Flask
        r'"message"\s*:\s*["\']([^"\']{4,100})["\']',              # JSON response
        r"'message'\s*:\s*['\"]([^'\"]{4,100})['\"]",
    ]
    for pat in patterns:
        for m in re.finditer(pat, content):
            text = m.group(1)
            results.append(SuccessState(
                kind="text_appears", value=text, source=f"{framework}:message"
            ))
    return results
```

### 4.5 摄取层入口

```python
def ingest_context(ctx: GenerationContext) -> UISemanticModel:
    """
    对所有变更文件运行对应解析器，合并结果。
    优先级：前端解析器 > 后端解析器（前端知道 UI 细节）
    """
    framework = ctx.framework_hint or detect_framework(ctx.code_changes)

    frontend_models: list[UISemanticModel] = []
    backend_models: list[UISemanticModel] = []

    for change in ctx.code_changes:
        if change.change_type == "deleted":
            continue
        content = change.after
        path = change.file_path.lower()

        if path.endswith((".html", ".htm")):
            frontend_models.append(extract_html_semantics(content, ctx.base_url))
        elif path.endswith((".jsx", ".tsx")) or (path.endswith((".js", ".ts")) and "component" in path):
            frontend_models.append(extract_react_semantics(content, ctx.base_url))
        elif path.endswith(".vue"):
            frontend_models.append(extract_vue_semantics(content, ctx.base_url))
        elif framework in ("django", "fastapi", "flask") and path.endswith(".py"):
            backend_models.append(extract_backend_semantics(content, ctx.base_url, framework))

    if frontend_models:
        return _merge_models(frontend_models + backend_models, framework)
    if backend_models:
        return _merge_models(backend_models, framework)

    # 没有可解析文件时，返回最低置信度模型，触发 LLM 兜底
    return UISemanticModel(
        entry_url=ctx.base_url,
        page_title=None,
        form_fields=(),
        submit_actions=(SubmitAction(text="Submit", selector="[type=submit]"),),
        success_states=(),
        error_states=(),
        data_displays=(),
        framework=framework,
        confidence=0.1,
        parse_warnings=("no parseable frontend or backend files found",),
    )


def _merge_models(models: list[UISemanticModel], framework: str) -> UISemanticModel:
    """多个文件的解析结果合并。前端字段优先，后端补充 success/error states。"""
    all_fields: list[FormField] = []
    seen_names: set[str] = set()
    all_success: list[SuccessState] = []
    all_error: list[ErrorState] = []
    all_displays: list[DataDisplay] = []
    all_submits: list[SubmitAction] = []
    all_warnings: list[str] = []
    entry_url = models[0].entry_url
    page_title = None

    for m in models:
        for f in m.form_fields:
            if f.name not in seen_names:
                all_fields.append(f)
                seen_names.add(f.name)
        all_success.extend(m.success_states)
        all_error.extend(m.error_states)
        all_displays.extend(m.data_displays)
        all_submits.extend(m.submit_actions)
        all_warnings.extend(m.parse_warnings)
        if page_title is None:
            page_title = m.page_title

    # 去重 success_states（相同 value 只保留一条）
    seen_vals: set[str] = set()
    deduped_success = []
    for s in all_success:
        if s.value not in seen_vals:
            deduped_success.append(s)
            seen_vals.add(s.value)

    avg_confidence = sum(m.confidence for m in models) / len(models)

    return UISemanticModel(
        entry_url=entry_url,
        page_title=page_title,
        form_fields=tuple(all_fields),
        submit_actions=tuple(dict.fromkeys(all_submits)),  # 去重保序
        success_states=tuple(deduped_success),
        error_states=tuple(all_error),
        data_displays=tuple(all_displays),
        framework=framework,
        confidence=avg_confidence,
        parse_warnings=tuple(all_warnings),
    )
```

---

## 五、Phase 2：Workflow 合成引擎

**目标：从 `UISemanticModel` 生成高质量 workflow YAML。**

### 5.1 静态合成（置信度 ≥ 0.5，不调用 LLM）

```python
# workflow_synthesis.py

from __future__ import annotations
import yaml
from .context_ingestion import UISemanticModel, FormField, SuccessState, ErrorState


def synthesize_workflow(
    model: UISemanticModel,
    workflow_name: str,
    description: str,
) -> str:
    """
    从语义模型直接合成 workflow YAML。
    不调用 LLM，100% 确定性输出，便于测试。
    """
    steps = []

    # Step 1: 导航
    steps.append({
        "id": "navigate",
        "action": "observe_browser",
        "url": model.entry_url,
        "timeout_ms": 10000,
        "wait_until": "domcontentloaded",
    })

    # Step 2: 断言页面就绪
    steps.append({
        "id": "assert_ready",
        "action": "assert_browser_ready",
        "min_text_length": 10,
        "min_interactive": len(model.form_fields),
    })

    # Step 3: 填充表单字段
    for field in model.form_fields:
        step_id = f"fill_{field.name}"
        step: dict = {
            "id": step_id,
            "action": "paste",
            "value_from": f"input.{field.name}",
        }
        if field.label:
            step["target"] = {"label": field.label, "role": "input"}
        else:
            step["target"] = {"selector": f'[name="{field.name}"]'}
        if field.is_sensitive:
            step["sensitive"] = True
        steps.append(step)

    # Step 4: 点击提交
    if model.submit_actions:
        submit = model.submit_actions[0]
        click_step: dict = {
            "id": "submit",
            "action": "click",
            "wait_after_seconds": 1.0,
            "browser_post_action_observe": True,
        }
        if submit.selector:
            click_step["target"] = {"selector": submit.selector}
        else:
            click_step["target"] = {"text": submit.text, "role": "button"}
        steps.append(click_step)

    # Step 5: 成功状态断言（这是质量关键）
    for i, state in enumerate(model.success_states):
        if state.kind == "url_redirect":
            steps.append({
                "id": f"assert_url_{i}",
                "action": "wait_for",
                "condition": "url",
                "url_contains": state.value,
                "timeout_seconds": 5.0,
            })
        elif state.kind == "text_appears":
            steps.append({
                "id": f"assert_text_{i}",
                "action": "wait_for",
                "condition": "text",
                "text": state.value,
                "timeout_seconds": 5.0,
            })

    # Step 6: 截图存档
    steps.append({
        "id": "screenshot_final",
        "action": "observe_browser",
        "reuse_page": True,
        "screenshot_label": "final-state",
    })

    doc = {
        "schema_version": 1,
        "min_runtime_version": "0.1.0",
        "name": workflow_name,
        "version": 1,
        "description": description,
        "tags": ["verification", "fast"],
        "visibility": "private",
        "author": "",
        "steps": steps,
    }

    # 生成 inputs schema
    inputs_schema = _build_inputs_schema(model)
    if inputs_schema:
        doc["inputs"] = inputs_schema

    return yaml.dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _build_inputs_schema(model: UISemanticModel) -> dict:
    """为每个表单字段生成 inputs schema，带类型和敏感标记。"""
    schema: dict = {"type": "object", "properties": {}, "required": []}
    for field in model.form_fields:
        prop: dict = {"type": "string"}
        if field.is_sensitive:
            prop["x-sensitive"] = True
        if field.label:
            prop["description"] = field.label
        schema["properties"][field.name] = prop
        if field.required:
            schema["required"].append(field.name)
    return schema if schema["properties"] else {}
```

### 5.2 LLM 兜底合成（置信度 < 0.5）

当静态解析置信度不足时（页面使用纯 Canvas、自绘控件、混淆代码等），调用 LLM：

```python
def synthesize_workflow_with_llm(
    model: UISemanticModel,
    ctx: GenerationContext,
    workflow_name: str,
    description: str,
    model_id: str = "claude-haiku-4-5-20251001",
) -> str:
    """
    LLM 兜底路径。
    将代码变更内容直接喂给 LLM，要求其生成 workflow。
    只在静态解析无法提取足够语义时使用。
    """
    try:
        import anthropic
    except ImportError:
        # 无 SDK 时退化到低质量静态模板
        return synthesize_workflow(model, workflow_name, description)

    # 构建 prompt：传入代码内容 + 任务描述 + 静态解析的部分结果
    code_summary = "\n\n".join(
        f"=== {c.file_path} ===\n{c.after[:2000]}"   # 截断，控制 token
        for c in ctx.code_changes
        if c.change_type != "deleted"
    )
    partial_model_desc = (
        f"Entry URL: {model.entry_url}\n"
        f"Detected fields: {[f.name for f in model.form_fields]}\n"
        f"Detected success states: {[s.value for s in model.success_states]}\n"
    )

    prompt = (
        f"Task: {ctx.task_description}\n\n"
        f"Partial static analysis result:\n{partial_model_desc}\n\n"
        f"Changed code:\n{code_summary}\n\n"
        f"Generate a workflow YAML named '{workflow_name}' that verifies the task is completed correctly.\n"
        f"Include meaningful assert_text and wait_for steps based on what the code actually does.\n"
        f"Return only YAML."
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model_id,
        max_tokens=1500,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    return _strip_fences(raw)


_SYSTEM_PROMPT = """\
You generate Visual Agent workflow YAML for UI verification.
Return only valid YAML. No markdown fences. No explanation.
Schema: schema_version:1, name, description, tags:[verification], steps.
Every workflow must end with at least one assert_text or wait_for(condition:url) step.
Use value_from:input.fieldname for all user input values (never hardcode credentials).
Mark sensitive fields with sensitive:true.
"""
```

### 5.3 合成策略路由

```python
def generate_workflow_from_context(
    ctx: GenerationContext,
    output_path: Path | None = None,
    dry_run: bool = False,
    model_id: str = "claude-haiku-4-5-20251001",
) -> WorkflowGenerationResult:
    """
    主入口。根据语义模型置信度自动选择静态或 LLM 合成路径。
    """
    model = ingest_context(ctx)

    # 生成 workflow name：从任务描述提取 snake_case
    name = _task_to_workflow_name(ctx.task_description)
    description = f"Auto-generated from: {ctx.task_description[:80]}"

    if model.confidence >= 0.5:
        yaml_text = synthesize_workflow(model, name, description)
        generation_method = "static"
    else:
        yaml_text = synthesize_workflow_with_llm(model, ctx, name, description, model_id)
        generation_method = "llm"

    # 质量评分
    quality = score_workflow_quality(yaml_text, model)

    if dry_run:
        return WorkflowGenerationResult(
            status="success",
            workflow_name=name,
            workflow_path=None,
            workflow_yaml=yaml_text,
            quality_score=quality,
            semantic_model=model,
            message=f"Generated via {generation_method} (quality: {quality.total_score:.2f})",
            warnings=model.parse_warnings,
        )

    # 写入文件
    saved_path = _save_workflow(yaml_text, name, output_path)
    _save_inputs_example(model, saved_path)

    return WorkflowGenerationResult(
        status="success",
        workflow_name=name,
        workflow_path=str(saved_path),
        workflow_yaml=yaml_text,
        quality_score=quality,
        semantic_model=model,
        message=f"Saved to {saved_path} (quality: {quality.total_score:.2f})",
        warnings=model.parse_warnings,
    )
```

---

## 六、Phase 3：Workflow 质量评分器

**目标：对任何 workflow（生成的或手写的）给出可量化的质量分，让 AI 助手知道是否需要改进。**

### 6.1 评分算法

```python
# workflow_quality.py

import yaml
from .context_ingestion import UISemanticModel


ASSERTION_ACTIONS = frozenset({
    "wait_for",
    "assert_browser_ready",
    "assert_text",
    "assert_no_error",
    "assert_url_contains",
})

STRUCTURAL_ACTIONS = frozenset({
    "assert_browser_ready",   # 仅检查元素数量，无语义
})


def score_workflow_quality(
    workflow_yaml: str,
    model: UISemanticModel | None = None,
) -> WorkflowQualityScore:
    try:
        doc = yaml.safe_load(workflow_yaml)
        steps = doc.get("steps", [])
    except Exception:
        return WorkflowQualityScore(
            total_score=0.0,
            assertion_density=0.0,
            business_assertion_count=0,
            structural_assertion_count=0,
            covers_success_path=False,
            covers_error_path=False,
            covers_data_display=False,
            gaps=("failed to parse workflow YAML",),
            recommendation="Fix YAML syntax errors before scoring.",
        )

    total_steps = len(steps)
    if total_steps == 0:
        return _zero_score("workflow has no steps")

    assertion_steps = [s for s in steps if s.get("action") in ASSERTION_ACTIONS]
    business_assertions = [
        s for s in assertion_steps
        if s.get("action") not in STRUCTURAL_ACTIONS
        and (s.get("text") or s.get("url_contains") or s.get("url_contains_from"))
    ]
    structural_assertions = [s for s in assertion_steps if s.get("action") in STRUCTURAL_ACTIONS]

    assertion_density = len(assertion_steps) / total_steps

    covers_success = any(
        s.get("action") in ("wait_for", "assert_url_contains", "assert_text")
        for s in steps
    )
    covers_error = any("error" in str(s).lower() for s in steps)
    covers_data = any(
        s.get("action") == "wait_for" and s.get("condition") == "text"
        and s.get("text") and not s.get("text", "").startswith("{")
        for s in steps
    )

    # 如果有语义模型，检查 success_states 的覆盖率
    model_coverage = 1.0
    if model and model.success_states:
        covered = 0
        for state in model.success_states:
            for step in steps:
                if state.value and state.value in str(step):
                    covered += 1
                    break
        model_coverage = covered / len(model.success_states)

    # 加权总分
    score = (
        min(assertion_density / 0.3, 1.0) * 0.30          # 断言密度，目标 30%
        + (len(business_assertions) / max(total_steps * 0.2, 1)) * 0.30  # 业务断言比例
        + (1.0 if covers_success else 0.0) * 0.20          # 覆盖成功路径
        + (1.0 if covers_error else 0.0) * 0.10            # 覆盖错误路径
        + model_coverage * 0.10                             # 与语义模型的吻合度
    )
    score = min(score, 1.0)

    gaps = []
    if not covers_success:
        gaps.append("no success state assertion (add wait_for or assert_text after submit)")
    if not covers_error:
        gaps.append("no error path covered (add at least one error scenario)")
    if assertion_density < 0.2:
        gaps.append(f"low assertion density ({assertion_density:.0%}), add more assertions")
    if len(business_assertions) == 0:
        gaps.append("no business assertions (all checks are structural only)")
    if model and model_coverage < 0.5:
        gaps.append(f"only {model_coverage:.0%} of expected success states are verified")

    recommendation = _build_recommendation(gaps)

    return WorkflowQualityScore(
        total_score=round(score, 3),
        assertion_density=round(assertion_density, 3),
        business_assertion_count=len(business_assertions),
        structural_assertion_count=len(structural_assertions),
        covers_success_path=covers_success,
        covers_error_path=covers_error,
        covers_data_display=covers_data,
        gaps=tuple(gaps),
        recommendation=recommendation,
    )


def _build_recommendation(gaps: list[str]) -> str:
    if not gaps:
        return "Workflow quality is good."
    first = gaps[0]
    if "success state" in first:
        return "Add a wait_for(condition:text) or wait_for(condition:url) step after the submit action."
    if "error path" in first:
        return "Add a separate scenario or step that fills invalid input and asserts the error message appears."
    if "assertion density" in first:
        return "Add assert_text steps to verify page content after each major action."
    return f"Address: {first}"


def _zero_score(reason: str) -> WorkflowQualityScore:
    return WorkflowQualityScore(
        total_score=0.0, assertion_density=0.0,
        business_assertion_count=0, structural_assertion_count=0,
        covers_success_path=False, covers_error_path=False, covers_data_display=False,
        gaps=(reason,), recommendation=reason,
    )
```

---

## 七、Phase 4：MCP 新工具

在 `mcp_server.py` 的 `mcp_tools()` 列表末尾追加，**不修改现有工具**。

### 7.1 `generate_workflow_from_context`

```python
Tool(
    name="generate_workflow_from_context",
    description=(
        "Generate a high-quality verification workflow from code changes. "
        "Call this after writing or modifying UI code. "
        "Provide the code diff and task description; the tool extracts form fields, "
        "success states, and error messages from the code to build semantically meaningful assertions. "
        "Returns the workflow path and a quality score (0.0–1.0). "
        "A score below 0.6 means the workflow lacks meaningful assertions — "
        "inspect the gaps field and add missing steps."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "The original task you were asked to implement.",
            },
            "code_changes": {
                "type": "array",
                "description": "Files changed in this task.",
                "items": {
                    "type": "object",
                    "properties": {
                        "file_path":   {"type": "string"},
                        "before":      {"type": ["string", "null"]},
                        "after":       {"type": "string"},
                        "change_type": {"type": "string", "enum": ["added", "modified", "deleted"]},
                    },
                    "required": ["file_path", "after", "change_type"],
                },
            },
            "base_url": {
                "type": "string",
                "description": "Base URL where the app is running, e.g. http://localhost:3000",
            },
            "workspace_root": {"type": "string"},
            "framework_hint": {
                "type": "string",
                "description": "Optional: react | vue | html | django | fastapi | flask",
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "If true, return YAML without saving to disk.",
            },
        },
        "required": ["task_description", "code_changes", "base_url", "workspace_root"],
    },
),
```

**响应格式（token budget ≤ 500）：**

```python
def _generate_workflow_from_context_payload(args: dict) -> dict:
    from .context_ingestion import GenerationContext, CodeChange
    from .workflow_synthesis import generate_workflow_from_context
    from .security import scrub_secrets

    ctx = GenerationContext(
        task_description=str(args["task_description"]),
        code_changes=tuple(
            CodeChange(
                file_path=c["file_path"],
                before=c.get("before"),
                after=c["after"],
                change_type=c["change_type"],
            )
            for c in args.get("code_changes", [])
        ),
        base_url=str(args["base_url"]),
        project_root=str(args.get("workspace_root", ".")),
        framework_hint=args.get("framework_hint"),
    )

    result = generate_workflow_from_context(
        ctx=ctx,
        output_path=None,
        dry_run=bool(args.get("dry_run", False)),
    )

    # token budget: 返回摘要，不返回完整 YAML（YAML 可通过 validate_workflow 获取）
    q = result.quality_score
    return scrub_secrets({
        "status": result.status,
        "workflow_name": result.workflow_name,
        "workflow_path": result.workflow_path,
        "quality": {
            "score": q.total_score if q else None,
            "covers_success_path": q.covers_success_path if q else False,
            "covers_error_path": q.covers_error_path if q else False,
            "business_assertions": q.business_assertion_count if q else 0,
            "gaps": list(q.gaps[:3]) if q else [],          # 最多 3 条
            "recommendation": q.recommendation if q else "",
        },
        "framework_detected": result.semantic_model.framework if result.semantic_model else "unknown",
        "confidence": result.semantic_model.confidence if result.semantic_model else 0.0,
        "warnings": list(result.warnings[:3]),
        "message": result.message,
        # 仅在 dry_run=true 时返回 YAML
        "yaml": result.workflow_yaml if args.get("dry_run") else None,
    })
```

### 7.2 `verify_implementation`

**一个工具完成"生成→运行→诊断"闭环**，专为 AI 助手的编码-验证循环设计：

```python
Tool(
    name="verify_implementation",
    description=(
        "Generate a workflow from code changes, run it, and return a structured result. "
        "This is the single-call verification loop for AI coding assistants: "
        "write code → call verify_implementation → get pass/fail with diagnosis. "
        "On failure, the response includes the failing step, actual vs expected, "
        "and a fix_hint so you can correct the code and retry. "
        "Token budget: ≤ 800 tokens."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "task_description": {"type": "string"},
            "code_changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file_path":   {"type": "string"},
                        "before":      {"type": ["string", "null"]},
                        "after":       {"type": "string"},
                        "change_type": {"type": "string"},
                    },
                    "required": ["file_path", "after", "change_type"],
                },
            },
            "base_url":       {"type": "string"},
            "workspace_root": {"type": "string"},
            "inputs":         {
                "type": "object",
                "description": "Runtime input values for the workflow, e.g. {username: 'testuser', password: 'testpass'}",
            },
            "run_profile": {
                "type": "string",
                "enum": ["dry-run", "supervised", "approved"],
                "default": "supervised",
            },
        },
        "required": ["task_description", "code_changes", "base_url", "workspace_root"],
    },
),
```

**响应结构：**

```python
# pass 时（≤ 400 tokens）
{
  "result": "pass",
  "workflow_name": "login_form_verification",
  "quality_score": 0.82,
  "steps_passed": 7,
  "duration_ms": 1240,
  "message": "All steps passed. Implementation verified."
}

# fail 时（≤ 800 tokens）
{
  "result": "fail",
  "workflow_name": "login_form_verification",
  "quality_score": 0.82,
  "failed_step": {
    "id": "assert_text_0",
    "action": "wait_for",
    "expected": "欢迎回来，张三",
    "actual": "页面未找到该文本（当前文本前200字：登录失败，请检查账号密码）",
    "fix_hint": "The success message '欢迎回来，张三' was not found. Check that the backend returns this string on successful login and that the frontend renders it."
  },
  "screenshot_path": ".runs/20260605-191028-xxx/after-click.png",
  "steps_passed": 4,
  "steps_total": 7
}
```

---

## 八、Phase 5：VS Code 扩展重定位

扩展的定位从"开发者操作面板"改为"AI 工作结果的人类可视化层"。

### 8.1 核心变更

| 功能 | 旧设计 | 新设计 |
|------|--------|--------|
| 侧边栏 | 显示 workflow 列表，等待用户点击运行 | 实时显示 AI 最近一次 `verify_implementation` 结果 |
| 状态栏 | 通过/失败计数 | 最后一次验证的 pass/fail + 质量分 |
| 命令 | `runAll`、`runAffected` | `showLastVerification`、`openFailureDetail` |
| 触发方式 | 用户主动 | AI 调用 MCP 后自动推送，用户被动接收 |
| `generateWorkflow` | 弹出 InputBox | 移除（AI 直接调用 MCP，不需要 UI） |

### 8.2 实时推送机制

AI 每次调用 `verify_implementation` 后，MCP server 写入一个状态文件：

```python
# mcp_server.py 中 verify_implementation 完成后

def _write_vscode_status(workspace_root: Path, result: dict) -> None:
    status_path = workspace_root / ".vscode-agent-status.json"
    status_path.write_text(
        json.dumps({
            "updated_at": time.time(),
            "result": result.get("result"),
            "workflow_name": result.get("workflow_name"),
            "quality_score": result.get("quality_score"),
            "failed_step": result.get("failed_step"),
            "message": result.get("message"),
        }),
        encoding="utf-8",
    )
```

VS Code 扩展用 `fs.watch` 监听该文件，有变化时自动刷新 UI：

```typescript
// bridge.ts
export function watchVerificationStatus(
    workspaceRoot: string,
    onChange: (status: VerificationStatus) => void
): vscode.Disposable {
    const statusFile = path.join(workspaceRoot, ".vscode-agent-status.json");
    const watcher = fs.watch(statusFile, () => {
        try {
            const raw = fs.readFileSync(statusFile, "utf-8");
            onChange(JSON.parse(raw) as VerificationStatus);
        } catch { /* file not ready */ }
    });
    return { dispose: () => watcher.close() };
}
```

---

## 九、测试策略

### 9.1 上下文摄取层测试

每个解析函数单独测试，fixture 使用真实代码片段：

```python
# tests/test_context_ingestion.py

def test_html_extract_login_form():
    html = """
    <form action="/auth/login" method="post">
        <label for="username">用户名</label>
        <input type="text" id="username" name="username" required />
        <label for="password">密码</label>
        <input type="password" id="password" name="password" required />
        <button type="submit">登录</button>
    </form>
    """
    model = extract_html_semantics(html, "http://localhost:3000/login")
    assert len(model.form_fields) == 2
    assert model.form_fields[0].name == "username"
    assert model.form_fields[0].label == "用户名"
    assert model.form_fields[1].is_sensitive is True
    assert any(s.value == "/auth/login" for s in model.success_states)
    assert model.confidence >= 0.7


def test_react_extract_redirect():
    jsx = """
    const handleSubmit = async (e) => {
        await login(formData);
        navigate("/dashboard");
    };
    return (
        <form onSubmit={handleSubmit}>
            <input type="email" name="email" placeholder="Email" required />
            <input type="password" name="password" />
            <button type="submit">Sign In</button>
        </form>
    );
    """
    model = extract_react_semantics(jsx, "http://localhost:3000")
    assert any(s.value == "/dashboard" for s in model.success_states)
    assert len(model.form_fields) == 2
    assert model.form_fields[1].is_sensitive is True


def test_merge_frontend_backend():
    """前端知道字段，后端知道 redirect，合并后两者都有。"""
    frontend = extract_react_semantics(REACT_FORM_FIXTURE, "http://localhost:3000")
    backend = extract_backend_semantics(DJANGO_VIEW_FIXTURE, "http://localhost:3000", "django")
    merged = _merge_models([frontend, backend], "django")
    assert len(merged.form_fields) > 0
    assert any(s.kind == "url_redirect" for s in merged.success_states)
```

### 9.2 Workflow 合成测试

```python
# tests/test_workflow_synthesis.py

def test_synthesize_produces_assertions():
    model = UISemanticModel(
        entry_url="http://localhost:3000/login",
        page_title="Login",
        form_fields=(
            FormField("username", "用户名", "text", True, (), False),
            FormField("password", "密码", "password", True, (), True),
        ),
        submit_actions=(SubmitAction("登录", None),),
        success_states=(SuccessState("url_redirect", "/dashboard", "test"),),
        error_states=(),
        data_displays=(),
        framework="html",
        confidence=0.9,
        parse_warnings=(),
    )
    yaml_text = synthesize_workflow(model, "login_test", "Login form verification")
    doc = yaml.safe_load(yaml_text)
    steps = doc["steps"]

    # 必须有 navigate
    assert any(s["action"] == "observe_browser" for s in steps)
    # 必须有 password 填充且标记 sensitive
    password_steps = [s for s in steps if s.get("value_from") == "input.password"]
    assert password_steps and password_steps[0].get("sensitive") is True
    # 必须有 URL 断言
    assert any(
        s.get("action") == "wait_for" and s.get("condition") == "url"
        for s in steps
    )
```

### 9.3 质量评分测试

```python
# tests/test_workflow_quality.py

def test_score_workflow_no_assertions():
    yaml_text = """
    schema_version: 1
    name: empty_workflow
    steps:
      - id: navigate
        action: observe_browser
        url: http://localhost
      - id: click
        action: click
        target: {text: Submit}
    """
    score = score_workflow_quality(yaml_text)
    assert score.total_score < 0.4
    assert score.covers_success_path is False
    assert len(score.gaps) > 0


def test_score_workflow_full_assertions():
    yaml_text = """
    schema_version: 1
    name: full_workflow
    steps:
      - id: navigate
        action: observe_browser
        url: http://localhost/login
      - id: assert_ready
        action: assert_browser_ready
        min_text_length: 10
      - id: fill_user
        action: paste
        target: {label: Username}
        value_from: input.username
      - id: fill_pass
        action: paste
        target: {label: Password}
        value_from: input.password
        sensitive: true
      - id: submit
        action: click
        target: {text: Login}
      - id: assert_redirect
        action: wait_for
        condition: url
        url_contains: /dashboard
        timeout_seconds: 5.0
      - id: assert_welcome
        action: wait_for
        condition: text
        text: Welcome back
        timeout_seconds: 3.0
    """
    score = score_workflow_quality(yaml_text)
    assert score.total_score >= 0.7
    assert score.covers_success_path is True
    assert score.business_assertion_count >= 2
```

### 9.4 MCP 集成测试

```python
# tests/test_mcp_generate_from_context.py

def test_mcp_generate_from_context_html(tmp_path):
    args = {
        "task_description": "Build a login form",
        "code_changes": [{
            "file_path": "templates/login.html",
            "before": None,
            "after": HTML_LOGIN_FIXTURE,
            "change_type": "added",
        }],
        "base_url": "http://localhost:8000/login",
        "workspace_root": str(tmp_path),
        "dry_run": True,
    }
    result = _generate_workflow_from_context_payload(args)
    assert result["status"] == "success"
    assert result["quality"]["score"] is not None
    assert result["quality"]["score"] >= 0.5
    assert result["yaml"] is not None   # dry_run=True 时返回 YAML
    assert "password" not in result["yaml"].lower().replace("input.password", "")  # 无明文密码
```

---

## 十、时间线与交付顺序

```
Week 1（5 天）
  - context_ingestion.py：HTML + React 解析器 + 框架检测 + 合并逻辑
  - tests/test_context_ingestion.py：覆盖率 ≥ 90%
  - 验收：pytest tests/test_context_ingestion.py 全部通过

Week 2（5 天）
  - workflow_synthesis.py：静态合成路径 + LLM 兜底路径
  - workflow_quality.py：质量评分器
  - tests/test_workflow_synthesis.py + tests/test_workflow_quality.py
  - 验收：synthesize_workflow 对 HTML/React 输出的 workflow 质量分 ≥ 0.6

Week 3（5 天）
  - mcp_server.py：新增 generate_workflow_from_context + verify_implementation
  - cli.py：新增 generate-from-diff + verify-impl 命令
  - tests/test_mcp_generate_from_context.py
  - 验收：Claude Code 调用 generate_workflow_from_context MCP tool 返回质量分 ≥ 0.6 的 workflow

Week 4（5 天）
  - 后端解析器：Django / FastAPI / Flask
  - Vue 解析器（基于 React 解析器扩展）
  - .vscode-agent-status.json 写入逻辑
  - VS Code 扩展：fs.watch + 状态刷新
  - 验收：运行 verify_implementation 后 VS Code 状态栏自动更新

Week 5（3 天）
  - 全量测试：pytest tests/ -q
  - 更新 DEVELOPMENT_LOG.md
  - 更新 CHANGELOG.md
  - 验收：778+ passed，0 regression
```

---

## 十一、风险登记

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 框架多样性超出解析器覆盖范围（Next.js、SvelteKit、Remix 等） | 高 | 中 | LLM 兜底路径兜底；置信度 < 0.5 自动切换；用 `parse_warnings` 向 AI 报告盲区 |
| 混淆/压缩代码导致解析器无输出 | 中 | 中 | 置信度返回 0.1，触发 LLM 路径；LLM 直接读源码 |
| LLM 生成 YAML 语法错误 | 中 | 低 | 生成后立即调用现有 `validate_workflow_file()` 校验；校验失败时返回 `status:partial` + 原始文本供 AI 修正 |
| `verify_implementation` 运行时间过长阻塞 AI 响应 | 低 | 高 | 加 `timeout_seconds` 参数，默认 30 秒；超时返回 `result:timeout` + 已完成步骤数 |
| 质量分虚高（断言存在但断言值无意义） | 中 | 中 | 评分中区分 `business_assertions`（有实际文本/URL 值）和 `structural_assertions`（仅检查元素存在），单独暴露两者数值 |
| .inputs.local.json 明文凭据泄露到 workflow 内容 | 低 | 高 | `synthesize_workflow` 输出中的所有 `value_from` 使用 `input.*` 变量引用，永不内联实际值；`scrub_secrets()` 在 MCP 响应出口再过一遍 |

---

## 十二、验收标准汇总

```
Phase 1 通过条件：
  [ ] extract_html_semantics 对包含 label+input 的 HTML 输出 confidence ≥ 0.7
  [ ] extract_react_semantics 对含 navigate() 的 JSX 输出至少 1 条 SuccessState(kind=url_redirect)
  [ ] extract_backend_semantics 对 Django redirect() 输出 SuccessState
  [ ] _merge_models 合并前端+后端模型，字段和 success_states 均完整
  [ ] detect_framework 对 .jsx/.tsx 文件返回 "react"

Phase 2 通过条件：
  [ ] synthesize_workflow 对置信度 ≥ 0.5 的模型输出的 YAML 可被 validate_workflow_file() 验证通过
  [ ] 生成的 YAML 中 password 字段标记 sensitive:true
  [ ] 生成的 YAML 不包含任何明文密码
  [ ] LLM 兜底路径在无 anthropic SDK 时退化到静态模板，不崩溃

Phase 3 通过条件：
  [ ] score_workflow_quality 对无断言 workflow 返回 score < 0.4
  [ ] score_workflow_quality 对含 wait_for(url) + wait_for(text) 的 workflow 返回 score ≥ 0.7
  [ ] gaps 字段对低分 workflow 给出至少 1 条可操作建议

Phase 4 通过条件：
  [ ] generate_workflow_from_context MCP tool 在 Claude Code 中可调用
  [ ] 返回的 quality.score 对 HTML 登录表单 ≥ 0.6
  [ ] verify_implementation 在 run_profile=supervised 下运行并返回 pass/fail
  [ ] 失败时返回 failed_step.fix_hint 非空

Phase 5 通过条件：
  [ ] verify_implementation 完成后 .vscode-agent-status.json 被写入
  [ ] VS Code 扩展状态栏在 1 秒内反映最新结果
  [ ] 状态栏失败时显示红色背景
```
