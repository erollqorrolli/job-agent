import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("JOBAGENT_DB", DATA / "jobs.db"))
SHOTS = DATA / "screenshots"
SHOTS.mkdir(exist_ok=True)


def load_config(path: Path | None = None) -> dict:
    p = path or ROOT / "config.yaml"
    cfg = yaml.safe_load(open(p)) or {}
    # settings changed from the dashboard are layered on top (see db.kv)
    return cfg
