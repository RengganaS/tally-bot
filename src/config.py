import os
from dotenv import load_dotenv

load_dotenv()

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

DISCORD_BOT_TOKEN: str = _require_env("DISCORD_BOT_TOKEN")
ALLOWED_USER_ID: int = int(_require_env("ALLOWED_USER_ID"))
FINANCE_CHANNEL_ID: int = int(_require_env("FINANCE_CHANNEL_ID"))
DATABASE_URL: str = _require_env("DATABASE_URL")
GEMINI_API_KEY: str = _require_env("GEMINI_API_KEY")

