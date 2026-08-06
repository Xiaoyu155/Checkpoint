from __future__ import annotations

from visual_agent.chief_dispatch import build_worker_prompt, chief_dispatch_to_markdown
from visual_agent.worker_report import (
    audit_declared_scope,
    audit_evidence_claims,
    collect_worker_objections,
    objection_markdown_lines,
    parse_worker_report,
)


NL = "\n"


def _completed(stdout: str, *, attempt: str = "primary") -> dict[str, object]:
    return {"status": "completed", "attempt": attempt, "agent": "codex", "stdout_tail": stdout}


BLOCK = (
    "PACER-REPORT\n"
    "objection: spec_contradiction\n"
    "summary: 骨架屏和失败横幅不能同时占同一个位置\n"
    "detail: 规格要求请求在飞时显示骨架屏\n"
    "detail: 又要求同一容器里显示失败横幅\n"
    "END-PACER-REPORT\n"
)


def test_worker_prompt_demands_a_report_even_when_there_is_no_objection() -> None:
    prompt = build_worker_prompt(
        plan={"objective": "x", "plan_id": "p1"},
        track={"id": "t1", "agent": "codex"},
        worktree="/tmp/wt",
        verification_command="pytest",
    )

    assert "PACER-REPORT" in prompt
    assert "even when you have nothing to object to" in prompt


def test_parse_reads_objection_summary_and_multiline_detail() -> None:
    report = parse_worker_report("blah blah\n" + BLOCK + "done.\n")

    assert report["present"] is True
    assert report["objection"] == "spec_contradiction"
    assert "骨架屏" in report["summary"]
    assert report["detail"].count("\n") == 1


def test_parse_ignores_the_prompt_template_echoed_back() -> None:
    echoed = (
        "PACER-REPORT\n"
        "objection: none | spec_contradiction | cannot_comply | better_approach\n"
        "END-PACER-REPORT\n"
    )

    assert parse_worker_report(echoed)["objection"] == ""


def test_parse_keeps_the_last_block_when_a_repair_round_appends_one() -> None:
    text = BLOCK + "\nrepair round\n" + "PACER-REPORT\nobjection: none\nsummary: 改完了\nEND-PACER-REPORT\n"

    assert parse_worker_report(text)["objection"] == "none"


def test_parse_survives_a_block_truncated_by_the_log_tail() -> None:
    truncated = "PACER-REPORT\nobjection: cannot_comply\nsummary: 缺少上游凭证"

    report = parse_worker_report(truncated)

    assert report["present"] is True
    assert report["objection"] == "cannot_comply"


def test_missing_report_is_reported_as_missing_not_as_agreement() -> None:
    summary = collect_worker_objections([_completed("all done, tests pass")])

    assert summary["objections"] == []
    # Silence is not consent: claiming "the worker had no objection" without a
    # report would be exactly the unevidenced statement Pacer must not make.
    assert summary["silent_attempts"] == ["primary"]


def test_a_crashed_worker_is_not_counted_as_silent() -> None:
    summary = collect_worker_objections([{"status": "crashed", "attempt": "primary", "stdout_tail": ""}])

    assert summary["silent_attempts"] == []
    assert summary["reports_present"] == 0


def test_objection_reaches_the_dispatch_markdown() -> None:
    summary = collect_worker_objections([_completed(BLOCK)])

    assert summary["objections"][0]["kind"] == "spec_contradiction"

    markdown = chief_dispatch_to_markdown({"status": "verified", "worker_objections": summary})

    assert "编程助手的异议" in markdown
    assert "需求自相矛盾" in markdown
    assert "骨架屏" in markdown


def test_no_objection_and_no_silence_renders_nothing() -> None:
    summary = collect_worker_objections([_completed("PACER-REPORT\nobjection: none\nEND-PACER-REPORT")])

    assert objection_markdown_lines(summary) == []


def test_declared_scope_matches_when_the_worker_stayed_where_it_said() -> None:
    audit = audit_declared_scope(
        declared=["src/payment/checkout.py", "src/payment/"],
        changed=["src/payment/checkout.py", "src/payment/retry.py"],
        reported=True,
    )

    assert audit["status"] == "matched"
    assert audit["undeclared"] == []


def test_undeclared_change_is_flagged_but_never_rejected() -> None:
    audit = audit_declared_scope(
        declared=["src/payment/checkout.py"],
        changed=["src/payment/checkout.py", "src/auth/session.py"],
        reported=True,
    )

    assert audit["status"] == "undeclared_changes"
    assert audit["undeclared"] == ["src/auth/session.py"]
    # The audit reports; it does not decide the change was wrong.
    assert "reverted" not in str(audit)


