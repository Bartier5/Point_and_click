import asyncio
import csv
import io
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openpyxl import Workbook
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


async def read_field(page, e):
    key = f"{e['kind']}_{e['n']}"
    loc = page.locator(e["selector"]).first
    if await loc.count() == 0:
        return {key: ""}
    if e["kind"] == "image":
        return {key: await loc.evaluate("el => el.src")}
    text = (await loc.inner_text(timeout=5000)).strip()
    if e["kind"] == "link":
        href = await loc.evaluate("el => el.href")
        return {key: text, key + "_url": href}
    return {key: text}


def make_csv(rows):
    headers = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def make_xlsx(rows):
    headers = list(rows[0].keys())
    wb = Workbook()
    ws = wb.active
    ws.title = "Scraped"
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h, "") for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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

    s["chosen"] = [e for e in s["elements"] if e["n"] in s["selected"]]

    kb = InlineKeyboardBuilder()
    kb.button(text="📄 CSV", callback_data="export:csv")
    kb.button(text="📊 Excel", callback_data="export:xlsx")
    await cb.message.answer(
        f"{len(s['chosen'])} fields selected. Pick a format:",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("export:"))
async def export(cb: CallbackQuery):
    s = sessions.get(cb.from_user.id)
    if not s or not s.get("chosen"):
        await cb.answer("Session expired. Send /detect again.", show_alert=True)
        return

    fmt = cb.data.split(":")[1]
    await cb.answer("Extracting...")

    try:
        row = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(s["url"], wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            for e in s["chosen"]:
                row.update(await read_field(page, e))
            await browser.close()

        rows = [row]
        if fmt == "csv":
            data, name = make_csv(rows), "scrape.csv"
        else:
            data, name = make_xlsx(rows), "scrape.xlsx"

        await cb.message.answer_document(BufferedInputFile(data, filename=name))
    except Exception as e:
        logging.exception("export failed")
        await cb.message.answer(f"Failed: {type(e).__name__}\n{str(e)[:300]}")


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN not found. Check your .env file.")
    bot = Bot(token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())