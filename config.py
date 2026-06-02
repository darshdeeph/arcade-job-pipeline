import os

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}\n"
            "Copy .env.example to .env and fill it in."
        )
    return val


ARCADE_API_KEY = _require("ARCADE_API_KEY")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
GMAIL_USER_ID = _require("GMAIL_USER_ID")

ARCADE_BASE_URL = os.environ.get("ARCADE_BASE_URL", "https://api.arcade.dev")
DAYS_TO_SCAN = int(os.environ.get("DAYS_TO_SCAN", "30"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))

# Optional — if blank, sheets_sync.py will create a new spreadsheet on first sync
# and print the ID to save here.
SHEETS_SPREADSHEET_ID = os.environ.get("SHEETS_SPREADSHEET_ID", "")

# Stage ordering — higher index = further along the pipeline
STAGES = ["Applied", "Interviewing", "Ended"]
STAGE_ORDER: dict[str, int] = {s: i for i, s in enumerate(STAGES)}

# Arcade tool names — confirmed from docs
ARCADE_TOOL_GMAIL_SEARCH = "Gmail.SearchThreads"
ARCADE_TOOL_GMAIL_GET_THREAD = "Gmail.GetThread"
