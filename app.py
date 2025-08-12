from __future__ import annotations
import os, time, json, threading, asyncio, urllib.parse, uuid, shutil, atexit
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# ---------------- CONFIG ----------------
WAIT_SEC = 45
MAX_TXT = 4096
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "PASTE_TOKEN_IN_ENV"
BASE_URL = "https://www.linkedin.com/"
OUT_DIR = Path.cwd() / "out"; OUT_DIR.mkdir(exist_ok=True)
PNG_PATH = OUT_DIR / "job.png"

# Toggle: 0 = type in top search + click Jobs (original path)
#         1 = open the same Jobs results page by URL (more robust in headless)
USE_DIRECT_JOBS_URL = os.getenv("DIRECT_JOBS_URL", "0") == "1"

# -------------- tiny health server (Render health checks) ---------------
class _Health(BaseHTTPRequestHandler):
    def do_HEAD(self):
        if self.path in ("/", "/health"):
            self.send_response(200); self.end_headers()
        else:
            self.send_response(404); self.end_headers()
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(200); self.end_headers(); self.wfile.write(b"running")

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# -------------- Selenium ---------------
def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,3000")

    # unique profile dir to avoid "user data dir in use"
    profile_root = f"/tmp/chrome-user-data/{uuid.uuid4()}"
    os.makedirs(profile_root, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_root}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")

    drv = webdriver.Chrome(options=opts)

    def _cleanup():
        try: shutil.rmtree(profile_root, ignore_errors=True)
        except Exception: pass
    atexit.register(_cleanup)

    return drv

def wait(drv, cond):
    return WebDriverWait(drv, WAIT_SEC).until(cond)

def inject_cookies_if_any(drv) -> bool:
    """
    Reads LINKEDIN_COOKIES_JSON env var (JSON array of cookies).
    Each cookie sample:
    { "name": "li_at", "value": "...", "domain": ".linkedin.com", "path": "/", "secure": true, "httpOnly": true }
    """
    raw = os.getenv("LINKEDIN_COOKIES_JSON", "").strip()
    if not raw:
        return False
    try:
        cookies = json.loads(raw)
        # Must visit domain first so Selenium allows add_cookie
        drv.get("https://www.linkedin.com")
        for c in cookies:
            drv.add_cookie({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".linkedin.com"),
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
                "httpOnly": c.get("httpOnly", True),
            })
        return True
    except Exception:
        return False

def logged_in(drv) -> bool:
    try:
        wait(drv, EC.presence_of_element_located((By.ID, "global-nav")))
        return True
    except TimeoutException:
        return False

def login(drv):
    inject_cookies_if_any(drv)  # cookie-based login
    drv.get("https://www.linkedin.com/feed/")
    if not logged_in(drv):
        print("⚠️ Not logged in; results may be limited.")

def perform_search(drv, query: str):
    # primary selector for top search
    sel1 = (By.CSS_SELECTOR, 'input[placeholder="Search"][role="combobox"]')
    # fallback sometimes used in alternate layouts
    sel2 = (By.CSS_SELECTOR, 'input[aria-label="Search"]')
    try:
        box = wait(drv, EC.visibility_of_element_located(sel1))
    except TimeoutException:
        box = wait(drv, EC.visibility_of_element_located(sel2))
    box.clear()
    box.send_keys(query)
    time.sleep(0.3)
    box.send_keys(Keys.ENTER)
    wait(drv, EC.url_contains("/search/results/"))

def open_jobs_tab(drv):
    btn = wait(drv, EC.element_to_be_clickable((By.XPATH, '//button[normalize-space()="Jobs"]')))
    btn.click()
    wait(drv, EC.presence_of_all_elements_located((
        By.CSS_SELECTOR,
        'a.job-card-job-posting-card-wrapper__card-link, a.job-card-container__link'
    )))

def go_to_jobs_search(drv, query: str):
    url = "https://www.linkedin.com/jobs/search/?keywords=" + urllib.parse.quote_plus(query)
    drv.get(url)
    wait(drv, EC.presence_of_all_elements_located((
        By.CSS_SELECTOR,
        'a.job-card-job-posting-card-wrapper__card-link, a.job-card-container__link'
    )))

def job_links(drv):
    return drv.find_elements(By.CSS_SELECTOR, 'a.job-card-job-posting-card-wrapper__card-link, a.job-card-container__link')

