import asyncio
import json
import os
import aiohttp
from google import genai
from google.genai.types import GenerateContentConfig
from openai import AsyncOpenAI
from models.image_prompt import ImagePrompt
from models.sql.image_asset import ImageAsset
from utils.download_helpers import download_file
from utils.get_env import get_pexels_api_key_env
from utils.get_env import get_pixabay_api_key_env
from utils.get_env import get_ideogram_api_key_env
from utils.image_provider import (
    is_pixels_selected,
    is_pixabay_selected,
    is_gemini_flash_selected,
    is_dalle3_selected,
    is_ideogram_selected,
)
from utils.s3_utils import upload_file_to_s3
import uuid


class ImageGenerationService:

    def __init__(self, output_directory: str):
        self.output_directory = output_directory
        self.image_gen_func = self.get_image_gen_func()

    def get_image_gen_func(self):
        if is_pixabay_selected():
            return self.get_image_from_pixabay
        elif is_pixels_selected():
            return self.get_image_from_pexels
        elif is_gemini_flash_selected():
            return self.generate_image_google
        elif is_dalle3_selected():
            return self.generate_image_openai
        elif is_ideogram_selected():
            return self.get_image_from_ideogram
        return None

    def is_stock_provider_selected(self):
        return is_pixels_selected() or is_pixabay_selected()

    async def generate_image(self, prompt: ImagePrompt) -> str | ImageAsset:
        """
        Generates an image based on the provided prompt.
        - If no image generation function is available, returns a placeholder image.
        - If the stock provider is selected, it uses the prompt directly,
        otherwise it uses the full image prompt with theme.
        - Output Directory is used for saving the generated image not the stock provider.
        """
        if not self.image_gen_func:
            print("No image generation function found. Using placeholder image.")
            return "/static/images/placeholder.jpg"

        image_prompt = prompt.get_image_prompt(
            with_theme=not self.is_stock_provider_selected()
        )
        print(f"Request - Generating Image for {image_prompt}")

        try:
            if self.is_stock_provider_selected():
                image_path = await self.image_gen_func(image_prompt)
            else:
                image_path = await self.image_gen_func(
                    image_prompt, self.output_directory
                )

            if image_path:
                # If provider already returns an HTTP URL (e.g. Pexels/Pixabay),
                # just return it as-is.
                if image_path.startswith("http"):
                    return image_path
                elif os.path.exists(image_path):
                    # For locally generated images, upload to S3/MinIO (when configured)
                    # and return an ImageAsset that keeps both the local path and S3 key.
                    s3_key = await upload_file_to_s3(
                        image_path,
                        # Use a generic postfix so S3 keys can still be grouped by presentation
                        postfix=prompt.presentation_id,
                    )
                    if not s3_key:
                        raise Exception("Failed to upload generated image to S3")
                    print(f"Image uploaded to S3: {s3_key}")

                    return ImageAsset(
                        path=image_path,
                        is_uploaded=True,
                        s3_url=s3_key,
                        extras={
                            "prompt": prompt.prompt,
                            "theme_prompt": prompt.theme_prompt,
                        },
                    )

                # # For locally generated images, upload to S3/MinIO when configured.
                # if os.path.exists(image_path):
                #     s3_url = await upload_file_to_s3(image_path)
                #     if s3_url:
                #         # Return S3 URL in the same way as stock providers.
                #         return s3_url

            # If we reach this point, either no path was returned, the file does
            # not exist, or the upload failed.
            raise Exception(f"Image not found or upload failed for {image_path}")

        except Exception as e:
            print(f"Error generating image: {e}")
            return "/static/images/placeholder.jpg"

    async def generate_image_openai(self, prompt: str, output_directory: str) -> str:
        client = AsyncOpenAI()
        result = await client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            n=1,
            quality="standard",
            size="1024x1024",
        )
        image_url = result.data[0].url
        return await download_file(image_url, output_directory)

    async def generate_image_google(self, prompt: str, output_directory: str) -> str:
        client = genai.Client()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash-image-preview",
            contents=[prompt],
            config=GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
        )

        for part in response.candidates[0].content.parts:
            if part.text is not None:
                print(part.text)
            elif part.inline_data is not None:
                image_path = os.path.join(output_directory, f"{uuid.uuid4()}.jpg")
                with open(image_path, "wb") as f:
                    f.write(part.inline_data.data)

        return image_path

    async def get_image_from_ideogram(self, prompt: str, output_directory: str) -> str:
        api_key = get_ideogram_api_key_env()
        if not api_key:
            raise Exception("IDEOGRAM_API_KEY must be provided")

        url = "https://api.ideogram.ai/v1/ideogram-v3/generate"
        headers = {
            "Api-Key": api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "rendering_speed": "DEFAULT",
            "aspect_ratio": "1x1",
        }

        async with aiohttp.ClientSession(trust_env=True) as session:
            response = await session.post(url, headers=headers, json=payload)
            if response.status >= 400:
                text = await response.text()
                raise Exception(f"Ideogram API error {response.status}: {text}")
            data = await response.json()

        image_url = None
        try:
            if isinstance(data, dict) and "data" in data and data["data"]:
                first = data["data"][0]
                image_url = (
                    first.get("url") or first.get("image_url") or first.get("imageUrl")
                )
        except Exception:
            image_url = None

        if not image_url:
            raise Exception("Ideogram API did not return an image URL")

        return await download_file(image_url, output_directory)

    async def get_image_from_pexels(self, prompt: str) -> str:
        async with aiohttp.ClientSession(trust_env=True) as session:
            response = await session.get(
                f"https://api.pexels.com/v1/search?query={prompt}&per_page=1",
                headers={"Authorization": f"{get_pexels_api_key_env()}"},
            )
            data = await response.json()
            image_url = data["photos"][0]["src"]["large"]
            return image_url

    async def get_image_from_pixabay(self, prompt: str) -> str:
        api_key = get_pixabay_api_key_env()
        if not api_key:
            raise Exception("PIXABAY_API_KEY must be provided")

        url = "https://pixabay.com/api/"
        params = {
            "key": api_key,
            "q": prompt,
            "image_type": "photo",
            "per_page": "3",
        }
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(url, params=params) as response:
                text = await response.text()
                ctype = response.headers.get("Content-Type", "")

                if response.status >= 400:
                    raise Exception(
                        f"Pixabay HTTP {response.status} ({ctype}): {text[:500]}"
                    )

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    raise Exception(
                        "Pixabay returned non-JSON (invalid key, rate limit, IP block, "
                        f"or HTML error page). Content-Type: {ctype}. Preview: {text[:400]}"
                    )

                hits = data.get("hits") or []
                if not hits:
                    raise Exception(
                        "Pixabay returned no images for this query. "
                        f"total={data.get('total', 'n/a')}"
                    )

                image_url = hits[0].get("largeImageURL")
                if not image_url:
                    raise Exception("Pixabay hit missing largeImageURL")

                return image_url
