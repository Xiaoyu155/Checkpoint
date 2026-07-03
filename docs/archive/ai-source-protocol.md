# AI Source Protocol (`x-ai-source`)

An open, optional convention that lets Checkpoint distinguish real model output from degraded fallbacks during acceptance. Any web app can adopt it; apps that do not adopt it still get all other Checkpoint capabilities.

## The problem

AI features commonly degrade silently: the model call fails or times out, the backend returns a canned fallback, and the page still says "生成成功". Text-quality heuristics alone cannot reliably tell these apart, so a green test on the fallback path gets misreported as "the AI feature works".

## The convention

The backend labels every AI-derived HTTP response with one header:

```
x-ai-source: real        # produced by the real model path
x-ai-source: degraded    # produced by a weaker/cheaper backup path
x-ai-source: fallback    # produced by canned/non-AI fallback logic
```

That is the entire protocol. No SDK, no registration.

## What Checkpoint does with it

- Browser sessions record `ai_source` on every labeled network event.
- `assert_ai_response_quality` always reports the run's AI path in its result
  (`ai_source: real | degraded | fallback | unknown`). The worst label seen
  wins: one `fallback` response taints the path.
- With `require_real_ai: true`, the assertion **fails** when the path is
  `degraded`, `fallback`, or `unknown`. Unknown fails deliberately: an app
  that has not implemented the convention cannot claim real-AI verification.
- `ai_url_contains` scopes classification to matching URLs when a page mixes
  AI and non-AI requests.

```yaml
- id: ai_quality
  action: assert_ai_response_quality
  question: "帮我推荐一份杭州两日游行程"
  require_real_ai: true
  ai_url_contains: "/api/ai/"
```

## Honest reporting

Without `require_real_ai`, a degraded-path pass is still a pass — but the
report carries `ai_source: degraded`, so nobody can read it as a
commercial-model pass. The three states are exactly:

| state | meaning |
| --- | --- |
| `real` | real model path verified |
| `degraded` / `fallback` | feature works on the backup path only |
| `unknown` | the app does not implement the convention; AI authenticity unverified |
