from visual_agent.diff_patcher import apply_search_replace_text


def test_unlisted_new_file_is_rejected(tmp_path):
    result = apply_search_replace_text(
        "extra.py\n<<<<<<< SEARCH\n=======\nvalue = 1\n>>>>>>> REPLACE\n",
        workspace_root=tmp_path, allowed_files=["planned.py"])
    assert not result.success
    assert not (tmp_path / "extra.py").exists()


def test_listed_new_file_is_created(tmp_path):
    result = apply_search_replace_text(
        "planned.py\n<<<<<<< SEARCH\n=======\nvalue = 1\n>>>>>>> REPLACE\n",
        workspace_root=tmp_path, allowed_files=["planned.py"])
    assert result.success
    assert (tmp_path / "planned.py").read_text().strip() == "value = 1"


def test_batch_failure_does_not_modify_first_file(tmp_path):
    first = tmp_path / "first.py"
    first.write_text("value = 1\n")
    patch = "first.py\n<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE\n"
    patch += "outside.py\n<<<<<<< SEARCH\n=======\nvalue = 3\n>>>>>>> REPLACE\n"
    result = apply_search_replace_text(patch, workspace_root=tmp_path, allowed_files=["first.py"])
    assert not result.success
    assert first.read_text() == "value = 1\n"
    assert not (tmp_path / "outside.py").exists()
