from visual_agent.wish_intake import HOST_MAX_STEPS, format_progress_frame, host_stop_reason


def test_progress_frames_name_the_task_steps_and_quota() -> None:
    frames = [
        format_progress_frame("task_2", "coder", "workbuddy", f"心跳 #{index} 写码中", 3 - index)
        for index in range(1, 4)
    ]
    assert len(frames) >= 3
    for frame in frames:
        assert "task_2" in frame
        assert "当前岗位: workbuddy" in frame
        assert "剩余托管步数:" in frame
        assert "配额剩余时间:" in frame


def test_host_stops_on_repeat_and_caps_steps() -> None:
    assert HOST_MAX_STEPS == 50
    assert host_stop_reason(0, 50, 1, "", "first failure") == ""
    assert host_stop_reason(1, 50, 1, "first failure", "first failure") == "同一失败重复出现"
    assert host_stop_reason(49, 50, 1, "a", "b") == "已达到本次托管步数"
