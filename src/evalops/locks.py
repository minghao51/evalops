"""Lock files are per environment: the same git source maps to different backend versions per env."""
import json


def path(cfg, kind: str):
    d = cfg.lock_dir / "locks" / cfg.env
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{kind}.lock.json"


def read(cfg, kind: str) -> dict:
    p = path(cfg, kind)
    return json.loads(p.read_text()) if p.exists() else {}


def write(cfg, kind: str, data: dict) -> None:
    path(cfg, kind).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def scratch(cfg, name: str):
    """Generated, git-ignored outputs (last_eval.json, reports)."""
    d = cfg.lock_dir / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d / name
