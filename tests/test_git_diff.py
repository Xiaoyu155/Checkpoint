from __future__ import annotations

from dataclasses import dataclass

from visual_agent.git_diff import affected_workflows, workflow_affects_changed_path


@dataclass(frozen=True)
class Ref:
    name: str
    affects: tuple[str, ...] = ()


def test_workflow_affects_changed_path_matches_directory_and_exact_file() -> None:
    changed = ["src/payment/checkout.py", "templates/cart.html"]

    assert workflow_affects_changed_path("src/payment/", changed) is True
    assert workflow_affects_changed_path("templates/cart.html", changed) is True
    assert workflow_affects_changed_path("src/profile/", changed) is False


def test_workflow_affects_changed_path_matches_glob() -> None:
    assert workflow_affects_changed_path("src/**/*.py", ["src/payment/checkout.py"]) is True


def test_affected_workflows_keeps_unscoped_and_matching_workflows() -> None:
    workflows = [
        Ref("always"),
        Ref("checkout", ("src/payment/",)),
        Ref("profile", ("src/profile/",)),
    ]

    selected = affected_workflows(workflows, changed=["src/payment/checkout.py"])

    assert [item.name for item in selected] == ["always", "checkout"]


def test_affected_workflows_returns_all_when_changed_files_unknown() -> None:
    workflows = [Ref("always"), Ref("checkout", ("src/payment/",))]

    assert affected_workflows(workflows, changed=[]) == workflows
