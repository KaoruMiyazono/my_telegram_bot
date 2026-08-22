# Legacy lifecycle tests

This directory is retained for historical reference. It targets a removed
`src.lifecycle` prototype and does not exercise the production lifecycle under
`agent.lifecycle`. The unified pytest entry explicitly quarantines it instead
of creating a fake compatibility package.

Production lifecycle coverage lives in `tests/test_plugins.py`,
`tests/test_before_turn.py`, `tests/test_before_reasoning.py`,
`tests/test_after_phases.py`, and `tests/test_pipeline_integration.py`.
