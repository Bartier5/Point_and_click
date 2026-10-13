# Point and Click Scraper Bot

A Telegram bot that lets you build a web scraper by tapping. Send a URL,
tap the elements you want on a numbered screenshot, and get the data back
as CSV or Excel. For product lists and directories, pick fields from the
first item and the bot repeats them across every item and page.

![demo](docs/demo.gif)

## How it works

1. `/detect <url> [pages]` opens the page in headless Chromium (Playwright)
   and finds visible text, prices, images, headings and links.
2. It draws numbered, color-coded boxes on a screenshot and sends it with
   one button per element.
3. You tap the fields you want. Repeating structures (product cards, rows)
   are detected automatically and only the first item is shown.
4. For each pick it generates a CSS selector, preferring stable attributes
   (`data-testid`, clean `id`, `aria-label`, `alt`) and falling back to a
   structural path.
5. The bot re-opens the page, extracts every item, follows the Next button
   for the requested number of pages, and sends a CSV or Excel file.

## Features

- Visual element picker inside Telegram (inline keyboard)
- Automatic list and card detection with relative selectors
- Multi-page scraping (up to 10 pages) via common Next-button patterns
- CSV (Excel-safe UTF-8) and Excel export, generated in memory
- Shared browser, per-request isolated contexts, job limit
- Allowlist by Telegram user ID, URL safety checks, session expiry

## Setup

    git clone https://github.com/Bartier5/<repo-name>.git
    cd <repo-name>
    python -m venv venv
    venv\Scripts\activate
    pip install -r requirements.txt
    playwright install chromium

Create a bot with @BotFather, then copy `.env.example` to `.env` and fill it:

    BOT_TOKEN=...
    ALLOWED_USERS=your-telegram-id

Send `/id` to the bot to get your ID. Run:

    python bot.py

## Usage

    /detect books.toscrape.com 3

Pick the title, price and image of the first book, press Done, choose CSV.
Result: 60 rows across 3 pages.

## Limitations

- Detects only elements visible in the first screen (no infinite scroll)
- Pagination supports standard Next buttons and links, not "Load more"
- Pages protected by CAPTCHAs, bot detection or login are not supported
- Content inside iframes or shadow DOM is not detected
- Cards with inconsistent inner structure can produce blank cells

Use only on sites whose terms of service and robots.txt allow scraping.

## Project history

Built in six modules: bot skeleton, element detection, tap-to-select,
selector generation, export, repeating lists and pagination
(see `module1.py` to `module6.py`). `bot.py` is the final version.

## Tech

Python 3.12, aiogram 3, Playwright (async), openpyxl