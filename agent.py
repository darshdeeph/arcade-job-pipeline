"""Job Search Pipeline Agent — CLI entrypoint.

Commands:
  sync    Scan Gmail → summarize threads → classify per company → update Sheets → chart
  status  Show current Sheets pipeline state + action items
  chart   Print history table and QuickChart.io URL
"""

import argparse
import asyncio
import collections
import logging
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import cache
import chart as chart_mod
from classifier import classify_company, summarize_thread
from config import CONCURRENCY
from gmail_scanner import GmailScanner
from sheets_sync import SheetsSync
from utils import IRRELEVANT_SENTINEL, normalize_company

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent")


def _parse_date(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        pass
    for fmt, length in [("%Y-%m-%d", 10), ("%Y/%m/%d", 10), ("%d %b %Y", 11)]:
        try:
            return datetime.strptime(raw.strip()[:length], fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _print_action_items(items: list[dict]) -> None:
    if items:
        for i, item in enumerate(items, 1):
            print(f"  {i}. [{item['company']}] {item['action']}")
    else:
        print("  No pending actions.")


# ------------------------------------------------------------------
# sync
# ------------------------------------------------------------------

async def cmd_sync(_args) -> int:
    print("\n╔══════════════════════════════════════╗")
    print("║   Job Search Pipeline Agent — sync   ║")
    print("╚══════════════════════════════════════╝\n")

    await asyncio.to_thread(cache.init)

    scanner = GmailScanner()
    sheets = SheetsSync()
    await sheets.ensure_spreadsheet()

    # --- Pass 0: Gmail scan ---
    print("Scanning Gmail for job-related emails...")
    threads = await scanner.scan_emails()

    if not threads:
        print("No job-related email threads found.\n")
        print("Tips: adjust DAYS_TO_SCAN in .env, or check the subject keywords in gmail_scanner.py")
        return 0

    print(f"Found {len(threads)} thread(s).\n")

    # --- Pass 1: Summarize threads (concurrent — cache miss → fetch body + LLM) ---
    print("── Pass 1: Summarizing threads ──────────────────")
    summaries_by_company: dict[str, list] = collections.defaultdict(list)
    cache_hits = 0
    cache_misses = 0
    _sem = asyncio.Semaphore(CONCURRENCY)
    _print_lock = asyncio.Lock()

    async def _process_thread(i: int, thread: dict):
        from cache import ThreadSummary
        thread_id = thread["thread_id"]
        subject = thread.get("subject") or "(no subject)"
        prefix = f"[{i:>3}/{len(threads)}] {subject[:70]!r}"

        cached = await asyncio.to_thread(cache.get, thread_id)
        if cached:
            async with _print_lock:
                print(f"{prefix}  [cache hit]")
            return "hit", cached

        async with _sem:
            body = thread.get("body") or ""
            if len(body) < 300:
                body = await scanner.get_thread_body(thread_id) or body

            date_str = _parse_date(thread.get("date") or "")
            summary = await summarize_thread(
                thread_id=thread_id,
                subject=subject,
                sender=thread.get("sender") or "",
                date=date_str,
                body=body,
            )

            if summary is None:
                async with _print_lock:
                    print(f"{prefix}\n          ↳ not job-related or error — skipping")
                await asyncio.to_thread(
                    cache.put,
                    ThreadSummary(thread_id=thread_id, company_name="", summary=IRRELEVANT_SENTINEL, date=date_str),
                )
                return "miss", None

            async with _print_lock:
                print(f"{prefix}\n          ↳ [{summary.company_name}] {summary.summary}")
            await asyncio.to_thread(cache.put, summary)
            return "miss", summary

    results = await asyncio.gather(*[_process_thread(i, t) for i, t in enumerate(threads, 1)])

    for kind, result in results:
        if kind == "hit":
            cache_hits += 1
            if result and result.company_name:
                summaries_by_company[normalize_company(result.company_name)].append(result)
        else:
            cache_misses += 1
            if result:
                summaries_by_company[normalize_company(result.company_name)].append(result)

    print(f"\n  Cache: {cache_hits} hits, {cache_misses} misses\n")

    # --- Pass 2: Classify per company ---
    print("── Pass 2: Classifying companies ────────────────")
    await sheets.load_existing_entries()
    counters: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    # Fold in historical threads not captured in today's scan
    all_cached = await asyncio.to_thread(cache.get_all)
    for s in all_cached:
        if s.company_name and s.summary != IRRELEVANT_SENTINEL:
            norm = normalize_company(s.company_name)
            company_threads = {t.thread_id for t in summaries_by_company[norm]}
            if s.thread_id not in company_threads:
                summaries_by_company[norm].append(s)

    async def _process_company(norm_key: str, company_summaries: list):
        if not norm_key:
            return None

        sorted_summaries = sorted(company_summaries, key=lambda s: s.date or "")
        display_name = max(
            (s.company_name for s in sorted_summaries if s.company_name),
            key=lambda n: sum(1 for s in sorted_summaries if s.company_name == n),
            default=norm_key,
        )

        async with _sem:
            classification = await classify_company(display_name, company_summaries)

        if classification is None:
            async with _print_lock:
                print(f"  {display_name}  ({len(company_summaries)} thread(s))\n    ↳ classification error")
            return "error"

        flags = []
        if classification.sub_status:
            flags.append(classification.sub_status)
        if classification.action_required:
            flags.append("⚡ ACTION REQUIRED")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""

        latest_date = max((s.date for s in company_summaries if s.date), default="")
        sheet_result = await sheets.upsert_entry(
            thread_id=sorted_summaries[-1].thread_id,
            classification=classification,
            date_str=latest_date or _parse_date(""),
            subject=display_name,
        )

        icon = {"created": "✚", "updated": "↑", "skipped": "–", "error": "✗"}.get(sheet_result, "?")
        async with _print_lock:
            print(
                f"  {display_name}  ({len(company_summaries)} thread(s))\n"
                f"    ↳ {classification.stage}{flag_str}\n"
                f"    ↳ Sheets: {icon} {sheet_result}"
            )
        return sheet_result

    company_results = await asyncio.gather(*[
        _process_company(k, v) for k, v in sorted(summaries_by_company.items())
    ])

    for result in company_results:
        if result:
            counters[result] += 1

    # --- Summary ---
    print(f"\n=== Sync Summary ===")
    print(f"  Companies processed: {len(summaries_by_company)}")
    print(f"  Created in Sheets:  {counters['created']:>4}")
    print(f"  Updated in Sheets:  {counters['updated']:>4}")
    print(f"  Unchanged:          {counters['skipped']:>4}")
    print(f"  Errors:             {counters['error']:>4}")

    # --- Action Required ---
    print("\n=== Action Required ===")
    _print_action_items(sheets.get_action_required_items())

    # --- Chart ---
    history = chart_mod.build_timeline()
    print()
    chart_mod.print_chart_to_terminal(history)

    chart_url = await chart_mod.generate_chart_url(history)
    if chart_url:
        print(f"\nChart PNG:\n  {chart_url}\n")

    return 0


# ------------------------------------------------------------------
# status / chart
# ------------------------------------------------------------------

async def cmd_status(_args) -> int:
    print("\n=== Pipeline Status ===\n")
    await asyncio.to_thread(cache.init)
    sheets = SheetsSync()
    await sheets.ensure_spreadsheet()
    await sheets.load_existing_entries()

    counts = sheets.get_stage_counts()
    total = sum(counts.values())
    print(f"  Applied:      {counts['Applied']:>4}")
    print(f"  Interviewing: {counts['Interviewing']:>4}")
    print(f"  Ended:        {counts['Ended']:>4}")
    print(f"  ─────────────────")
    print(f"  Total:        {total:>4}")

    print("\n=== Action Required ===")
    _print_action_items(sheets.get_action_required_items())

    cs = await asyncio.to_thread(cache.stats)
    print(f"\n  (Cache: {cs['cached_threads']} threads from {cs['companies']} companies)")
    return 0


async def cmd_chart(_args) -> int:
    await asyncio.to_thread(cache.init)
    history = chart_mod.build_timeline()
    if not history:
        print("No history yet. Run 'python agent.py sync' first.")
        return 1
    chart_mod.print_chart_to_terminal(history)
    chart_url = await chart_mod.generate_chart_url(history)
    if chart_url:
        print(f"\nChart PNG:\n  {chart_url}\n")
    return 0


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Job Search Pipeline Agent — Gmail + Google Sheets + local SQLite cache",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("sync", help="Scan Gmail, summarize threads, classify per company, update Sheets")
    sub.add_parser("status", help="Show current pipeline state + action items")
    sub.add_parser("chart", help="Print history table and QuickChart.io URL")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    commands = {"sync": cmd_sync, "status": cmd_status, "chart": cmd_chart}
    return asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
