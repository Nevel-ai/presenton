from anthropic import AsyncAnthropic
import httpx
from openai import AsyncOpenAI
from google import genai


async def list_available_openai_compatible_models(
    url: str, api_key: str, proxy_url: str | None = None
) -> list[str]:
    async with httpx.AsyncClient(
        proxy=proxy_url,
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
    ) as http_client:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=url,
            http_client=http_client,
        )
        models = (await client.models.list()).data
        if models:
            return list(map(lambda x: x.id, models))
        return []


async def list_available_anthropic_models(api_key: str) -> list[str]:
    client = AsyncAnthropic(api_key=api_key)
    return list(map(lambda x: x.id, (await client.models.list(limit=50)).data))


async def list_available_google_models(api_key: str) -> list[str]:
    client = genai.Client(api_key=api_key)
    return list(map(lambda x: x.name, client.models.list(config={"page_size": 50})))
