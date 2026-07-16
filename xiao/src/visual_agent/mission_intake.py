from __future__ import annotations


def is_manual_verification_goal(goal: str) -> bool:
    """True for field/device validation work that cannot be reduced to pytest.

    These missions need a human acceptance plan rather than an automatic
    workflow gate. The core runner, dashboard, queue, and retry paths all use
    this predicate so coverage handling stays consistent across entry points.
    """
    text = str(goal or "").lower()
    return any(
        token in text
        for token in (
            "livekit",
            "真机",
            "手机",
            "弱网",
            "户外",
            "噪声",
            "语音通话",
            "通话",
            "人工验收",
            "现场",
            "adb",
            "ios",
            "android",
        )
    )


def is_review_plan_goal(goal: str) -> bool:
    """True when the deliverable is a review or development plan, not a diff."""
    text = str(goal or "").lower()
    if any(token in text for token in ("审核", "审查", "评估", "review", "audit")):
        return True
    if "开发计划" in text and any(token in text for token in ("生成", "给出", "制定", "输出", "产出")):
        return True
    if any(token in text for token in ("review plan", "development plan report", "plan report")):
        return True
    return False
