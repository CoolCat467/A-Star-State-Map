import state_map


def test_has_run() -> None:
    assert hasattr(state_map, "run")
    assert callable(state_map.run)
