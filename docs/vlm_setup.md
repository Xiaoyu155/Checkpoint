# VLM Setup

Checkpoint uses structured providers first: DOM for browsers and Windows UIA
for desktop apps. A VLM is optional and only acts as a visual fallback when
structured selectors cannot locate a target.

## Cloud Provider

Create a local credentials file that is ignored by Git:

```powershell
notepad model_api_keys.txt
```

Add an OpenAI-compatible key:

```text
openai api key: sk-...
```

Then check readiness without printing the secret:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli model-credentials-inspect --source model_api_keys.txt --preferred openai --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli doctor
```

You can also use environment variables:

```powershell
$env:VISUAL_AGENT_MODEL_PROVIDER="openai"
$env:VISUAL_AGENT_OPENAI_API_KEY="sk-..."
```

## Local Provider

Local VLM support requires model-specific dependencies and model files:

```powershell
.\.venv\Scripts\python.exe -m pip install torch transformers
```

Then configure the workflow or environment for the selected local engine. If no
local model is configured, Checkpoint can still run DOM and UIA workflows.

## OCR

OCR is separate from VLM. Install both the Python wrapper and the Tesseract
binary if you need OCR fallback:

```powershell
.\.venv\Scripts\python.exe -m pip install pytesseract
```

Then install the Tesseract binary for Windows and make sure it is on `PATH`.

## Safety

- Never commit `model_api_keys.txt`.
- Run `doctor` to confirm whether cloud, local VLM, OCR, DOM, and UIA providers
  are available.
- Prefer DOM/UIA selectors. Use VLM only as a fallback.

