from pathlib import Path

from visual_agent.fixtures import load_observation_fixture


def test_regression_r_20260523_214247_2ee3c16b():
    workspace_root = Path(__file__).resolve().parents[1]
    observation = load_observation_fixture(workspace_root / 'fixtures/regression/r_20260523_214247_2ee3c16b_observation.json')
    assert observation.elements
    assert observation.metadata.get('regression_source_run_id') == '20260523-214247-2ee3c16b'
    # Source workflow: failing_regression_demo
    # Failed step: assert_missing