def test_a_worker_that_declared_nothing_is_distinguished_from_one_that_stayed_in_scope() -> None:
    audit = audit_declared_scope(declared=[], changed=["src/a.py"], reported=False)

    assert audit["status"] == "not_declared"


def test_scope_lines_survive_backticks_windows_slashes_and_comma_lists() -> None:
    report = parse_worker_report(
        "PACER-REPORT\n"
        "objection: none\n"
        "scope: `src\\payment\\checkout.py`\n"
        "scope: src/auth/session.py, src/auth/token.py\n"
        "scope: <the files you changed>\n"
        "END-PACER-REPORT\n"
    )

    assert report["scope"] == [
        "src/payment/checkout.py",
        "src/auth/session.py",
        "src/auth/token.py",
    ]


def test_undeclared_scope_reaches_the_markdown() -> None:
    markdown = chief_dispatch_to_markdown(
        {
            "status": "verified",
            "declared_scope_audit": audit_declared_scope(
                declared=["src/payment/"], changed=["src/auth/session.py"], reported=True
            ),
        }
    )

    assert "改动面超出了助手自己的声明" in markdown
    assert "src/auth/session.py" in markdown


def _claims(text: str):
    return parse_worker_report("PACER-REPORT" + NL + "objection: none" + NL + text + NL + "END-PACER-REPORT")["claims"]


def test_a_local_citation_is_verified_when_the_quoted_text_is_really_there(tmp_path) -> None:
    source = tmp_path / "site-packages" / "starlette" / "background.py"
    source.parent.mkdir(parents=True)
    source.write_text("async def __call__(self) -> None:" + NL + "    for task in self.tasks:" + NL, encoding="utf-8")

    audit = audit_evidence_claims(
        [{"claim": "BackgroundTasks run after the response", "evidence": "local:site-packages/starlette/background.py#for task in self.tasks"}],
        repo_root=tmp_path,
    )

    assert audit["claims"][0]["grade"] == "verified"


def test_a_citation_to_a_file_that_does_not_exist_is_rejected(tmp_path) -> None:
    audit = audit_evidence_claims(
        [{"claim": "X behaves like Y", "evidence": "local:site-packages/ghost/module.py#def whatever"}],
        repo_root=tmp_path,
    )

    assert audit["claims"][0]["grade"] == "rejected"
    assert "不存在" in audit["claims"][0]["reason"]


def test_a_real_file_that_does_not_contain_the_quoted_text_is_rejected(tmp_path) -> None:
    source = tmp_path / "lib.py"
    source.write_text("def unrelated(): pass" + NL, encoding="utf-8")

    audit = audit_evidence_claims(
        [{"claim": "lib retries twice", "evidence": "local:lib.py#max_retries = 2"}],
        repo_root=tmp_path,
    )

    assert audit["claims"][0]["grade"] == "rejected"


def test_an_upstream_link_to_a_moving_branch_is_not_a_citation() -> None:
    audit = audit_evidence_claims(
        [{"claim": "requests closes the socket", "evidence": "upstream:https://github.com/psf/requests@main"}]
    )

    assert audit["claims"][0]["grade"] == "rejected"
    assert "漂移" in audit["claims"][0]["reason"]


def test_a_pinned_upstream_link_is_recorded_as_unverified_not_verified() -> None:
    audit = audit_evidence_claims(
        [{"claim": "requests closes the socket", "evidence": "upstream:https://github.com/psf/requests@v2.31.0"}]
    )

    # Pacer did not fetch it, so calling it verified would be the same
    # unevidenced "passed" the acceptance work already ruled out.
    assert audit["claims"][0]["grade"] == "unverified"


def test_a_probe_without_observed_output_is_rejected() -> None:
    audit = audit_evidence_claims([{"claim": "the flag defaults to on", "evidence": "probe:python -c 'import x'"}])

    assert audit["claims"][0]["grade"] == "rejected"


def test_a_claim_with_no_evidence_line_is_rejected() -> None:
    audit = audit_evidence_claims([{"claim": "the API returns 204 on success", "evidence": ""}])

    assert audit["claims"][0]["grade"] == "rejected"
    assert "没有给出处" in audit["claims"][0]["reason"]


def test_claims_pair_with_the_evidence_line_that_follows_them() -> None:
    claims = _claims("claim: A is true" + NL + "evidence: local:a.py#x" + NL + "claim: B is true" + NL + "evidence: local:b.py#y")

    assert [item["claim"] for item in claims] == ["A is true", "B is true"]
    assert claims[1]["evidence"] == "local:b.py#y"


