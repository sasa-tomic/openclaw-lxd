"""pytest configuration for the twitter test suite.

Pre-loads real lib modules so that test_target_monitor.py's sys.modules stubs
(which use setdefault) do not accidentally override them for other test files.
"""
import sys
from pathlib import Path

# Ensure /projects/automations is on the path so lib.* imports resolve
_root = str(Path(__file__).parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

# Pre-load real modules before test_target_monitor.py's module-level setdefault
# calls can stub them out.  test_target_monitor uses setdefault so this is safe
# to do here: whichever side loads first wins, and we want the real one.
import lib.llm_utils  # noqa: F401
import lib.config  # noqa: F401
