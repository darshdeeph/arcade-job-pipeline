# Job Search Pipeline Agent

A CLI agent that scans your Gmail for job-search emails, builds a per-company timeline, classifies your pipeline stage with Claude, and syncs everything into a Google Sheet. Built on [Arcade.dev](https://api.arcade.dev) for Gmail and Sheets tool access.

## What It Does

1. **Scans Gmail** — searches recent emails for recruiter outreach, interview confirmations, rejections, and offers using Arcade's Gmail toolkit
2. **Summarizes threads** — each email thread is condensed into a one-sentence summary (e.g. "Onsite confirmed for June 5, two rounds with engineering") and cached locally in SQLite by thread ID
3. **Classifies per company** — all summaries for a company are fed to Claude in chronological order; Claude decides the current stage and whether any action is still outstanding
4. **Syncs to Google Sheets** — one row per company, updated in place; duplicate rows are cleaned up automatically
5. **Charts progress** — generates a time-series chart (Applied / Interviewing / Ended) via QuickChart.io

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Arcade MCP Gateway                       │
│                    (single federated endpoint)                  │
│                                                                 │
│  ┌──────────────┐          ┌───────────────────┐                │
│  │  Gmail MCP   │          │  Google Sheets MCP │                │
│  │  (pre-built) │          │  (pre-built)       │                │
│  └──────────────┘          └───────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
             ▲                        ▲
             └──────────┬─────────────┘
                        │ Arcade SDK tool calls
                        │
              ┌─────────┴──────────┐
              │   agent.py (CLI)   │
              │                    │
              │  Pass 1: summarize │──▶ SQLite cache (thread_cache.db)
              │  Pass 2: classify  │──▶ Claude Haiku (per company)
              │                    │
              │  gmail_scanner.py  │
              │  sheets_sync.py    │       ┌─────────────────┐
              │  classifier.py     │──────▶│  QuickChart.io  │
              │  cache.py          │       │  (PNG charts)   │
              │  chart.py          │       └─────────────────┘
              └────────────────────┘
```

## Prerequisites

- Python 3.11+
- [Arcade.dev](https://api.arcade.dev) account (free tier works)
- [Anthropic API key](https://console.anthropic.com)

## Setup

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd arcade-job-pipeline

# 2. Install dependencies
pip install python-dotenv arcade-ai anthropic

# 3. Configure environment
cp .env.example .env
# Fill in ARCADE_API_KEY and ANTHROPIC_API_KEY
# Leave SHEETS_SPREADSHEET_ID blank — the agent creates the sheet on first run

# 4. Run
python agent.py sync
```

On the first run, the agent will open a Google OAuth prompt in your browser (covers both Gmail and Sheets). After authorizing, it creates a "Job Pipeline" spreadsheet automatically and prints the ID to save in your `.env`.

## Usage

```bash
# Full sync: scan Gmail → summarize threads → classify per company → update Sheets → chart
python agent.py sync

# Show current pipeline state and action items (reads from Sheets, no Gmail scan)
python agent.py status

# Print history table and QuickChart.io URL
python agent.py chart
```

## Sample Output

```
╔══════════════════════════════════════╗
║   Job Search Pipeline Agent — sync   ║
╚══════════════════════════════════════╝

Scanning Gmail for job-related emails...
Found 24 thread(s).

── Pass 1: Summarizing threads ──────────────────
[  1/24] 'Onsite interview confirmed - Company A'  [cache hit]
[  2/24] 'Your Company B application'  [cache hit]
[  3/24] 'Follow-up from recruiter at Company C'
          ↳ [Company C] Recruiter asked for availability for a 30-min intro call.
...
  Cache: 21 hits, 3 misses

── Pass 2: Classifying companies ────────────────
  Company A  (7 thread(s))
    ↳ Interviewing  [Onsite]
    ↳ Sheets: ↑ updated
  Company B  (5 thread(s))
    ↳ Ended  [Rejected]
    ↳ Sheets: ↑ updated
  Company C  (2 thread(s))
    ↳ Interviewing  [Recruiter Screen, ⚡ ACTION REQUIRED]
    ↳ Sheets: ✚ created

=== Action Required ===
  1. [Company C] Reply to recruiter with availability for intro call

=== Pipeline History (ASCII) ===

Date           Applied  Interviewing   Ended
──────────────────────────────────────────────
2026-06-01           4             6       3
```

## Project Structure

```
arcade-job-pipeline/
├── agent.py             # CLI entrypoint — orchestrates both passes
├── classifier.py        # Pass 1: thread summarization / Pass 2: company classification
├── gmail_scanner.py     # Arcade Gmail toolkit wrapper
├── sheets_sync.py       # Arcade Google Sheets wrapper — one row per company
├── cache.py             # SQLite cache — thread_id → summary
├── chart.py             # QuickChart.io time-series chart
├── config.py            # Env vars and Arcade tool name constants
├── pyproject.toml
├── .env.example
├── thread_cache.db      # Created on first sync
└── pipeline_history.json
```

## Decisions Made

**Two-pass architecture**
A single LLM call per thread is not enough to determine pipeline state — multiple threads from the same company need to be read together. The agent separates concerns: Pass 1 summarizes each thread in isolation (one sentence, cacheable), and Pass 2 reads the full chronological timeline per company and makes a single judgment about stage and action. This mirrors how a human would review their inbox: skim individual emails first, then think about where things stand with each company.

**SQLite caching by thread ID**
Gmail threads don't change once they're summarized. Caching the per-thread summary in SQLite means re-runs skip the Gmail body fetch and the Anthropic summarization call entirely for threads already seen. On a real inbox with months of history, this is the difference between a 2-minute run and a 20-minute one.

**Company name normalization**
The summarizer LLM doesn't always spell company names consistently — "Ramp", "Ramp Financial", and "Ramp Financial Corp" would otherwise create three separate rows. All grouping operations (building the per-company timeline, deduplicating sheet rows) use a normalized key: lowercased with legal suffixes stripped. The original spelling is preserved for display by picking the most common variant across all summaries.

**Google Sheets instead of Notion**
The original plan used Notion. After checking Arcade's Notion toolkit, it only supports page-level operations — there is no `QueryDatabase`, no `UpdatePage`, and no way to read existing records back to deduplicate. Google Sheets has `GetSpreadsheet` and `UpdateCells` which provide the full read/write access needed. Sheets also auto-creates on first run via `CreateSpreadsheet`, so there's no manual setup required.

**asyncio instead of threads**
Both passes are I/O-bound — the bottleneck is waiting on Anthropic API responses and Arcade tool calls, not CPU. Using `asyncio` with `asyncio.gather()` lets all thread summarizations (Pass 1) and all company classifications (Pass 2) run concurrently in a single thread, with no thread-safety complexity beyond one `asyncio.Lock` guarding the Sheets row counter for new entries. An `asyncio.Semaphore` (configurable via `CONCURRENCY` in `.env`, default 4) caps how many API calls are in-flight at once to stay within rate limits. The Anthropic and Arcade SDKs both have native async clients (`AsyncAnthropic`, `AsyncArcade`), so no thread pool is needed. Synchronous SQLite calls (`cache.py`) are dispatched via `asyncio.to_thread` to avoid blocking the event loop.

**Shared Arcade wrapper extracted to `arcade_client.py`**
Both `GmailScanner` and `SheetsSync` make Arcade tool calls with identical auth and error-handling logic. Rather than duplicating `_authorize`/`_execute` across two classes, a single `ArcadeClient` owns that logic and is composed into each class. Any change to how Arcade calls are made (retry logic, error formatting, auth flow) has one place to land.

**`utils.py` for shared primitives**
`normalize_company` is used in `sheets_sync.py`, `agent.py`, and `chart.py`. `IRRELEVANT_SENTINEL` (the marker for non-job-related threads in the SQLite cache) is written in `agent.py` and read in `chart.py`. Both belong in a shared module rather than being a private function imported from a sibling or a magic string scattered across files.

## What I'd Do Differently

**Broader Gmail search coverage**
The agent searches by subject keyword (interview, recruiter, offer, etc.). This misses threads where the job-related context is only in the email body — follow-up threads often have subjects like "Re: Quick question" with no obvious signal. A body-keyword search pass would catch more of the pipeline.

**Ghosting detection**
Detecting ghosting (no response after a certain number of days) requires knowing when the candidate last sent an email, which the current search doesn't capture. The `action_required` flag also creates a false positive here: if the candidate took an action but the company never responded, that open action eventually becomes ghosting. A proper fix would track the date of the candidate's last outbound email per company and flag it as ghosted after a configurable window.

## Suggested Platform Feature

**The Arcade dashboard should surface per-user operational visibility.**

Arcade sits at the tool execution layer — every call goes through it, which means it already has the data to answer the questions that matter most when something breaks. Right now that data isn't surfaced, and the dashboard shows only cumulative totals that tell you very little in practice.

Three specific views would make a meaningful difference for anyone operating a user-facing agent:

**Tool call breakdown over time.** Cumulative counts don't help you debug or understand usage patterns. A time-series breakdown per tool — volume, error rate, latency — lets you see when failures started, which integrations are actually being used, and whether a problem is isolated or systemic.

**Tool calls by user.** When a user reports their agent isn't working, there's currently no way to see what happened for that specific user. A per-user call log — which tools were called, when, and whether they succeeded — would collapse a debugging session from a guessing game into a direct lookup.

**Auth status per user.** The most common reason a user-facing agent silently fails is that OAuth was never completed, expired, or was revoked. An ops view showing which users have active tokens, which have failed auth, and which have never authorized at all would let you diagnose this class of problem immediately rather than asking users to re-run the agent and watch for the auth prompt.

All three of these are data Arcade already holds. Surfacing them in the dashboard would make Arcade meaningfully more operable for teams running agents in production, not just building them.

## Troubleshooting

**"Missing required environment variable"** — Copy `.env.example` to `.env` and fill in all values.

**`tool_not_found` error** — Arcade tool names are versioned. Check the exact names available in your account by running:
```bash
python3 -c "
from arcadepy import Arcade; import os; from dotenv import load_dotenv; load_dotenv()
client = Arcade(api_key=os.environ['ARCADE_API_KEY'])
for t in client.tools.list(toolkit='gmail', limit=50).items: print(t.name)
"
```
Then update `ARCADE_TOOL_GMAIL_SEARCH` and `ARCADE_TOOL_GMAIL_GET_THREAD` in `config.py`.

**No threads found** — Increase `DAYS_TO_SCAN` in `.env` or add subject keywords to `SUBJECT_SEARCHES` in `gmail_scanner.py`.

**Duplicate company rows in Sheets** — Run `python agent.py sync`; the agent detects and blanks duplicate rows automatically on load.
