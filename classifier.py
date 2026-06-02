"""Two-pass LLM classification.

Pass 1 — summarize_thread():
    Called once per thread. Produces a compact, date-stamped summary of what
    happened (e.g. "Onsite confirmation for June 5, two rounds with engineering").
    Result is stored in SQLite by thread_id and never re-generated on subsequent runs.

Pass 2 — classify_company():
    Called once per company per run. Reads the full sorted timeline of thread
    summaries and decides: stage, sub_status, action_required, action_description.
"""

import json
import logging
from dataclasses import dataclass

import anthropic

from cache import ThreadSummary
from config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ------------------------------------------------------------------
# Output types
# ------------------------------------------------------------------

@dataclass
class EmailClassification:
    company_name: str = "Unknown"
    role: str = "Unknown"
    stage: str = "Applied"
    sub_status: str | None = None
    action_required: bool = False
    action_description: str = ""
    source: str = "Inbound"


# ------------------------------------------------------------------
# Pass 1: Summarize a single thread
# ------------------------------------------------------------------

_SUMMARIZE_PROMPT = """\
You are a job search assistant. Given a single email thread, produce a compact summary.

Respond with JSON only:
{
  "relevant": true/false,
  "company_name": "Exact company name",
  "summary": "1-2 sentence plain-English description of the key event in this thread",
  "date": "YYYY-MM-DD of the most recent message, or empty string if unclear"
}

Guidelines for summary:
- Be specific and factual. Capture the event type and any key dates.
- Good: "Onsite interview confirmed for June 5, two rounds with engineering and a PM."
- Good: "Recruiter reached out about a Senior SWE role, asked for availability for a 30-min call."
- Good: "Rejection email after final round."
- Good: "Take-home coding assignment sent, due June 10."
- Bad: "Email about interview" (too vague)
- The summary is used later to determine pipeline stage — include enough detail to make that judgment.

If the email is not about a specific job opportunity (newsletter, job board digest, generic marketing), respond with {"relevant": false}.
"""


async def summarize_thread(
    thread_id: str,
    subject: str,
    sender: str,
    date: str,
    body: str,
) -> ThreadSummary | None:
    """Summarize one email thread. Returns None if irrelevant or on error."""
    user_message = (
        f"Subject: {subject}\nFrom: {sender}\nDate: {date}\n\n"
        f"Body:\n{body[:4000]}"
    )

    try:
        response = await _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=_SUMMARIZE_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error("Anthropic error summarizing thread %s: %s", thread_id, e)
        return None

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON parse error for thread %s: %s\nRaw: %r", thread_id, e, raw[:200])
        return None

    if not data.get("relevant", True):
        return None

    return ThreadSummary(
        thread_id=thread_id,
        company_name=data.get("company_name") or "Unknown",
        summary=data.get("summary") or "",
        date=data.get("date") or date or "",
    )


# ------------------------------------------------------------------
# Pass 2: Classify a company from its full thread timeline
# ------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
You are a job search assistant. Given a chronological list of email summaries for a single company,
determine the current state of this job application.

Respond with JSON only:
{
  "role": "Job title if known, otherwise Unknown",
  "stage": "Applied" | "Interviewing" | "Ended",
  "sub_status": "Recruiter Screen" | "Technical" | "Onsite" | "Offer" | "Rejected" | "Ghosted" | "Withdrew" | null,
  "action_required": true | false,
  "action_description": "One sentence on what still needs to be done and by when, or empty string",
  "source": "Inbound" | "Outbound" | "Referral"
}

Stage: use the most recent and advanced event. Ignore earlier stages once the process has moved on.
- Applied: application submitted or first recruiter contact, nothing further yet
- Interviewing: any active process — phone screen, technical, onsite, take-home
- Ended: rejection, offer, withdrew, or ghosted

action_required — read the full timeline before deciding:
  TRUE only if there is something the candidate still needs to do RIGHT NOW:
    - A request for availability or scheduling that has NOT yet been followed by a confirmation
    - A take-home or work sample that has NOT yet been submitted
    - A form or document that has NOT yet been completed

  FALSE if:
    - The action was already completed (a later thread shows a confirmation, scheduling, or submission)
    - The email is a confirmation ("your interview is confirmed", "see you Thursday")
    - The next step just requires showing up
    - It's a rejection, offer letter, or informational update

  The key question: reading all threads in order, is there an open request that has NOT been resolved
  by a subsequent thread? If the recruiter asked for availability and a later thread confirms the
  interview is scheduled, the action is DONE — action_required = false.
"""


async def classify_company(
    company_name: str,
    summaries: list[ThreadSummary],
) -> EmailClassification | None:
    """Classify a company's pipeline state from its thread timeline."""
    if not summaries:
        return None

    sorted_summaries = sorted(summaries, key=lambda s: s.date or "")
    timeline = "\n".join(
        f"- [{s.date or 'unknown date'}] {s.summary}"
        for s in sorted_summaries
    )
    user_message = f"Company: {company_name}\n\nEmail timeline:\n{timeline}"

    try:
        response = await _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=_CLASSIFY_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error("Anthropic error classifying %s: %s", company_name, e)
        return None

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON parse error for %s: %s\nRaw: %r", company_name, e, raw[:200])
        return None

    return EmailClassification(
        company_name=company_name,
        role=data.get("role") or "Unknown",
        stage=data.get("stage") or "Applied",
        sub_status=data.get("sub_status"),
        action_required=bool(data.get("action_required", False)),
        action_description=data.get("action_description") or "",
        source=data.get("source") or "Inbound",
    )
