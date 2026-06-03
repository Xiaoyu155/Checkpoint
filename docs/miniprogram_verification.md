# WeChat Mini Program Verification

Visual Agent can verify a WeChat Mini Program from the outside of WeChat
DevTools. It first uses Windows UI Automation to find the DevTools/project
window, then captures the simulator region for screenshots, OCR, or VLM checks.

## Default Workflows

New workspaces include three mini program workflows:

- `wechat_devtools_shell` checks that WeChat DevTools is open and the current
  project shell is visible.
- `miniprogram_simulator_capture` captures the simulator/rendering region as
  visual evidence. This works without OCR or VLM.
- `miniprogram_visual_text_contract` OCRs the simulator region and checks a
  required text such as `填分数` or `购买服务`.

## Run The Useful Baseline

```powershell
python -m visual_agent.cli init-workspace --root .agent-workspace --overwrite
python -m visual_agent.cli workspace-run --root .agent-workspace --workflow miniprogram_simulator_capture --run-profile dry-run
python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
```

The run report contains a screenshot path. Open that image to confirm the crop
is actually the mini program simulator.

## Match Your Project Window

The default templates look for UIA elements containing one of the concrete
DevTools/project window names:

```yaml
window:
  title_contains_any:
    - "微信开发者工具"
    - "alipay-miniprogram"
```

If your DevTools window uses another project name, edit
`.agent-workspace/inputs/miniprogram_default.json` and add your project title to
`window_title_candidates`. Prefer the exact DevTools simulator/project title;
avoid broad words that also appear in your code editor.

The default mini program workflows also request `bring_to_front: true`, so
Visual Agent tries to restore and foreground the matched DevTools window before
capturing the simulator.

## Tune The Simulator Crop

If the screenshot includes too much DevTools chrome or misses the simulator,
adjust `simulator_crop`:

```yaml
simulator_crop:
  left_percent: 0.0
  top_percent: 0.0
  width_percent: 0.7
  height_percent: 1.0
```

The percentages are relative to the matched DevTools/project window.

## Verify Real Page Text With OCR

Install OCR support first:

```powershell
pip install pytesseract
```

You also need the Tesseract binary installed and available on `PATH`. If the
workflow reports `OCR engine unavailable`, the screenshot capture worked but the
OCR binary is missing or not on `PATH`.

Then run:

```powershell
python -m visual_agent.cli workspace-run --root .agent-workspace --workflow miniprogram_visual_text_contract --run-profile dry-run --inputs-file miniprogram_default.json
```

Change `required_text` in `.agent-workspace/inputs/miniprogram_default.json` for
the page contract you want Codex to preserve.

For Chinese OCR, the default input uses:

```json
"ocr_language": "chi_sim+eng"
```

Change it only if your Tesseract installation uses different language packs.

## Development Rule

When Codex changes mini program UI or behavior, run at least:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --tags verification,miniprogram --run-profile dry-run --format markdown
```

For true page-level acceptance, also run `miniprogram_visual_text_contract`
with the text that must be present on the target screen.
