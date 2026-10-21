from pathlib import Path
from .core import run
raise SystemExit(run(Path(__file__).resolve().parent.parent))
