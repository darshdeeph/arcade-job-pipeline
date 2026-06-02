import json

from arcadepy import AsyncArcade

from config import ARCADE_API_KEY, ARCADE_BASE_URL, GMAIL_USER_ID


class ArcadeClient:
    """Thin async wrapper around the Arcade SDK — handles auth and tool execution."""

    def __init__(self) -> None:
        self._client = AsyncArcade(api_key=ARCADE_API_KEY, base_url=ARCADE_BASE_URL)
        self._user_id = GMAIL_USER_ID

    async def _authorize(self, tool_name: str) -> None:
        auth = await self._client.tools.authorize(tool_name=tool_name, user_id=self._user_id)
        if auth.status != "completed":
            print(f"\nGoogle authorization required. Open this URL in your browser:\n  {auth.url}\n")
            print("Waiting for authorization...", end="", flush=True)
            await self._client.auth.wait_for_completion(auth)
            print(" done!\n")

    async def execute(self, tool_name: str, inputs: dict) -> dict:
        await self._authorize(tool_name)

        result = await self._client.tools.execute(
            tool_name=tool_name,
            input=inputs,
            user_id=self._user_id,
        )

        if not result.success:
            raise RuntimeError(f"{tool_name} failed: {result.output.error.message}")

        value = result.output.value
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {"raw": value}
        return {}
