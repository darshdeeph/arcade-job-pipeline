"""Chart generation from SQLite cache.

Builds three cumulative time-series lines directly from thread_cache.db:
  1. First Contact — date of each company's earliest email
  2. Technical Round — date a company's summary first mentions a technical event
  3. Rejected / Onsite — date a company's summary first mentions rejection or onsite
"""

import asyncio
import json
import logging
import urllib.parse
import urllib.request

import cache
from utils import IRRELEVANT_SENTINEL, normalize_company

logger = logging.getLogger(__name__)

_TECHNICAL_KEYWORDS = {"technical", "take-home", "takehome", "coding", "assessment", "leetcode", "hackerrank", "coderpad"}
_ONSITE_REJECTED_KEYWORDS = {"onsite", "on-site", "on site", "rejected", "rejection", "not moving forward", "not selected", "offer"}


def _classify_summary(summary: str) -> set[str]:
    """Return which milestone labels apply to this summary text."""
    low = summary.lower()
    labels = set()
    if any(kw in low for kw in _TECHNICAL_KEYWORDS):
        labels.add("technical")
    if any(kw in low for kw in _ONSITE_REJECTED_KEYWORDS):
        labels.add("onsite_rejected")
    return labels


def build_timeline() -> list[dict]:
    """Derive cumulative event counts from thread_cache.db.

    Returns a list of dicts sorted by date:
      {"date": "YYYY-MM-DD", "first_contact": int, "technical": int, "onsite_rejected": int}
    """
    summaries = cache.get_all()

    company_first: dict[str, str] = {}
    company_technical: dict[str, str] = {}
    company_onsite_rej: dict[str, str] = {}

    for s in summaries:
        if not s.company_name or s.summary == IRRELEVANT_SENTINEL or not s.date:
            continue

        key = normalize_company(s.company_name)
        if not key:
            continue

        date = s.date

        if key not in company_first or date < company_first[key]:
            company_first[key] = date

        milestones = _classify_summary(s.summary)
        if "technical" in milestones:
            if key not in company_technical or date < company_technical[key]:
                company_technical[key] = date
        if "onsite_rejected" in milestones:
            if key not in company_onsite_rej or date < company_onsite_rej[key]:
                company_onsite_rej[key] = date

    all_dates: set[str] = (
        set(company_first.values())
        | set(company_technical.values())
        | set(company_onsite_rej.values())
    )

    if not all_dates:
        return []

    return [
        {
            "date": date,
            "first_contact": sum(1 for d in company_first.values() if d <= date),
            "technical": sum(1 for d in company_technical.values() if d <= date),
            "onsite_rejected": sum(1 for d in company_onsite_rej.values() if d <= date),
        }
        for date in sorted(all_dates)
    ]


def print_chart_to_terminal(history: list[dict]) -> None:
    if not history:
        print("No timeline data yet. Run 'sync' to scan emails first.")
        return

    print("\n=== Pipeline Timeline (from email history) ===\n")
    print(f"{'Date':<14} {'First Contact':>14} {'Technical':>10} {'Onsite/Rejected':>16}")
    print("─" * 58)
    for h in history:
        print(f"{h['date']:<14} {h['first_contact']:>14} {h['technical']:>10} {h['onsite_rejected']:>16}")


def _build_chart_config(history: list[dict]) -> str:
    labels = [h["date"] for h in history]
    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "First Contact",
                    "data": [h["first_contact"] for h in history],
                    "borderColor": "#4F86C6",
                    "backgroundColor": "rgba(79,134,198,0.08)",
                    "fill": True,
                    "tension": 0.3,
                    "pointRadius": 4,
                },
                {
                    "label": "Technical Round",
                    "data": [h["technical"] for h in history],
                    "borderColor": "#F4B942",
                    "backgroundColor": "rgba(244,185,66,0.08)",
                    "fill": True,
                    "tension": 0.3,
                    "pointRadius": 4,
                },
                {
                    "label": "Onsite / Rejected",
                    "data": [h["onsite_rejected"] for h in history],
                    "borderColor": "#E05C5C",
                    "backgroundColor": "rgba(224,92,92,0.08)",
                    "fill": True,
                    "tension": 0.3,
                    "pointRadius": 4,
                },
            ],
        },
        "options": {
            "title": {"display": True, "text": "Job Search Pipeline — Cumulative Milestones", "fontSize": 16},
            "legend": {"display": True, "position": "bottom"},
            "scales": {
                "yAxes": [{"ticks": {"beginAtZero": True, "precision": 0}}],
                "xAxes": [{"ticks": {"maxRotation": 45}}],
            },
        },
    }
    return json.dumps(config, separators=(",", ":"))


async def generate_chart_url(history: list[dict]) -> str | None:
    """Build a QuickChart.io URL for the three-line cumulative timeline chart."""
    if not history:
        return None

    config_json = _build_chart_config(history)
    encoded = urllib.parse.quote(config_json)
    url = f"https://quickchart.io/chart?c={encoded}&width=900&height=450&format=png"

    if len(url) <= 8000:
        return url

    # Fallback: POST to get a short URL for large configs
    return await _quickchart_short_url(config_json)


async def _quickchart_short_url(config_json: str) -> str | None:
    payload = json.dumps({"chart": config_json, "width": 900, "height": 450}).encode()
    req = urllib.request.Request(
        "https://quickchart.io/chart/create",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    def _post() -> str | None:
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()).get("url")
        except Exception as e:
            logger.error("QuickChart POST failed: %s", e)
            return None

    return await asyncio.to_thread(_post)
