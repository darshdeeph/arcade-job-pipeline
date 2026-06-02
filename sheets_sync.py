import asyncio
import json
import logging
from datetime import datetime, timezone

from arcade_client import ArcadeClient
from classifier import EmailClassification
from config import SHEETS_SPREADSHEET_ID, STAGE_ORDER
from utils import normalize_company

logger = logging.getLogger(__name__)

HEADERS = [
    "Company", "Role", "Stage", "Sub-Status", "Action Required",
    "Action Description", "Last Contact", "Source", "Notes",
]
COL_LETTERS = list("ABCDEFGHI")
COL = {header: letter for header, letter in zip(HEADERS, COL_LETTERS)}
_IDX = {header: i for i, header in enumerate(HEADERS)}


class SheetsSync:
    def __init__(self) -> None:
        self._arcade = ArcadeClient()
        self.spreadsheet_id: str = SHEETS_SPREADSHEET_ID
        # normalize_company(name) -> {"row": int, "stage": str, "action_required": bool,
        #                             "company": str, "action_description": str}
        self._cache: dict[str, dict] = {}
        self._next_row: int = 2
        self._row_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Spreadsheet setup
    # ------------------------------------------------------------------

    async def ensure_spreadsheet(self) -> None:
        if not self.spreadsheet_id:
            print("No SHEETS_SPREADSHEET_ID set — creating 'Job Pipeline' spreadsheet...")
            header_row = {letter: header for letter, header in zip(COL_LETTERS, HEADERS)}
            data = await self._arcade.execute(
                "GoogleSheets.CreateSpreadsheet",
                {"title": "Job Pipeline", "data": json.dumps({"1": header_row})},
            )

            sheet_id = data.get("spreadsheet_id") or data.get("id") or data.get("spreadsheetId") or ""
            if not sheet_id:
                raise RuntimeError(f"CreateSpreadsheet did not return an ID. Response: {data}")

            self.spreadsheet_id = sheet_id
            print(f"\nSpreadsheet created! Add this to your .env:\n  SHEETS_SPREADSHEET_ID={sheet_id}")
            url = data.get("url") or data.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            print(f"  Open it here: {url}\n")

        # Always write the header row — restores headers if the sheet was cleared
        header_row = {letter: header for letter, header in zip(COL_LETTERS, HEADERS)}
        await self._arcade.execute(
            "GoogleSheets.UpdateCells",
            {"spreadsheet_id": self.spreadsheet_id, "data": json.dumps({"1": header_row})},
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def load_existing_entries(self) -> None:
        """Read all data rows; cache keyed by normalized company name.
        Duplicate rows for the same company are blanked out, keeping only
        the highest-stage row.
        """
        data = await self._arcade.execute(
            "GoogleSheets.GetSpreadsheet",
            {
                "spreadsheet_id": self.spreadsheet_id,
                "start_row": 2,
                "max_rows": 1000,
                "max_cols": len(HEADERS),
            },
        )

        rows = data.get("values") or data.get("rows") or data.get("data") or data.get("cells") or []
        rows_to_clear: list[int] = []

        for offset, row in enumerate(rows):
            row_number = offset + 2
            company, stage, action_required, action_description = self._extract_key_fields(row)
            if not company:
                continue

            key = normalize_company(company)
            existing = self._cache.get(key)

            if existing:
                existing_order = STAGE_ORDER.get(existing["stage"], -1)
                new_order = STAGE_ORDER.get(stage, 0)
                if new_order > existing_order:
                    rows_to_clear.append(existing["row"])
                    self._cache[key] = {
                        "row": row_number,
                        "stage": stage,
                        "action_required": action_required,
                        "company": company,
                        "action_description": action_description,
                    }
                else:
                    rows_to_clear.append(row_number)
            else:
                self._cache[key] = {
                    "row": row_number,
                    "stage": stage,
                    "action_required": action_required,
                    "company": company,
                    "action_description": action_description,
                }

        if rows_to_clear:
            print(f"  Clearing {len(rows_to_clear)} duplicate row(s)...")
            for row_num in rows_to_clear:
                await self._clear_row(row_num)

        self._next_row = max((e["row"] for e in self._cache.values()), default=1) + 1
        if self._next_row < 2:
            self._next_row = 2

        logger.info("Loaded %d companies; next free row: %d", len(self._cache), self._next_row)

    async def _clear_row(self, row_number: int) -> None:
        """Blank out all cells in a row (removes duplicate company entries)."""
        empty = {str(row_number): {letter: "" for letter in COL_LETTERS}}
        try:
            await self._arcade.execute(
                "GoogleSheets.UpdateCells",
                {"spreadsheet_id": self.spreadsheet_id, "data": json.dumps(empty)},
            )
        except RuntimeError as e:
            logger.warning("Could not clear row %d: %s", row_number, e)

    @staticmethod
    def _extract_key_fields(row) -> tuple[str, str, bool, str]:
        """Return (company, stage, action_required, action_description) from a row."""
        if isinstance(row, list):
            padded = list(row) + [""] * max(0, len(HEADERS) - len(row))
            company = str(padded[_IDX["Company"]]).strip()
            stage = str(padded[_IDX["Stage"]]).strip()
            action_required = str(padded[_IDX["Action Required"]]).upper() == "TRUE"
            action_description = str(padded[_IDX["Action Description"]]).strip()
        elif isinstance(row, dict):
            company = str(row.get(COL["Company"]) or row.get("Company") or "").strip()
            stage = str(row.get(COL["Stage"]) or row.get("Stage") or "").strip()
            ar_raw = row.get(COL["Action Required"]) or row.get("Action Required") or ""
            action_required = str(ar_raw).upper() == "TRUE"
            action_description = str(row.get(COL["Action Description"]) or row.get("Action Description") or "").strip()
        else:
            return "", "", False, ""

        return company, stage, action_required, action_description

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def _build_row_data(
        self,
        row_number: int,
        c: EmailClassification,
        date_str: str,
        note: str,
    ) -> dict:
        return {
            str(row_number): {
                COL["Company"]: c.company_name,
                COL["Role"]: c.role,
                COL["Stage"]: c.stage,
                COL["Sub-Status"]: c.sub_status or "",
                COL["Action Required"]: "TRUE" if c.action_required else "FALSE",
                COL["Action Description"]: c.action_description,
                COL["Last Contact"]: date_str,
                COL["Source"]: c.source,
                COL["Notes"]: note[:500],
            }
        }

    async def upsert_entry(
        self,
        thread_id: str,
        classification: EmailClassification,
        date_str: str,
        subject: str,
    ) -> str:
        """Create or update one row per company (max stage wins). Returns 'created' | 'updated' | 'skipped' | 'error'."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = normalize_company(classification.company_name)
        existing = self._cache.get(key)

        if existing:
            old_order = STAGE_ORDER.get(existing["stage"], -1)
            new_order = STAGE_ORDER.get(classification.stage, 0)
            if new_order <= old_order:
                return "skipped"

            note = f"{today}: {existing['stage']} → {classification.stage}"
            if classification.action_required:
                note += f"; action: {classification.action_description}"
            row_number = existing["row"]
        else:
            note = f"{today}: First tracked"
            if classification.action_required:
                note += f"; action: {classification.action_description}"
            async with self._row_lock:
                row_number = self._next_row
                self._next_row += 1

        try:
            await self._arcade.execute(
                "GoogleSheets.UpdateCells",
                {
                    "spreadsheet_id": self.spreadsheet_id,
                    "data": json.dumps(self._build_row_data(row_number, classification, date_str, note)),
                },
            )
        except RuntimeError as e:
            logger.error("UpdateCells failed for %s: %s", classification.company_name, e)
            return "error"

        self._cache[key] = {
            "row": row_number,
            "stage": classification.stage,
            "action_required": classification.action_required,
            "company": classification.company_name,
            "action_description": classification.action_description,
        }
        return "created" if not existing else "updated"

    # ------------------------------------------------------------------
    # Queries on local cache
    # ------------------------------------------------------------------

    def get_action_required_items(self) -> list[dict]:
        return [
            {
                "company": e["company"],
                "action": e["action_description"] or "See spreadsheet for details",
            }
            for e in self._cache.values()
            if e.get("action_required")
        ]

    def get_stage_counts(self) -> dict[str, int]:
        counts = {"Applied": 0, "Interviewing": 0, "Ended": 0}
        for entry in self._cache.values():
            stage = entry.get("stage", "")
            if stage in counts:
                counts[stage] += 1
        return counts
