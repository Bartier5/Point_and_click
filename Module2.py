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

DETECT_JS = """
() => {
  const priceRe = /(₦|NGN|\\$|€|£)\\s?\\d[\\d,]*(\\.\\d+)?/;
  const colors = {price: "#16a34a", image: "#2563eb", heading: "#dc2626",
                  link: "#9333ea", text: "#ea580c"};
  const limits = {price: 20, image: 20, heading: 10, link: 12, text: 12};
  const counts = {};
  const found = [];

  const isStableId = id =>
    /^[A-Za-z][A-Za-z0-9_-]{1,30}$/.test(id) && !/[0-9]{4,}/.test(id);

  const countMatches = css => {
    try { return document.querySelectorAll(css).length; }
    catch (e) { return 0; }
  };

  const buildSelector = el => {
    const tag = el.tagName.toLowerCase();
    const testid = el.getAttribute("data-testid");
    if (testid && !testid.includes('"'))
      return {css: `[data-testid="${testid}"]`, stable: true};
    if (el.id && isStableId(el.id))
      return {css: "#" + CSS.escape(el.id), stable: true};
    for (const a of ["aria-label", "placeholder", "name", "alt"]) {
      const v = el.getAttribute(a);
      if (v && v.length < 60 && !v.includes('"'))
        return {css: `${tag}[${a}="${v}"]`, stable: true};
    }
    const path = [];
    let node = el;
    while (node && node !== document.body && path.length < 6) {
      if (node !== el && node.id && isStableId(node.id)) {
        path.unshift("#" + CSS.escape(node.id));
        break;
      }
      const t = node.tagName.toLowerCase();
      const same = [...node.parentElement.children]
        .filter(c => c.tagName === node.tagName);
      path.unshift(same.length > 1 ? `${t}:nth-of-type(${same.indexOf(node) + 1})` : t);
      node = node.parentElement;
    }
    return {css: path.join(" > "), stable: false};
  };

  const overlay = document.createElement("div");
  overlay.style.cssText = "position:fixed;top:0;left:0;width:100vw;height:100vh;" +
                          "pointer-events:none;z-index:2147483647;";
  document.body.appendChild(overlay);

  const candidates = document.querySelectorAll(
    "h1,h2,h3,h4,p,span,a,button,li,img,div"
  );

  for (const el of candidates) {
    const isImg = el.tagName === "IMG";
    const ownText = [...el.childNodes]
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent.trim())
      .join(" ")
      .trim();
    if (!isImg && ownText.length === 0) continue;

    const text = isImg ? "" : ownText;
    if (!isImg && text.length > 120) continue;

    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (r.top < 0 || r.bottom > window.innerHeight) continue;
    if (r.left < 0 || r.right > window.innerWidth) continue;

    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") continue;

    let kind = "text";
    if (isImg) kind = "image";
    else if (priceRe.test(text)) kind = "price";
    else if (/^H[1-4]$/.test(el.tagName)) kind = "heading";
    else if (el.tagName === "A") kind = "link";

    counts[kind] = (counts[kind] || 0) + 1;
    if (counts[kind] > limits[kind]) continue;

    const n = found.length + 1;
    const color = colors[kind];

    const box = document.createElement("div");
    box.style.cssText = `position:absolute;left:${r.left}px;top:${r.top}px;` +
      `width:${r.width}px;height:${r.height}px;border:2px solid ${color};`;
    const label = document.createElement("div");
    label.textContent = n;
    label.style.cssText = `position:absolute;left:0;top:0;background:${color};` +
      "color:white;font:bold 12px Arial;padding:1px 4px;";
    box.appendChild(label);
    overlay.appendChild(box);

    const sel = buildSelector(el);
    found.push({n, kind, tag: el.tagName.toLowerCase(),
                text: isImg ? (el.alt || el.src.slice(0, 40)) : text,
                selector: sel.css, stable: sel.stable,
                matches: countMatches(sel.css)});
  }
  return found;
}
"""


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

        await message.answer_photo(BufferedInputFile(png, filename="detected.png"))

        lines = [f"{e['n']}. [{e['kind']}] {e['text'][:40]}" for e in elements]
        if not lines:
            await message.answer("No elements found.")
            return

        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3500:
                await message.answer(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await message.answer(chunk)

    except Exception as e:
        logging.exception("detect failed")
        await message.answer(f"Failed: {type(e).__name__}\n{str(e)[:300]}")


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN not found. Check your .env file.")
    bot = Bot(token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())