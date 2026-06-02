import logging

from arcade_client import ArcadeClient
from config import (
    ARCADE_TOOL_GMAIL_GET_THREAD,
    ARCADE_TOOL_GMAIL_SEARCH,
    DAYS_TO_SCAN,
)

logger = logging.getLogger(__name__)


def _date_range_for_days(days: int) -> str:
    if days <= 1:
        return "today"
    if days <= 7:
        return "last_7_days"
    if days <= 30:
        return "last_30_days"
    if days <= 31:
        return "this_month"
    return "this_year"


SUBJECT_SEARCHES = [
    "interview",
    "application",
    "recruiter",
    "offer letter",
    "phone screen",
    "take home",
    "coding challenge",
    "next steps",
    "rejection",
    "we regret",
    "job opportunity",
    "good news",
]


class GmailScanner:
    def __init__(self) -> None:
        self._arcade = ArcadeClient()

    async def scan_emails(self) -> list[dict]:
        """Search Gmail for job-related threads; deduplicated by thread ID."""
        date_range = _date_range_for_days(DAYS_TO_SCAN)
        seen: set[str] = set()
        threads: list[dict] = []

        for keyword in SUBJECT_SEARCHES:
            logger.info("Searching Gmail subject: %r date_range=%s", keyword, date_range)
            try:
                data = await self._arcade.execute(
                    ARCADE_TOOL_GMAIL_SEARCH,
                    {
                        "subject": keyword,
                        "date_range": date_range,
                        "max_results": 50,
                        "exclude_automated": False,
                    },
                )
            except RuntimeError as e:
                logger.error("Search failed for %r: %s", keyword, e)
                continue

            for thread in data.get("threads", data.get("results", [])):
                thread_id = thread.get("thread_id") or thread.get("id") or ""
                if not thread_id or thread_id in seen:
                    continue
                seen.add(thread_id)
                threads.append({
                    "thread_id": thread_id,
                    "subject": thread.get("subject") or "",
                    "sender": thread.get("sender") or thread.get("from") or "",
                    "date": thread.get("date") or "",
                    "body": thread.get("snippet") or thread.get("body") or "",
                })

        logger.info("Found %d unique job-related threads", len(threads))
        return threads

    async def get_thread_body(self, thread_id: str) -> str:
        """Fetch full message body for a thread."""
        try:
            data = await self._arcade.execute(ARCADE_TOOL_GMAIL_GET_THREAD, {"thread_id": thread_id})
        except RuntimeError as e:
            logger.warning("Could not fetch thread %s: %s", thread_id, e)
            return ""

        messages = data.get("messages") or []
        parts: list[str] = []
        for msg in messages[:5]:
            body = msg.get("body") or msg.get("snippet") or msg.get("text") or ""
            if body:
                parts.append(body)
        return "\n\n---\n\n".join(parts)
