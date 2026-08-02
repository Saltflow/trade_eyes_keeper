from src.core.process_lock import exclusive_process_lock


def test_optimizer_process_lock_rejects_overlap(tmp_path):
    path = tmp_path / "optimizer.lock"

    with exclusive_process_lock(path) as first:
        with exclusive_process_lock(path) as second:
            assert first is True
            assert second is False

    with exclusive_process_lock(path) as reacquired:
        assert reacquired is True
