import sys
from pathlib import Path

# agent.py / monitor.py are standalone deploy scripts (not a package) — put their
# dir on the path so `import agent` resolves to THIS checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
