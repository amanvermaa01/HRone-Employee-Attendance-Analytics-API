import runpy
from pathlib import Path

# Run root sample_seed.py when invoked from app/
root_seed = Path(__file__).resolve().parent.parent / "sample_seed.py"
runpy.run_path(str(root_seed), run_name="__main__")
