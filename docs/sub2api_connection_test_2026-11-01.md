# Sub2API 定向接入测试

用户要求重新核实外援凭据。凭据文件始终仅在内存中读取；未写入源码、配置、日志或 Git。

## 结论与实测

两个 Key 均成功完成 Claude 原生 Messages 调用。不能根据此前的 HTTP 403 判定密钥或模型服务不可用。

| 检查 | 真实结果 |
|---|---|
| 第一个 Key，Bearer 鉴权，Python 默认 User-Agent，`GET /v1/models` | HTTP 403，响应体 `error code: 1010` |
| 同一 Key、同一路径及鉴权，改为明确的 Pacer User-Agent | HTTP 200 |
| 第一个 Key，`POST /v1/messages`，`claude-haiku-4-5-20251001`，输出上限 16 token | HTTP 200，`OK`；实际输入 6、输出 1 token；5.45 秒 |
| 第二个 Key，通过修复后的 `run_llm_completion`，相同模型与输出上限 | HTTP 200，`OK`；5.22 秒 |

模型列表请求不生成文本；每个 Key 仅做一次最小生成测试。第二次适配器只返回文本，没有保留原始 usage，所以不声称第二次精确计费量。没有使用这些 Key 运行开发任务或启动后台付费调用。

根因验证使用同一个 Key 与相同 Bearer 认证做对照，只改变 User-Agent：默认 Python 客户端被网关前的 Cloudflare 规则拒绝。修复为 HTTP completion 请求增加 `User-Agent: Pacer`，保留原有认证方式，不重试或掩盖真正的鉴权错误。

Sub2API 支持 Anthropic 兼容接入，可参考[项目维护者说明](https://github.com/Wei-Shaw/sub2api)。本次文件中的 Key 标注用于 Claude，因此实测采用 `/v1/messages`、`x-api-key` 和 `anthropic-version`；没有把它发送到官方 Anthropic 地址。

## 可复用配置格式

如果后续明确授权启用，可在使用者的当前进程中提供 `SUB2API_API_KEY`，再将以下 JSON 作为 `PACER_API_POOL` 值。占位主机应替换成密钥文件中的网关地址，密钥不写进 JSON：

```json
[
  {
    "provider": "anthropic",
    "model": "claude-haiku-4-5-20251001",
    "base_url": "https://YOUR_SUB2API_HOST/v1",
    "endpoint": "/messages",
    "api_key_env": "SUB2API_API_KEY"
  }
]
```

## 回归验收

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_llm_providers.py tests/test_api_cascade_recovery.py -q --tb=short
```

```text
25 passed in 7.20s
```

新增的模拟网关测试验证默认 Python 标识会被 403 拒绝、Pacer 适配器携带明确标识，以及原生 Messages 路径、认证头、版本头和输出 token 上限。单测不使用真实密钥。
