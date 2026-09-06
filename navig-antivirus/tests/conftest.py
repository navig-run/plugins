import sys
from pathlib import Path

# navig_antivirus is editable-installed against the MAIN checkout; put THIS
# worktree's package first so tests exercise the code under test, not the install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
