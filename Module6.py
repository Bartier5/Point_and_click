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
from detector import DETECT_JS

load_dotenv()
logging.basicConfig(level=logging.INFO)

dp = Dispatcher()
sessions = {}

NEXT_SELECTORS = [
    'a[rel="next"]',
    "li.next a",
    '[aria-label*="next" i]',
    'a:has-text("Next")',
    'button:has-text("Next")',
]

EXTRACT_JS = """
({card, fields}) => {
  return [...document.querySelectorAll(card)].map(c => {
    const row = {};
    for (const f of fields) {
      const el = c.querySelector(f.rel);
      row[f.key] = "";
      if (f.kind === "link") {
        row[f.key + "_url"] = "";
        row[f.key + "_title"] = "";
      }
      if (!el) continue;
      if (f.kind === "image") {
        row[f.key] = el.src;
      } else {
        row[f.key] = (el.innerText || el.textContent || "").trim();
        if (f.kind === "link") {
          row[f.key + "_url"] = el.href;
          row[f.key + "_title"] = el.getAttribute("title") || "";
        }
      }
    }
    return row;
  });
}
"""


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
        return {
            key: text,
            key + "_url": await loc.evaluate("el => el.href"),
            key + "_title": await loc.get_attribute("title") or "",
        }
    return {key: text}


async def go_next(page):
    for sel in NEXT_SELECTORS:
        loc = page.locator(sel).first
        if await loc.count() and await loc.is_visible():
            try:
                await loc.click(timeout=5000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(800)
                return True
            except Exception:
                return False
    return False


async def scrape_list(page, card_css, fields, pages):
    rows = []
    for i in range(pages):
        batch = await page.evaluate(
            EXTRACT_JS, {"card": card_css, "fields": fields}
        )
        for r in batch:
            rows.append({"page": i + 1, **r})
        if i < pages - 1 and not await go_next(page):
            break
    return rows


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
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Usage: /detect https://example.com [pages]")
        return

    url = parts[1]
    if not url.startswith("http"):
        url = "https://" + url
    pages = 1
    if len(parts) > 2 and parts[2].isdigit():
        pages = max(1, min(int(parts[2]), 10))

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
            "pages": pages,
            "elements": elements,
            "selected": set(),
        }
        await message.answer_photo(
            BufferedInputFile(png, filename="detected.png"),
            caption=(
                "Tap fields to scrape. For lists, pick from the first item "
                "and I'll repeat it for every item. Then press Done."
            ),
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

    card_set = {e["card_css"] for e in chosen}
    first = chosen[0]
    s["list_mode"] = (
        len(card_set) == 1 and bool(first["card_css"]) and first["card_count"] >= 3
    )
    if s["list_mode"]:
        s["card_css"] = first["card_css"]
        s["fields"] = [
            {"key": f"{e['kind']}_{e['n']}", "kind": e["kind"], "rel": e["rel"]}
            for e in chosen
        ]
        info = (
            f"List mode: {first['card_count']} items per page, "
            f"{s['pages']} page(s)."
        )
    else:
        info = "Single item mode."

    kb = InlineKeyboardBuilder()
    kb.button(text="📄 CSV", callback_data="export:csv")
    kb.button(text="📊 Excel", callback_data="export:xlsx")
    await cb.message.answer(
        f"{len(chosen)} fields selected. {info}\nPick a format:",
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
    if s["list_mode"] and s["pages"] > 1:
        await cb.message.answer(f"Scraping {s['pages']} pages, this can take a minute...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(s["url"], wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            if s["list_mode"]:
                rows = await scrape_list(
                    page, s["card_css"], s["fields"], s["pages"]
                )
            else:
                row = {}
                for e in s["chosen"]:
                    row.update(await read_field(page, e))
                rows = [row]
            await browser.close()

        if not rows:
            await cb.message.answer("No items found on the page.")
            return

        if fmt == "csv":
            data, name = make_csv(rows), "scrape.csv"
        else:
            data, name = make_xlsx(rows), "scrape.xlsx"

        await cb.message.answer_document(
            BufferedInputFile(data, filename=name),
            caption=f"{len(rows)} row(s)",
        )
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