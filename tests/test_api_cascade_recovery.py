from unittest.mock import Mock
import json

import pytest

import visual_agent.subagent_orchestrator as module
from visual_agent.subagent_orchestrator import SubagentOrchestrator, SubagentPlan, SubagentTask


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("PACER_CODER_CASCADE", "api")
    task = SubagentTask(id="writer", title="repair", role="coder", backend="api", instructions="repair app.py")
    orch = SubagentOrchestrator(SubagentPlan(goal="repair", tasks=[task], repo_root=str(tmp_path)), workspace_root=tmp_path)
    return orch, task


@pytest.fixture
def completion_config(monkeypatch):
    config = dict(available=True, provider="qwen", model="qwen-test", _api_key="test-only",
                  base_url="https://example.invalid/v1", endpoint="/chat/completions")
    monkeypatch.setattr(module, "resolve_model_provider_config", lambda: config)
    return config


def test_api_keeps_full_completion_and_stateless_prompt(worker, completion_config, monkeypatch):
    orch, task = worker
    task._stateless_prompt = "current canvas and complete patch instructions"
    response = "x" * 12000 + "\n>>>>>>> REPLACE"
    complete = Mock(return_value=response)
    monkeypatch.setattr(module, "run_llm_completion", complete)
    assert orch._dispatch_to_backend(task) == (response, None)
    args = complete.call_args.kwargs
    assert args["prompt"] == task._stateless_prompt
    assert args["backend"].provider == "qwen"
    assert args["backend"].model_id == "qwen-test"
    assert args["api_key"] == "test-only"
    assert args["base_url"] == completion_config["base_url"]
    assert args["max_tokens"] > 64


def test_api_error_survives_and_opens_circuit_without_other_backends(worker, completion_config, monkeypatch):
    orch, task = worker
    complete = Mock(side_effect=RuntimeError("HTTP 401: invalid_api_key"))
    monkeypatch.setattr(module, "run_llm_completion", complete)
    # Even if every other backend is available, the explicit list constrains calls.
    monkeypatch.setattr(orch, "_is_backend_live", lambda name: True)
    out, error = orch._dispatch_to_backend(task)
    assert out == ""
    assert "HTTP 401" in error
    assert orch._backend_in_cooldown("api")
    assert [d["backend"] for d in orch._prompt_dispatches] == ["api"]
    complete.assert_called_once()


def test_missing_configuration_does_not_call_completion(worker, monkeypatch):
    orch, task = worker
    monkeypatch.setattr(module, "resolve_model_provider_config", lambda: dict(available=False, blockers=["missing_api_key"]))
    complete = Mock()
    monkeypatch.setattr(module, "run_llm_completion", complete)
    assert "missing_api_key" in orch._dispatch_to_backend(task)[1]
    complete.assert_not_called()


@pytest.mark.parametrize("response", ["", " \n\t"])
def test_empty_completion_is_failure(worker, completion_config, monkeypatch, response):
    orch, task = worker
    monkeypatch.setattr(module, "run_llm_completion", Mock(return_value=response))
    out, error = orch._dispatch_to_backend(task)
    assert out == ""
    assert "empty content" in error


@pytest.mark.parametrize("configured,current,expected", [
    ("api", "api", "api"), ("", "api", "api"),
    ("api,codex", "api", "codex"), ("api,codex", "codex", "api"),
    ("api,codex", "workbuddy", "api"), (" API,api,CODEX ", "API", "codex"),
])
def test_retry_follows_configured_order(worker, monkeypatch, configured, current, expected):
    orch, _ = worker
    monkeypatch.setenv("PACER_CODER_CASCADE", configured)
    assert orch._next_cascade_backend(current) == expected
    if not configured:
        assert orch._coder_cascade() == []


