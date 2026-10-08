"""Environment, paths and shared constants. Secrets come from .env or the process environment."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobs.sqlite"
PROFILE_PATH = DATA_DIR / "profile.txt"  # personal, gitignored
DESCRIPTION_CACHE = DATA_DIR / "descriptions.json"


def _load_dotenv() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        # On the server the file lives in the home directory of the bot user
        path = Path.home() / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


_DOTENV = _load_dotenv()


def get(name: str, default: str = "") -> str:
    return os.environ.get(name) or _DOTENV.get(name, default)


def telegram_proxy() -> str:
    """hh needs no proxy; Telegram does. socks5h makes DNS resolve through the proxy."""
    proxy = get("TELEGRAM_PROXY_URL")
    return proxy.replace("socks5://", "socks5h://", 1) if proxy.startswith("socks5://") else proxy
