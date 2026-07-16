# Pacer Dogfood 专项重构

日期：2026-07-15

目标不是让 Dogfood 制造更多日志，而是让错误在发布前安静、稳定、低成本地消失。Pacer 把“善战者无赫赫之功”落实为三个原则：同一候选、独立运行、标准证明。

## GitHub 对标

| 项目 | 固定实现 | 采纳内容 |
|---|---|---|
| GitHub Artifact Attestations | [`actions/attest-build-provenance@0f67c3f`](https://github.com/actions/attest-build-provenance/blob/0f67c3f4856b2e3261c31976d6725780e5e4c373/.github/workflows/ci.yml) | job 级 `attestations: write` + `id-token: write`，subject digest 与 OIDC 身份绑定 |
| SLSA GitHub Generator | [`generator_generic_slsa3.yml@4d014fa`](https://github.com/slsa-framework/slsa-github-generator/blob/4d014fae4dbd39eb09e8d40348b73db095e6ba9a/.github/workflows/generator_generic_slsa3.yml) | 构建与 provenance 分 job、secure artifact handoff、in-toto bundle |
| Ruff | [`release.yml@a616e08`](https://github.com/astral-sh/ruff/blob/a616e0873c32d3f82d89af4a9959dcf6f5a3d04a/.github/workflows/release.yml) | action 固定完整 SHA、先汇聚不可变 artifact，再 attest/publish |
| Pants | [`release.yaml@4f08d3f`](https://github.com/pantsbuild/pants/blob/4f08d3faeef64e36c9299f94f94bf9681d9846b7/.github/workflows/release.yaml) | 工具自举、用自己构建 wheel/PEX、smoke test、逐 artifact 证明 |
| Sigstore Cosign | [`kind-verify-attestation.yaml@8ca5b20`](https://github.com/sigstore/cosign/blob/8ca5b2002f5cd43614c476665e2055e59392b59d/.github/workflows/kind-verify-attestation.yaml) | 由维护的 verifier 校验 subject、issuer、identity 和 policy，不自研 DSSE/Sigstore 验签器 |
| uv | [`release.yml@950acf0`](https://github.com/astral-sh/uv/blob/950acf046e570ebb0eb1437fa6e20e28ed14719f/.github/workflows/release.yml) | release environment 保护、artifact 汇聚、构建与发布职责分离 |

Pacer 不复制这些项目的 agent loop，也不实现自己的 OIDC、DSSE、Sigstore 或 SLSA verifier。`github_attestation.py` 只是薄适配器，固定调用 `gh attestation verify`，并将退出码、subject SHA-256、repository、workflow 和 run identity 投影为有界事实。

## 三级标准

| Lane | 分数 | 机械条件 | 用途 |
|---|---:|---|---|
| Local | 85 | A/B wheel 不同、Pacer 自改、可信验收、B 全新安装、自检、artifact/HMAC 完整 | 快速反馈，不可发布 |
| CI | 95 | Local 全部条件 + candidate wheel 与 canonical evidence 均有 GitHub OIDC attestation | 合并/候选门禁 |
| Release | 100 | CI 全部条件 + 唯一 GitHub run identity；同一候选连续 3 次 | release-ready 的 Dogfood 部分 |

分数不是成功替代品。任一关键控制失败时，即使加总分数达到阈值也不能通过。默认 CLI 阈值为 95；HMAC-only 证据最多 85，不能冒充 CI Dogfood。

## 关键纠偏

旧门禁拒绝三次 Dogfood 使用相同 candidate wheel，反而要求每次重打 artifact。这不符合成熟发布实践：发布验证应固定同一个不可变候选，在独立环境中重复验证。

新 streak 规则：

- candidate wheel SHA-256 必须始终相同；变化记为 `candidate_drift` 并重置 streak。
- evidence digest 必须各不相同；重复证据拒绝。
- GitHub run identity digest 必须各不相同；重复运行拒绝。
- timeout、5xx、crash、warning、retry、证据重提或 artifact 篡改仍立即失败。

## 落地文件

- `.pacer/dogfood.json`：95/100 分标准、三级 lane、同候选三次策略和固定 GitHub 参考 commit。
- `.github/workflows/pacer-dogfood.yml`：可复用 proof/verification workflow。调用方上传 candidate artifact 与 canonical evidence artifact；workflow 分别 attest、全新安装候选、调用 `gh attestation verify`、执行 95 分门禁，并 attest 最终验证结果。
- `dogfood_quality.py`：8 项机械控制、固定权重和关键控制门禁。
- `github_attestation.py`：GitHub CLI 薄适配器，不处理密钥、不实现密码学。
- `dogfood_policy.py`：canonical policy、pinned reference、lane 和阈值校验。
- `release_gate.py`：同一候选、不同 evidence、不同 run identity 的连续运行语义。

## 调用方合同

生产 job 必须用已安装 wheel A 托管 Pacer 源码变化，生成 wheel B 和 `.pacer/dogfood-evidence.json`，然后上传两个独立 artifact。验证 job 以 job-level reusable workflow 调用：

```yaml
jobs:
  produce:
    # Install wheel A, run a real Pacer mission, build/install wheel B,
    # and upload candidate/evidence artifacts.
    steps: []

  prove:
    needs: produce
    uses: ./.github/workflows/pacer-dogfood.yml
    with:
      candidate_artifact_name: pacer-candidate
      evidence_artifact_name: pacer-dogfood-evidence
```

producer 与 verifier 分离是刻意设计：producer 可以使用 Codex/OpenAI secret，verifier 不接收该 secret，只拥有读取仓库、申请 OIDC 和写 attestation 的最小权限。proof workflow 禁止 `pull_request_target`，所有第三方 action 固定完整 commit SHA。

## 命令

```console
pacer pacer-dogfood-policy-check --repo-root .
pacer pacer-dogfood-check --repo-root . --minimum-score 95
pacer pacer-dogfood-check --repo-root . --artifact-root .dogfood/candidate --github-repository Xiaoyu155/Checkpoint --signer-workflow Xiaoyu155/Checkpoint/.github/workflows/pacer-dogfood.yml@refs/heads/main --require-github-provenance --minimum-score 95
```

## 当前边界

代码和 CI proof lane 已具备 95/100 分机械标准，但当前本地 checkout 仍没有真实 `.pacer/dogfood-evidence.json`，也没有在 GitHub runner 上产生 OIDC attestation。因此本轮不能宣称 Dogfood 已达到 95 分，只能宣称“达到 95 分所需的标准、适配器和 proof workflow 已实现”。真正达到 95 分必须由一次远端 CI A-to-B run 生成不可伪造证明；达到 100 分必须对同一 candidate wheel 完成三次独立 run。

专项候选 wheel：`.runs/pacer-dogfood-95-20260715-v2/visual_agent-0.1.2-py3-none-any.whl`，SHA-256 `E7D27FD63E22BF851D627DDDF858B0B394993CA14D40E0231EEFAD26821B923E`。全新 Python 3.13 venv 安装、`pip check`、policy 95/100、release manifest 锁、site-packages 导入和包内 Checkpoint workflow 均通过；缺少真实 evidence 时安装态 CLI 仍非零退出。