def open_job(drv, idx: int) -> Optional[str]:
    ls = job_links(drv)
    if idx >= len(ls): return None
    drv.execute_script("arguments[0].scrollIntoView({block:'center'});", ls[idx])
    title = ls[idx].text.strip() or f"Job {idx+1}"
    ls[idx].click()
    wait(drv, EC.presence_of_element_located((By.CSS_SELECTOR, 'div.jobs-semantic-search-job-details-wrapper')))
    time.sleep(1)
    return title

def wrapper(drv):
    return drv.find_element(By.CSS_SELECTOR, 'div.jobs-semantic-search-job-details-wrapper')

def capture(drv) -> str:
    elem = wrapper(drv)
    full_h = drv.execute_script("return arguments[0].scrollHeight", elem)
    drv.set_window_size(1280, min(full_h + 120, 16000))
    drv.execute_script("arguments[0].scrollTop = 0", elem); time.sleep(0.4)
    elem.screenshot(str(PNG_PATH))
    return elem.text.strip()

# -------------- Telegram helpers ----------------
async def send_job(ctx: ContextTypes.DEFAULT_TYPE, title: str):
    drv = ctx.user_data["drv"]
    idx = ctx.user_data["idx"]
    total = ctx.user_data["total"]

    text = capture(drv)
    chat = ctx.chat_data["chat"]
    ids: List[int] = []

    with open(PNG_PATH, "rb") as f:
        p = await chat.send_photo(f, caption=f"{title} ({idx+1}/{total})"); ids.append(p.message_id)

    if text:
        for i in range(0, len(text), MAX_TXT):
            m = await chat.send_message(text[i:i+MAX_TXT]); ids.append(m.message_id)

    buttons = [InlineKeyboardButton("Next ▶️", callback_data="next")] if idx+1 < total else []
    buttons.append(InlineKeyboardButton("Clear", callback_data="clear"))
    lnk = await chat.send_message(drv.current_url, reply_markup=InlineKeyboardMarkup([buttons]))
    ids.append(lnk.message_id)

    ctx.user_data.setdefault("msg_ids", []).extend(ids)

async def clear_msgs(ctx: ContextTypes.DEFAULT_TYPE):
    chat = ctx.chat_data["chat"]
    for mid in ctx.user_data.get("msg_ids", []):
        try: await chat.delete_message(mid)
        except Exception: pass
    ctx.user_data["msg_ids"] = []

# -------------- Handlers -----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.chat_data["chat"] = update.effective_chat
    ctx.user_data["awaiting_query"] = True
    await update.effective_chat.send_message("Send your LinkedIn search query (e.g., 'Online Reputation Management').")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_query"): return
    query = (update.message.text or "").strip()
    if not query:
        await update.effective_chat.send_message("Please type a search query, e.g., Online Reputation Management")
        return

    ctx.user_data["awaiting_query"] = False
    drv = make_driver(); ctx.user_data.update({"drv": drv, "idx": 0})

    # Login, then reach the Jobs results page by the chosen method
    login(drv)
    try:
        if USE_DIRECT_JOBS_URL:
            go_to_jobs_search(drv, query)
        else:
            drv.get(BASE_URL)
            perform_search(drv, query)
            open_jobs_tab(drv)
    except TimeoutException:
        drv.save_screenshot(str(OUT_DIR / "fail.png"))
        await update.effective_chat.send_message("Timed out loading results; saved fail.png for debugging.")
        raise

    ctx.user_data["total"] = len(job_links(drv))
    if ctx.user_data["total"] == 0:
        await update.effective_chat.send_message("No jobs found for that query."); return

    title = open_job(drv, 0) or "Job 1"
    await send_job(ctx, title)

async def cb_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if ctx.user_data["idx"] + 1 >= ctx.user_data["total"]:
        await update.effective_chat.send_message("No more jobs."); return
    ctx.user_data["idx"] += 1
    title = open_job(ctx.user_data["drv"], ctx.user_data["idx"])
    await send_job(ctx, title)

async def cb_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await clear_msgs(ctx)
    drv = ctx.user_data.pop("drv", None)
    if drv:
        try: drv.quit()
        except Exception: pass
    ctx.user_data.clear()
    await update.effective_chat.send_message("Chat cleared. Send /start to run again.")

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        print(f"Exception: {ctx.error}")
    except Exception:
        pass

# -------------- Main (async) ---------------------
async def main():
    if not TOKEN or TOKEN.startswith("PASTE_"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()

    # Ensure polling mode (no webhook) and start clean
    await app.bot.delete_webhook(drop_pending_updates=True)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_next, pattern="^next$"))
    app.add_handler(CallbackQueryHandler(cb_clear, pattern="^clear$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    print("Bot running – /start in chat")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    start_health_server()
    asyncio.run(main())
