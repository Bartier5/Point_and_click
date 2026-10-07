import asyncio
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from playwright.async_api import async_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO)

dp = Dispatcher()


@dp.message(Command("shot"))
async def shot(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /shot https://example.com")
        return

    url = parts[1].strip()
    if not url.startswith("http"):
        url = "https://" + url

    await message.answer("Loading page...")

    try:
        async with async_playwright() as p:
            logging.info("Launching browser")
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            logging.info("Going to %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            logging.info("Page loaded, taking screenshot")
            png = await page.screenshot(timeout=15000)
            await browser.close()
        await message.answer_photo(BufferedInputFile(png, filename="page.png"))
    except Exception as e:
        logging.exception("shot failed")
        await message.answer(f"Failed: {type(e).__name__}\n{str(e)[:300]}")


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN not found. Check your .env file.")
    bot = Bot(token)
    await dp.start_polling(bot)


asyncio.run(main())