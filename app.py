from __future__ import annotations
import os, time, threading
from pathlib import Path
from typing import List, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------------- CONFIG ----------------
SEARCH_QUERY = "Online Reputation Management"
WAIT_SEC = 25
MAX_TXT = 4096
TXT_DOC_THRESHOLD = 15000
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8429459087:AAFVaSMqitwVbfGoNkhCV-MxtHpZ1-8uG3w"
BASE_URL = "https://www.linkedin.com/"
PROFILE_ROOT = Path.home()/"AppData"/"Local"/"Google"/"Chrome"/"LinkedInSeleniumProfile"
PROFILE_ROOT.parent.mkdir(parents=True, exist_ok=True)
OUT_DIR = Path.cwd()/"out"; OUT_DIR.mkdir(exist_ok=True)
PNG_PATH = OUT_DIR/"job.png"

# -------------- Health Check Server ---------------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))  # Render's default is 10000
    srv = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# -------------- Selenium ---------------
def make_driver() -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    profile = os.environ.get("PROFILE_ROOT", "/profile")
    Path(profile).mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument("--profile-directory=Default")
    # opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2400")
    return webdriver.Chrome(options=opts)

def wait(drv, cond):
    return WebDriverWait(drv, WAIT_SEC).until(cond)

def logged_in(drv) -> bool:
    try:
        wait(drv, EC.presence_of_element_located((By.ID, "global-nav")))
        return True
    except Exception:
        return False

def login(drv):
    try:
        WebDriverWait(drv, 5).until(EC.presence_of_element_located((By.ID, "global-nav")))
        return
    except Exception:
        pass
    drv.get(BASE_URL)

def perform_search(drv):
    box = wait(drv, EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[placeholder="Search"][role="combobox"]')))
    box.clear(); box.send_keys(SEARCH_QUERY); time.sleep(0.3); box.send_keys(Keys.ENTER)
    wait(drv, EC.url_contains("/search/results/"))

def open_jobs_tab(drv):
    wait(drv, EC.element_to_be_clickable((By.XPATH, '//button[normalize-space()="Jobs"]'))).click()
    wait(drv, EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'a.job-card-job-posting-card-wrapper__card-link, a.job-card-container__link')))

def job_links(drv):
    return drv.find_elements(By.CSS_SELECTOR, 'a.job-card-job-posting-card-wrapper__card-link, a.job-card-container__link')

def open_job(drv, idx: int) -> Optional[str]:
    ls = job_links(drv)
    if idx >= len(ls):
        return None
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
    drv.set_window_size(1200, min(full_h + 120, 16000))
    drv.execute_script("arguments[0].scrollTop = 0", elem); time.sleep(0.4)
    elem.screenshot(str(PNG_PATH))
    return elem.text.strip()

# -------------- Telegram ----------------
async def send_job(ctx: ContextTypes.DEFAULT_TYPE, title: str):
    drv = ctx.user_data["drv"]
    idx = ctx.user_data["idx"]
    total = ctx.user_data["total"]

    text = capture(drv)
    chat = ctx.chat_data["chat"]
    ids: List[int] = []

    with open(PNG_PATH, "rb") as f:
        p = await chat.send_photo(f, caption=f"{title} ({idx+1}/{total})"); ids.append(p.message_id)

    if len(text) <= MAX_TXT:
        m = await chat.send_message(text); ids.append(m.message_id)
    else:
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
    await update.effective_chat.send_message(
        "Send your LinkedIn search query (e.g., 'Online Reputation Management posted in the past 24 hours')."
    )

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_query"):
        return
    query = (update.message.text or "").strip()
    if not query:
        await update.effective_chat.send_message("Please type a search query, e.g., Online Reputation Management posted in the past 24 hours")
        return
    ctx.user_data["awaiting_query"] = False
    global SEARCH_QUERY
    SEARCH_QUERY = query

    drv = make_driver(); ctx.user_data.update({"drv": drv, "idx": 0})
    drv.get(BASE_URL)
    login(drv)
    perform_search(drv)
    open_jobs_tab(drv)
    ctx.user_data["total"] = len(job_links(drv))

    if ctx.user_data["total"] == 0:
        await update.effective_chat.send_message("No jobs found for that query.")
        return

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

# -------------- Main -----------------
if __name__ == "__main__":
    if TOKEN.startswith("PASTE_"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_next, pattern="^next$"))
    app.add_handler(CallbackQueryHandler(cb_clear, pattern="^clear$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    port = int(os.environ.get("PORT", "10000"))
    url_path = f"webhook/{TOKEN}"                  # secret-ish path
    webhook_url = os.environ.get("WEBHOOK_URL", "")  # set after first deploy

    start_health_server()  # Always start health check server

    if webhook_url:
        # Cloud Run / production mode
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=f"{webhook_url}/{url_path}"
        )
    else:
        # Local testing mode (polling)
        app.run_polling()
