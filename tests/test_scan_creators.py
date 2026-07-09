"""
scripts/scan_creators.py — the scheduled Creator Signals entry point. Loaded by
path (scripts/ isn't a package); the scan itself is covered in
test_creator_signals.py, so here we just assert the wiring.
"""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "scan_creators.py"
_spec = importlib.util.spec_from_file_location("scan_creators_mod", _PATH)
scan_creators = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan_creators)


def test_main_configures_ensures_schema_and_scans():
    with patch.object(scan_creators, "configure") as cfg, \
         patch.object(scan_creators.creator_signals, "scan_creators",
                      return_value={"creators": 1, "new_videos": 0}) as scan:
        scan_creators.main()  # create_all runs against the in-memory test DB — harmless
    cfg.assert_called_once()
    scan.assert_called_once()
