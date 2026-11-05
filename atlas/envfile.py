import os
from pathlib import Path


def load_dotenv(path=None):
    """Load KEY=VALUE lines into the environment without overriding existing keys.

    Missing files are ignored. Values are not logged. This does not enable live trading.
    """
    file_path = Path(path or os.getenv("ATLAS_ENV_FILE", ".env"))
    if not file_path.is_file():
        return []
    loaded = []
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