@pytest.mark.parametrize("stale_backend", ["workbuddy", "gemini38", "codex"])
def test_explicit_cascade_constrains_stale_task_backend(worker, monkeypatch, stale_backend):
    orch, task = worker
    task.backend = stale_backend
    invoke = Mock(return_value=("complete response", None))
    monkeypatch.setattr(orch, "_invoke_single_backend", invoke)
    assert orch._dispatch_to_backend(task) == ("complete response", None)
    assert invoke.call_args.args[0] == "api"
    invoke.assert_called_once()


def test_explicit_codex_is_not_replaced_by_workbuddy(worker, monkeypatch):
    orch, task = worker
    task.backend = "codex"
    monkeypatch.setenv("PACER_CODER_CASCADE", "codex")
    monkeypatch.setattr(orch, "_is_backend_live", lambda name: True)
    invoke = Mock(return_value=("complete response", None))
    monkeypatch.setattr(orch, "_invoke_single_backend", invoke)
    orch._dispatch_to_backend(task)
    assert invoke.call_args.args[0] == "codex"


def test_zero_diff_retries_never_escape_api(worker, monkeypatch, tmp_path):
    orch, task = worker
    (tmp_path / "app.py").write_text("answer = 0\n", encoding="utf-8")
    task.target_files = ["app.py"]
    invoke = Mock(return_value=("No applicable patch provided", None))
    monkeypatch.setattr(orch, "_invoke_single_backend", invoke)
    assert orch._execute_task_with_retry(task) is False
    assert invoke.call_count == task.max_retries + 1
    assert {call.args[0] for call in invoke.call_args_list} == {"api"}
    assert task.status == "failed"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "answer = 0\n"


def test_api_transport_completion_applies_entire_patch(worker, completion_config, monkeypatch, tmp_path):
    orch, task = worker
    (tmp_path / "app.py").write_text("answer = 0\n", encoding="utf-8")
    task.target_files = ["app.py"]
    patch = "app.py\n<<<<<<< SEARCH\nanswer = 0\n=======\nanswer = 42\n>>>>>>> REPLACE\n"
    # Exercise the actual completion adapter; intercept only the HTTP transport.
    response = Mock()
    response.read.return_value = json.dumps({"choices": [{"message": {"content": patch}}]}).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    transport = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", transport)
    assert orch._execute_task_with_retry(task) is True
    assert task.patched_files == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "answer = 42\n"
    request = transport.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["model"] == "qwen-test"
    assert "repair app.py" in payload["messages"][-1]["content"]


def test_resume_fingerprint_mismatch_logs_and_reexecutes(worker, tmp_path, monkeypatch, caplog):
    from visual_agent.managed_checkpoint import ManagedCheckpoint, save_managed_checkpoint

    orch, task = worker
    followup = SubagentTask(id="inspect", title="inspect", role="indexer", backend="local",
                            instructions="inspect", dependencies=[task.id])
    orch.plan.tasks.append(followup)
    task.target_files = ["app.py"]
    (tmp_path / "app.py").write_text("answer = 0\n", encoding="utf-8")
    checkpoint = ManagedCheckpoint(
        checkpoint_id="mismatch", goal=orch.plan.goal, workspace_root=str(tmp_path),
        status="in_progress", current_wave_index=0, total_waves=2,
        completed_task_ids=[task.id], pending_task_ids=[followup.id], patched_files=["app.py"],
        task_states={task.id: {"status": "completed", "patched_files": ["app.py"],
                              "file_fingerprints": {"app.py": {"size": 999}}}},
    )
    save_managed_checkpoint(tmp_path, checkpoint)
    resumed = SubagentOrchestrator(orch.plan, workspace_root=tmp_path, resume=True)
    patch = "app.py\n<<<<<<< SEARCH\nanswer = 0\n=======\nanswer = 42\n>>>>>>> REPLACE\n"
    dispatch = Mock(side_effect=lambda current: (patch if current.id == task.id else "", None))
    monkeypatch.setattr(resumed, "_dispatch_to_backend", dispatch)
    assert resumed.run()["success"] is True
    assert [call.args[0].id for call in dispatch.call_args_list] == [task.id, followup.id]
    assert "failed fingerprint verification" in caplog.text
