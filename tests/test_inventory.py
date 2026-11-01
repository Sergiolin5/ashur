

def test_scan_rests_after_reading_big_files_and_priority_is_restored(tmp_path, monkeypatch):
    from modules.inventory import action
    slept = []
    monkeypatch.setattr(action.time, "sleep", lambda s: slept.append(s))
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (3 << 20))
    assert action.fingerprint(str(big), big.stat().st_size).startswith("fp:")
    assert len(slept) <= 1 and all(s <= action.POLITE_PAUSE_MAX_S for s in slept)
    restore = action.lower_priority()
    if restore:
        restore()
