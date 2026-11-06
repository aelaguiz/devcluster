import sys
from pathlib import Path

# Let the tests import devcluster from this checkout without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