def test_the_evidence_ledger_reaches_the_markdown(tmp_path) -> None:
    audit = audit_evidence_claims(
        [{"claim": "ghost claim", "evidence": "local:nope.py#x"}], repo_root=tmp_path
    )

    markdown = chief_dispatch_to_markdown({"status": "verified", "evidence_ledger": audit})

    assert "外部行为主张的出处" in markdown
    assert "ghost claim" in markdown
    assert "出处不实" in markdown


def test_the_echoed_claim_template_is_not_treated_as_a_real_claim() -> None:
    report = parse_worker_report(
        "PACER-REPORT" + NL
        + "objection: none" + NL
        + "claim: <a statement you made about how some external code behaves>" + NL
        + "evidence: <where you checked it; one 'evidence:' line after each 'claim:'>" + NL
        + "END-PACER-REPORT"
    )

    assert report["claims"] == []


def test_a_claim_containing_a_pipe_is_still_a_real_claim(tmp_path) -> None:
    report = parse_worker_report(
        "PACER-REPORT" + NL
        + "objection: none" + NL
        + "claim: the endpoint returns 200 | 204" + NL
        + "evidence: local:api.py#return 204" + NL
        + "END-PACER-REPORT"
    )

    assert report["claims"][0]["claim"] == "the endpoint returns 200 | 204"


def test_a_dotfile_directory_declaration_keeps_its_leading_dot() -> None:
    report = parse_worker_report(
        "PACER-REPORT" + NL + "objection: none" + NL + "scope: ./.github/workflows/ci.yml" + NL + "END-PACER-REPORT"
    )

    assert report["scope"] == [".github/workflows/ci.yml"]


def test_the_user_facing_run_report_shows_the_objection_not_just_the_dispatch_payload() -> None:
    from visual_agent.chief_run import chief_run_to_markdown

    report = chief_run_to_markdown(
        {
            "status": "verified",
            "stop_reason": "verified",
            "mission": {"mission_id": "m1", "objective": "改登录页", "plan_id": "p1"},
            "plan": {"status": "ready"},
            "rounds": [],
            "dispatch": {
                "status": "verified",
                "worker_objections": collect_worker_objections([_completed(BLOCK)]),
            },
        }
    )

    assert "要你拍板的" in report
    assert "需求自相矛盾" in report
    assert "骨架屏" in report


def test_the_run_report_stays_quiet_when_there_is_nothing_to_decide() -> None:
    from visual_agent.chief_run import chief_run_to_markdown

    report = chief_run_to_markdown(
        {
            "status": "verified",
            "stop_reason": "verified",
            "mission": {"mission_id": "m1", "objective": "改登录页", "plan_id": "p1"},
            "plan": {"status": "ready"},
            "rounds": [],
            "dispatch": {"status": "verified"},
        }
    )

    assert "要你拍板的" not in report


def test_the_goal_itself_names_the_change_surface_when_the_worker_stays_silent() -> None:
    from visual_agent.worker_report import infer_declared_scope

    # 实测：claude-code 的报告交回率是 0/17。目标里写着路径，答案就在那儿，
    # 根本不需要它配合。
    scope = infer_declared_scope(
        "把 frontend/src/pages/tools/reminders.vue 的空态改成使用共享组件 EmptyState，"
        "配 /static/illustrations/brand-empty-reminders.png 插画。"
    )

    assert "src/pages/tools/reminders.vue" in scope


def test_a_silent_worker_is_still_held_to_the_goal_s_named_files() -> None:
    audit = audit_declared_scope(
        declared=["src/pages/tools/reminders.vue"],
        changed=["src/pages/tools/reminders.vue", "src/services/payments.js"],
        reported=False,
    )

    assert audit["status"] == "undeclared_changes"
    assert audit["undeclared"] == ["src/services/payments.js"]
    assert audit["declared_by"] == "goal"


def test_a_worker_declaration_still_wins_when_it_exists() -> None:
    audit = audit_declared_scope(
        declared=["src/a.py"], changed=["src/a.py"], reported=True
    )

    assert audit["declared_by"] == "worker"


def test_a_goal_that_names_no_file_is_honestly_undeclarable() -> None:
    from visual_agent.worker_report import infer_declared_scope

    assert infer_declared_scope("把界面做得好看一点") == []
    assert audit_declared_scope(declared=[], changed=["src/a.py"], reported=False)["status"] == "not_declared"
