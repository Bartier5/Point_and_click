import asyncio
import csv
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from dotenv import load_dotenv
from aiogram import BaseMiddleware, Bot, Dispatcher, F
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

ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip().isdigit()
}
SESSION_TTL = 30 * 60
MAX_JOBS = 2
BLOCKED_HOSTS = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "169.254.")

dp = Dispatcher()
sessions = {}
state = {}
jobs = asyncio.Semaphore(MAX_JOBS)

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

HELP_TEXT = (
    "Point and Click Scraper\n\n"
    "/detect <url> [pages] - open a page and pick what to scrape\n"
    "Example: /detect books.toscrape.com 3\n\n"
    "Tap the numbered fields (pick from the first item of a list), "
    "press Done, then choose CSV or Excel.\n\n"
    "/id - show your Telegram ID"
)


# ---------- access control ----------

class AllowlistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        if user and user.id in ALLOWED:
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/id"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Not authorized.", show_alert=True)
        else:
            await event.answer("Not authorized. Send /id to see your ID.")
        return None


dp.message.outer_middleware(AllowlistMiddleware())
dp.callback_query.outer_middleware(AllowlistMiddleware())


# ---------- helpers ----------

@asynccontextmanager
async def page_session():
    context = await state["browser"].new_context(
        viewport={"width": 1280, "height": 800}
    )
    page = await context.new_page()
    try:
        yield page
    finally:
        await context.close()


def clean_url(raw):
    url = raw if raw.startswith(("http://", "https://")) else "https://" + raw
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return None
    if host.startswith(BLOCKED_HOSTS):
        return None
    return url


def friendly_error(e):
    name = type(e).__name__
    text = str(e)
    if name == "TimeoutError":
        return "The page took too long to respond. Try again or use a lighter page."
    if "ERR_NAME_NOT_RESOLVED" in text:
        return "I couldn't find that website. Check the address."
    return f"{name}: {text[:200]}"


def get_session(user_id):
    s = sessions.get(user_id)
    if s and time.time() - s["ts"] > SESSION_TTL:
        del sessions[user_id]
        return None
    return s


def purge_sessions():
    cutoff = time.time() - SESSION_TTL
    for uid in [u for u, s in sessions.items() if s["ts"] < cutoff]:
        del sessions[uid]


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
    pages_done = 0
    stopped_early = False
    for i in range(pages):
        batch = await page.evaluate(
            EXTRACT_JS, {"card": card_css, "fields": fields}
        )
        pages_done += 1
        rows.extend({"page": i + 1, **r} for r in batch)
        if i < pages - 1 and not await go_next(page):
            stopped_early = True
            break
    return rows, pages_done, stopped_early


async def run_export(s):
    async with page_session() as page:
        await page.goto(s["url"], wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
        if s["list_mode"]:
            return await scrape_list(page, s["card_css"], s["fields"], s["pages"])
        row = {}
        for e in s["chosen"]:
            row.update(await read_field(page, e))
        return [row], 1, False


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
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- handlers ----------

@dp.message(Command("start", "help"))
async def start(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"Your Telegram ID: {message.from_user.id}")


@dp.message(Command("detect"))
async def detect(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Usage: /detect https://example.com [pages]")
        return

    url = clean_url(parts[1])
    if not url:
        await message.answer("That doesn't look like a valid public web address.")
        return

    pages = 1
    if len(parts) > 2 and parts[2].isdigit():
        pages = max(1, min(int(parts[2]), 10))

    purge_sessions()
    await message.answer("Detecting elements...")

    try:
        async with jobs:
            async with page_session() as page:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1500)
                elements = await page.evaluate(DETECT_JS)
                png = await page.screenshot(timeout=15000)

        if not elements:
            await message.answer("No elements found on that page.")
            return

        sessions[message.from_user.id] = {
            "url": url,
            "pages": pages,
            "elements": elements,
            "selected": set(),
            "ts": time.time(),
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
        await message.answer(f"Failed: {friendly_error(e)}")


@dp.callback_query(F.data.startswith("t:"))
async def toggle(cb: CallbackQuery):
    s = get_session(cb.from_user.id)
    if not s:
        await cb.answer("Session expired. Send /detect again.", show_alert=True)
        return
    n = int(cb.data.split(":")[1])
    s["selected"] ^= {n}
    s["ts"] = time.time()
    await cb.message.edit_reply_markup(
        reply_markup=build_keyboard(s["elements"], s["selected"])
    )
    await cb.answer()


@dp.callback_query(F.data == "done")
async def done(cb: CallbackQuery):
    s = get_session(cb.from_user.id)
    if not s:
        await cb.answer("Session expired. Send /detect again.", show_alert=True)
        return
    if not s["selected"]:
        await cb.answer("Select at least one element first.", show_alert=True)
        return

    chosen = [e for e in s["elements"] if e["n"] in s["selected"]]
    s["chosen"] = chosen
    s["ts"] = time.time()

    first = chosen[0]
    card_set = {e["card_css"] for e in chosen}
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
    s = get_session(cb.from_user.id)
    if not s or not s.get("chosen"):
        await cb.answer("Session expired. Send /detect again.", show_alert=True)
        return

    fmt = cb.data.split(":")[1]
    await cb.answer("Extracting...")
    if s["list_mode"] and s["pages"] > 1:
        await cb.message.answer(
            f"Scraping up to {s['pages']} pages, this can take a minute..."
        )

    try:
        async with jobs:
            rows, pages_done, stopped_early = await asyncio.wait_for(
                run_export(s), timeout=180
            )

        if not rows:
            await cb.message.answer("No items found on the page.")
            return

        if fmt == "csv":
            data, name = make_csv(rows), "scrape.csv"
        else:
            data, name = make_xlsx(rows), "scrape.xlsx"

        note = f"{len(rows)} row(s) from {pages_done} page(s)"
        if stopped_early:
            note += f". No Next button found after page {pages_done}."
        await cb.message.answer_document(
            BufferedInputFile(data, filename=name), caption=note
        )
    except Exception as e:
        logging.exception("export failed")
        await cb.message.answer(f"Failed: {friendly_error(e)}")


# ---------- startup ----------

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN not found. Check your .env file.")
    if not ALLOWED:
        logging.warning(
            "ALLOWED_USERS is empty. Everyone is blocked. "
            "Send /id to the bot, then add your ID to .env."
        )

    pw = await async_playwright().start()
    state["browser"] = await pw.chromium.launch()
    bot = Bot(token)
    try:
        await dp.start_polling(bot)
    finally:
        await state["browser"].close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())