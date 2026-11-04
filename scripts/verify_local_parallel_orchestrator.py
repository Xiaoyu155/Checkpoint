"""Run a bounded, isolated Pacer generation check against local Gemini and Codex."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from visual_agent import provider_runtime
from visual_agent.subagent_orchestrator import SubagentOrchestrator, SubagentPlan, SubagentTask


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    evidence_dir = Path(sys.argv[1]).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    run_id = 'local-parallel-' + uuid.uuid4().hex
    workspace = Path(tempfile.mkdtemp(prefix=run_id + '-', dir=evidence_dir))
    (workspace / 'tests').mkdir()
    files = {
        'module_a.py': 'def normalize_unique_labels(values):\n    raise NotImplementedError()\n',
        'module_b.py': 'def parse_page_limit(value, default=20, maximum=200):\n    raise NotImplementedError()\n',
        'tests/test_a.py': '''import module_a\n\ndef test_normalize():\n    assert module_a.normalize_unique_labels([' A ', 'a', ' B ']) == ['A', 'B']\n\ndef test_empty():\n    assert module_a.normalize_unique_labels(None) == []\n    assert module_a.normalize_unique_labels(['', None, ' ']) == []\n''',
        'tests/test_b.py': '''import module_b\n\ndef test_limits():\n    assert module_b.parse_page_limit('12') == 12\n    assert module_b.parse_page_limit('300') == 200\n\ndef test_invalid():\n    for value in (None, True, 'bad', '0', '-1'):\n        assert module_b.parse_page_limit(value) == 20\n''',
    }
    for path, content in files.items():
        (workspace / path).write_text(content, encoding='utf-8')
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')
    os.environ['PACER_CODEX_TIMEOUT_SECONDS'] = '90'
    os.environ['PACER_GEMINI38_FALLBACK_MODEL'] = 'gemini-3.8-flash-low'

    # Explicit local node and per-task route prevent loading any external provider.
    node = provider_runtime.ProviderNode('gemini38', 1, 'antigravity_gemini38', 'openai',
        'gemini-3.8-flash-low', 'http://127.0.0.1:8080', '/chat/completions',
        True, 'sk-local', [], True, False, False)
    provider_runtime.antigravity_node = lambda *args, **kwargs: node
    resolve = provider_runtime.resolve_coder_routes
    provider_runtime.resolve_coder_routes = lambda *, preferred='', cascade=None, cooldowns=None: resolve(
        preferred=preferred, cascade=preferred if preferred in {'gemini38', 'codex'} else '', cooldowns=cooldowns)

    descriptions = [
        ('a', 'gemini38', 'normalize_unique_labels(values): return [] for None; convert non-None items to stripped strings, ignore empty strings, deduplicate case-insensitively preserving first spelling and order.'),
        ('b', 'codex', 'parse_page_limit(value, default=20, maximum=200): return default for None, booleans, invalid text and values below 1; convert integer strings, and cap positive values at maximum.'),
    ]
    tasks = [SubagentTask(id='worker_' + name, title='Implement module ' + name, role='coder', backend=backend,
        instructions=f'Implement {contract} Edit only module_{name}.py. Return exactly one SEARCH/REPLACE block for module_{name}.py. Do not edit tests.',
        target_files=[f'module_{name}.py'], verification_command=f'python -m pytest tests/test_{name}.py -q', max_retries=0)
        for name, backend, contract in descriptions]
    plan = SubagentPlan(goal='Implement two independent utility modules with protected acceptance tests.', tasks=tasks, repo_root=str(workspace))
    test_paths = ['tests/test_a.py', 'tests/test_b.py']
    before_hashes = {p: digest((workspace / p).read_bytes()) for p in test_paths}
    env = dict(os.environ)
    baseline = subprocess.run([sys.executable, '-m', 'pytest', *test_paths, '-q'], cwd=workspace,
                              env=env, capture_output=True, text=True, timeout=20)
    if baseline.returncode != 1 or 'failed' not in baseline.stdout:
        raise RuntimeError('Baseline did not produce test failures: ' + baseline.stdout[-1000:])
    orchestrator = SubagentOrchestrator(plan, workspace_root=workspace, enforce_test_tamper_guard=True,
        require_red_baseline=True, enforce_anti_slop=False, enforce_witness_verifier=False,
        heartbeat_interval=0, show_timeline=False, defer_trace_recording=True)
    events = []
    event_lock = threading.Lock()
    original = orchestrator._invoke_single_backend

    def observed(backend, task, prompt):
        started_at, t0 = now(), time.monotonic()
        output, error = '', 'interrupted'
        try:
            output, error = original(backend, task, prompt)
            return output, error
        finally:
            with event_lock:
                events.append({'task_id': task.id, 'backend': backend, 'started_at': started_at,
                    'finished_at': now(), 'duration_seconds': round(time.monotonic() - t0, 3),
                    'success': bool(output and not error), 'error': error})

    orchestrator._invoke_single_backend = observed
    started_at, t0 = now(), time.monotonic()
    try:
        summary = orchestrator.run()
        guard_ok, _, tampered = orchestrator.guard.verify_and_enforce()
        final = subprocess.run([sys.executable, '-m', 'pytest', *test_paths, '-q'], cwd=workspace,
                               env=env, capture_output=True, text=True, timeout=20)
        after_hashes = {p: digest((workspace / p).read_bytes()) for p in test_paths}
    finally:
        orchestrator.guard.unlock_all()
    elapsed = round(time.monotonic() - t0, 3)
    for event in events:
        task = next(t for t in tasks if t.id == event['task_id'])
        target = task.target_files[0]
        after = (workspace / target).read_text(encoding='utf-8')
        diff = ''.join(difflib.unified_diff(files[target].splitlines(keepends=True), after.splitlines(keepends=True),
                                            fromfile='before/' + target, tofile='after/' + target))
        event.update({'applied_files': task.patched_files, 'diff_sha256': digest(diff.encode()),
                      'diff': diff, 'task_status': task.status})
    pair = [next((e for e in events if e['task_id'] == task.id and e['backend'] == task.backend and e['success']), None) for task in tasks]
    overlap = 0.0
    if all(pair):
        overlap = max(0.0, (min(datetime.fromisoformat(e['finished_at']) for e in pair)
                            - max(datetime.fromisoformat(e['started_at']) for e in pair)).total_seconds())
    passed = bool(summary.get('success') and len(events) == 2 and all(pair) and overlap > 0 and
                  all(t.status == 'completed' and t.patched_files == t.target_files for t in tasks) and
                  [t.backend for t in tasks] == ['gemini38', 'codex'] and final.returncode == 0 and
                  guard_ok and not tampered and before_hashes == after_hashes and
                  len(summary.get('waves', [])) == 1)
    receipt = {'status': 'verified' if passed else 'failed', 'orchestrator_run_id': run_id,
        'workspace': str(workspace), 'started_at': started_at, 'finished_at': now(),
        'gemini_endpoint': 'http://127.0.0.1:8080/v1/chat/completions', 'gemini_cli_fallback': False,
        'total_wall_seconds': elapsed, 'waves': summary.get('waves'), 'workers': events,
        'overlap_seconds': round(overlap, 3), 'baseline_pytest': {'exit_code': baseline.returncode, 'output': baseline.stdout},
        'tamper_detected': bool(tampered or not guard_ok or before_hashes != after_hashes),
        'protected_test_sha256_before': before_hashes, 'protected_test_sha256_after': after_hashes,
        'pytest': {'exit_code': final.returncode, 'output': final.stdout, 'stderr': final.stderr},
        'orchestrator_success': summary.get('success'), 'failover_path': summary.get('failover_path'),
        'task_errors': {t.id: t.error for t in tasks}, 'review': 'No task_review phase configured for this direct SubagentOrchestrator plan.'}
    path = evidence_dir / ('orchestrator_parallel_' + run_id + '.json')
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'receipt': str(path), 'seconds': elapsed,
                      'overlap': overlap, 'workers': events, 'pytest_exit_code': final.returncode}, ensure_ascii=False), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
