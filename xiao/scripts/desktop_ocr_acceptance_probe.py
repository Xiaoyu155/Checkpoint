from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / ".agent-workspace"
PAGE = WORKSPACE / "fixtures" / "desktop_ocr_real_acceptance_page.html"
INPUTS = WORKSPACE / "inputs" / "desktop_ocr_real_acceptance_inputs.json"
NEGATIVE_INPUTS = WORKSPACE / "inputs" / "desktop_ocr_real_acceptance_negative_inputs.json"
WORKFLOW = WORKSPACE / "workflows" / "desktop_ocr_real_acceptance.yaml"


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Desktop OCR Real Acceptance Probe</title>
  <style>
    html, body { margin: 0; min-height: 100%; font-family: Arial, Helvetica, sans-serif; background: #f5f7fa; color: #111827; }
    main { display: grid; place-items: center; min-height: 100vh; }
    section { width: min(860px, 82vw); text-align: center; padding: 56px; background: white; border: 2px solid #111827; }
    h1 { font-size: 48px; margin: 0 0 28px; letter-spacing: 0; }
    .facts { display: grid; gap: 14px; margin: 0 0 30px; font-size: 36px; font-weight: 700; }
    .status { color: #075985; }
    label { display: block; margin: 0 0 12px; font-size: 34px; font-weight: 700; }
    input { width: min(420px, 70vw); margin: 0 0 12px; padding: 18px 24px; text-align: center; font-size: 46px; font-weight: 700; border: 5px solid #111827; }
    #entered { min-height: 44px; margin: 0 0 24px; font-size: 34px; font-weight: 700; }
    .click-target { cursor: pointer; }
    button { font-size: 52px; font-weight: 700; padding: 24px 72px; color: #111827; background: #fff; border: 6px solid #111827; cursor: pointer; }
    #result { min-height: 92px; margin-top: 36px; font-size: 72px; font-weight: 800; color: #991b1b; }
  </style>
</head>
<body>
  <main>
    <section>
      <h1>OCR PROBE</h1>
      <div class="facts">
        <div>ORDER A100</div>
        <div>TOTAL 128</div>
        <div class="status">READY</div>
      </div>
      <label for="amount">AMOUNT</label>
      <div>FIELD EMPTY</div>
      <input id="amount" value="" oninput="document.getElementById('entered').textContent=this.value ? 'ENTERED ' + this.value : '';">
      <div id="entered" aria-live="polite"></div>
      <button class="click-target" type="button" onclick="document.getElementById('result').textContent=document.getElementById('amount').value === '128' ? 'PASSED RECEIPT TOTAL 128' : 'FAILED RECEIPT TOTAL ' + document.getElementById('amount').value">APPROVE</button>
      <div id="result" aria-live="polite"></div>
    </section>
  </main>
</body>
</html>
"""


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--probe-app":
        run_probe_app()
        return 0

    PAGE.parent.mkdir(parents=True, exist_ok=True)
    INPUTS.parent.mkdir(parents=True, exist_ok=True)
    PAGE.write_text(HTML, encoding="utf-8")
    INPUTS.write_text(json.dumps(positive_inputs(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    NEGATIVE_INPUTS.write_text(json.dumps(negative_inputs(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    app = open_probe_window()
    try:
        run = run_workflow(INPUTS)
    finally:
        app.terminate()
        try:
            app.wait(timeout=3)
        except subprocess.TimeoutExpired:
            app.kill()
    negative_app = open_probe_window()
    try:
        negative_run = run_workflow(NEGATIVE_INPUTS, expect_success=False)
    finally:
        negative_app.terminate()
        try:
            negative_app.wait(timeout=3)
        except subprocess.TimeoutExpired:
            negative_app.kill()
    acceptance = run.get("acceptance") if isinstance(run.get("acceptance"), dict) else {}
    negative_steps = negative_run.get("steps") if isinstance(negative_run.get("steps"), list) else []
    negative_failed = any(step.get("status") == "failed" for step in negative_steps)
    payload = {
        "status": "success"
        if run.get("steps")
        and all(step.get("status") == "success" for step in run["steps"])
        and acceptance.get("is_product_acceptance")
        and negative_failed
        else "failed",
        "run_id": run.get("run_id"),
        "run_dir": run.get("run_dir"),
        "acceptance_level": acceptance.get("label"),
        "is_product_acceptance": acceptance.get("is_product_acceptance"),
        "valid_operation_receipts": acceptance.get("valid_operation_receipts"),
        "invalid_operation_receipts": acceptance.get("invalid_operation_receipts"),
        "product_acceptance_blockers": acceptance.get("product_acceptance_blockers"),
        "negative_run_id": negative_run.get("run_id"),
        "negative_run_dir": negative_run.get("run_dir"),
        "negative_case_failed_as_expected": negative_failed,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "success" else 1


def positive_inputs() -> dict[str, object]:
    return {
        "engine": "tesseract",
        "amount_value": "128",
        "approve_text": "APPROVE",
        "expected_amount_text": "ENTERED 128",
        "expected_after_text": "PASSED",
        "required_before": ["ORDER", "TOTAL", "READY", "AMOUNT", "APPROVE"],
        "forbidden_before": ["PASSED", "RECEIPT", "FAILED"],
        "required_after": ["PASSED", "RECEIPT", "TOTAL", "128"],
        "forbidden_after": ["FAILED"],
        "wait_seconds": 0.5,
    }


def negative_inputs() -> dict[str, object]:
    payload = dict(positive_inputs())
    payload["amount_value"] = "129"
    payload["expected_amount_text"] = "ENTERED 129"
    return payload


def open_probe_window() -> subprocess.Popen:
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--probe-app"], cwd=ROOT)
    time.sleep(3)
    foreground_window("Desktop OCR Real Acceptance Probe")
    return process


def run_probe_app() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("Desktop OCR Real Acceptance Probe")
    root.geometry("1200x900+520+220")
    root.configure(bg="#f5f7fa")
    root.attributes("-topmost", True)
    root.after(1000, lambda: root.attributes("-topmost", False))

    frame = tk.Frame(root, bg="white", highlightbackground="#111827", highlightthickness=2)
    frame.pack(expand=True, fill="both", padx=70, pady=70)

    tk.Label(frame, text="OCR PROBE", bg="white", fg="#111827", font=("Arial", 42, "bold")).pack(pady=(34, 24))
    tk.Label(frame, text="ORDER A100", bg="white", fg="#111827", font=("Arial", 32, "bold")).pack(pady=5)
    tk.Label(frame, text="TOTAL 128", bg="white", fg="#111827", font=("Arial", 32, "bold")).pack(pady=5)
    tk.Label(frame, text="READY", bg="white", fg="#075985", font=("Arial", 32, "bold")).pack(pady=(5, 24))

    result = tk.StringVar(value="")
    amount = tk.StringVar(value="")
    entered = tk.StringVar(value="")

    def approve() -> None:
        value = amount.get().strip()
        if value == "128":
            result.set("PASSED RECEIPT TOTAL 128")
        else:
            result.set(f"FAILED RECEIPT TOTAL {value}")

    def update_entered(_event: object | None = None) -> None:
        value = amount.get().strip()
        entered.set(f"ENTERED {value}" if value else "")

    amount_label = tk.Label(frame, text="AMOUNT", bg="white", fg="#111827", font=("Arial", 30, "bold"))
    amount_label.pack(pady=(0, 5))
    tk.Label(frame, text="FIELD EMPTY", bg="white", fg="#111827", font=("Arial", 30, "bold")).pack(pady=(0, 5))
    entry = tk.Entry(
        frame,
        textvariable=amount,
        justify="center",
        bg="white",
        fg="#111827",
        font=("Arial", 38, "bold"),
        borderwidth=5,
        relief="solid",
    )
    entry.pack(ipadx=36, ipady=10, pady=(0, 8))
    entry.bind("<KeyRelease>", update_entered)
    tk.Label(frame, textvariable=entered, bg="white", fg="#111827", font=("Arial", 30, "bold")).pack(pady=(0, 18))

    def focus_amount(_event: object | None = None) -> None:
        entry.focus_set()
        entry.icursor("end")

    amount_label.bind("<Button-1>", focus_amount)

    approve_label = tk.Label(
        frame,
        text="APPROVE",
        bg="white",
        fg="#111827",
        font=("Arial", 44, "bold"),
        borderwidth=6,
        relief="solid",
        padx=56,
        pady=14,
    )
    approve_label.pack(pady=(0, 16))
    approve_label.bind("<Button-1>", lambda _event: approve())
    tk.Label(frame, textvariable=result, bg="white", fg="#991b1b", font=("Arial", 44, "bold")).pack(pady=(4, 0))
    root.mainloop()


def foreground_window(title_fragment: str) -> None:
    user32 = ctypes.windll.user32
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if title_fragment in buffer.value:
            handles.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc, 0)
    if handles:
        user32.ShowWindow(handles[0], 3)
        user32.SetForegroundWindow(handles[0])
        time.sleep(1)


def run_workflow(inputs_path: Path, *, expect_success: bool = True) -> dict:
    command = [
        sys.executable,
        "-m",
        "visual_agent.cli",
        "run-workflow",
        "--workflow",
        str(WORKFLOW),
        "--inputs-file",
        str(inputs_path),
        "--output-dir",
        str(WORKSPACE / "runs"),
        "--run-profile",
        "supervised",
        "--allow-click",
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0 and expect_success:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    if not result.stdout.strip():
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return json.loads(result.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
