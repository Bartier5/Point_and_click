import asyncio
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright
from Module2 import DETECT_JS

load_dotenv()
logging.basicConfig(level=logging.INFO)

dp = Dispatcher()
sessions = {}


def build_keyboard(elements, selected):
    kb = InlineKeyboardBuilder()
    for e in elements:
        mark = "✅" if e["n"] in selected else ""
        kb.button(text=f"{mark}{e['n']}", callback_data=f"t:{e['n']}")
    kb.adjust(6)
    kb.row(InlineKeyboardButton(text="Done ✔", callback_data="done"))
    return kb.as_markup()


@dp.message(Command("detect"))
async def detect(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /detect https://example.com")
        return

    url = parts[1].strip()
    if not url.startswith("http"):
        url = "https://" + url

    await message.answer("Detecting elements...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            elements = await page.evaluate(DETECT_JS)
            png = await page.screenshot(timeout=15000)
            await browser.close()

        if not elements:
            await message.answer("No elements found.")
            return

        sessions[message.from_user.id] = {
            "url": url,
            "elements": elements,
            "selected": set(),
        }
        await message.answer_photo(
            BufferedInputFile(png, filename="detected.png"),
            caption="Tap the numbers you want to scrape, then press Done.",
            reply_markup=build_keyboard(elements, set()),
        )
    except Exception as e:
        logging.exception("detect failed")
        await message.answer(f"Failed: {type(e).__name__}\n{str(e)[:300]}")


@dp.callback_query(F.data.startswith("t:"))
async def toggle(cb: CallbackQuery):
    s = sessions.get(cb.from_user.id)
    if not s:
        await cb.answer("Session expired. Send /detect again.", show_alert=True)
        return
    n = int(cb.data.split(":")[1])
    s["selected"] ^= {n}
    await cb.message.edit_reply_markup(
        reply_markup=build_keyboard(s["elements"], s["selected"])
    )
    await cb.answer()


@dp.callback_query(F.data == "done")
async def done(cb: CallbackQuery):
    s = sessions.get(cb.from_user.id)
    if not s or not s["selected"]:
        await cb.answer("Select at least one element first.", show_alert=True)
        return

    chosen = [e for e in s["elements"] if e["n"] in s["selected"]]
    s["chosen"] = chosen

    blocks = []
    for e in chosen:
        quality = "stable" if e["stable"] else "structural"
        unique = "unique" if e["matches"] == 1 else f"{e['matches']} matches"
        blocks.append(
            f"{e['n']}. [{e['kind']}] {e['text'][:40]}\n"
            f"   {e['selector']}\n"
            f"   {quality}, {unique}"
        )
    await cb.message.answer(("Selectors:\n\n" + "\n\n".join(blocks))[:4000])
    await cb.answer()


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN not found. Check your .env file.")
    bot = Bot(token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())