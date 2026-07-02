import asyncio, logging, os, re, random, sqlite3
from urllib.parse import urlparse
import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "6067177575:AAEUVOteOiERUHE5v75iudEdHAGiCRXBGus")
ADMIN_ID = int(os.getenv("ADMIN_ID", "2119464081"))
SIMS_API_KEY = os.getenv("SIMS_API_KEY", "03b8ccc51ef4cdc16246fdb3c4668b21")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")
PROXY_URL = os.getenv("PROXY_URL", "")
WEB_APP_URL = os.getenv("WEB_APP_URL", "")   # <-- your mini app URL

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Database
DB = sqlite3.connect("bot_data.db", check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT, first_name TEXT,
    balance REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, amount REAL,
    screenshot_file_id TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS email_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, desired_email TEXT, password TEXT,
    status TEXT DEFAULT 'processing',
    cost REAL DEFAULT 10.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
DB.commit()

# Proxy parser
def parse_proxy_url(url: str) -> dict | None:
    if not url: return None
    try:
        parsed = urlparse(url)
        if not parsed.hostname: return None
        cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
        if parsed.username: cfg["username"] = parsed.username
        if parsed.password: cfg["password"] = parsed.password
        return cfg
    except:
        return None

# ---------- 4sim API ----------
SIM_API_BASE = "https://api.4sim.st"
google_service_id = None

async def fetch_google_service_id():
    global google_service_id
    if google_service_id:
        return google_service_id
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{SIM_API_BASE}/getServices?apikey={SIMS_API_KEY}")
        data = resp.json()
        for service in data.get("services", data):
            if isinstance(service, dict):
                name = service.get("name", "").lower()
                if "google" in name:
                    google_service_id = service["id"]
                    logger.info(f"Found Google service ID: {google_service_id}")
                    return google_service_id
        raise Exception("Google service not found in 4sim")

async def buy_activation() -> dict:
    sid = await fetch_google_service_id()
    url = f"{SIM_API_BASE}/buyNumber?apikey={SIMS_API_KEY}&id={sid}&country=any"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        data = resp.json()
        if data.get("status") != "SUCCESS":
            raise Exception(f"4sim buy failed: {data.get('message', data)}")
        return {"id": data["id"], "phone": data["number"]}

async def get_sms(activation_id: str) -> str:
    for _ in range(10):
        await asyncio.sleep(30)
        url = f"{SIM_API_BASE}/checkSms?apikey={SIMS_API_KEY}&id={activation_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            data = resp.json()
            if data.get("status") == "SUCCESS" and data.get("sms"):
                sms_text = data["sms"]
                m = re.search(r'(\d{4,8})', sms_text)
                return m.group(1) if m else sms_text
    raise TimeoutError("SMS not received in 5 minutes")

async def cancel_activation(activation_id: str):
    try:
        url = f"{SIM_API_BASE}/cancelNumber?apikey={SIMS_API_KEY}&id={activation_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            await client.get(url)
    except Exception as e:
        logger.warning(f"Cancel failed (non-critical): {e}")

# ---------- 2Captcha ----------
async def solve_captcha(page) -> str | None:
    if not CAPTCHA_API_KEY: return None
    url = page.url
    sitekey = await page.evaluate('''()=>{
        const ifs=document.querySelectorAll('iframe');
        for(let f of ifs){const s=f.src;if(s.includes('google.com/recaptcha')){const m=s.match(/[?&]k=([^&]+)/);if(m)return m[1]}}
        const el=document.querySelector('[data-sitekey]');return el?el.getAttribute('data-sitekey'):null
    }''')
    if not sitekey: return None
    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.get("https://2captcha.com/in.php", params={
            "key": CAPTCHA_API_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": url, "json": 1
        })
        res = r.json()
        if res.get("status")!=1: return None
        task_id = res["request"]
        for _ in range(30):
            await asyncio.sleep(10)
            r = await cl.get("https://2captcha.com/res.php", params={
                "key": CAPTCHA_API_KEY, "action": "get", "id": task_id, "json": 1
            })
            data = r.json()
            if data.get("status")==1:
                token = data["request"]
                try:
                    await page.evaluate(f'''
                        var ta=document.getElementById('g-recaptcha-response');
                        if(ta){{ta.value="{token}";ta.dispatchEvent(new Event('change',{{bubbles:true}}))}}
                        var cb=document.getElementById('g-recaptcha-response').getAttribute('data-callback');
                        if(cb&&typeof window[cb]==='function'){{window[cb]("{token}")}}
                    ''')
                except: pass
                return token
            if data.get("request")=="ERROR_CAPTCHA_UNSOLVABLE": return None
    return None

# ---------- Gmail Creator (unchanged) ----------
async def create_gmail_auto(username: str, password: str) -> str:
    activation = await buy_activation()
    phone = activation["phone"]
    activation_id = activation["id"]
    logger.info(f"Rented {phone} (activation {activation_id})")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox","--disable-setuid-sandbox",
                "--disable-dev-shm-usage","--disable-gpu"
            ])
            ctx_opts = {
                "viewport": {"width":1280,"height":800},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            }
            proxy = parse_proxy_url(PROXY_URL)
            if proxy: ctx_opts["proxy"] = proxy
            context = await browser.new_context(**ctx_opts)
            page = await context.new_page()

            await page.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
                Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
                window.chrome={runtime:{}};
                const oq=window.navigator.permissions.query;
                window.navigator.permissions.query=p=>p.name==='notifications'?Promise.resolve({state:Notification.permission}):oq(p);
            """)

            await page.goto("https://accounts.google.com/signup/v2/webcreateaccount?flowName=GlifWebSignIn&flowEntry=SignUp",
                            wait_until="networkidle", timeout=60000)
            await asyncio.sleep(random.uniform(2,3))

            await page.fill('input[name="firstName"]', "John")
            await page.fill('input[name="lastName"]', "Doe")
            await page.fill('input[name="Username"]', username)
            await page.fill('input[name="Passwd"]', password)
            await page.fill('input[name="ConfirmPasswd"]', password)
            await page.click('button:has-text("Next")')
            await page.wait_for_timeout(4000)

            for _ in range(2):
                if await page.is_visible('iframe[src*="google.com/recaptcha"]'):
                    logger.info("Captcha appeared, solving...")
                    token = await solve_captcha(page)
                    if token:
                        await page.click('button:has-text("Next")')
                        await page.wait_for_timeout(3000)
                        break
                    else:
                        await page.reload(wait_until="networkidle")
                        continue
                break

            await page.wait_for_selector('input[type="tel"]', timeout=30000)
            await page.fill('input[type="tel"]', phone)
            await page.click('button:has-text("Next")')

            logger.info("Waiting for SMS...")
            code = await get_sms(activation_id)
            logger.info(f"Got code: {code}")
            await page.wait_for_selector('input[type="tel"]', timeout=30000)
            await page.fill('input[type="tel"]', code)
            await page.click('button:has-text("Next")')

            try:
                await page.wait_for_selector('button:has-text("I agree")', timeout=5000)
                await page.click('button:has-text("I agree")')
                await page.wait_for_timeout(2000)
            except: pass
            try:
                await page.click('button:has-text("Next")', timeout=5000)
                await page.wait_for_timeout(2000)
            except: pass

            await page.wait_for_timeout(4000)
            await browser.close()

        return f"{username}@gmail.com:{password}"

    except Exception as e:
        await cancel_activation(activation_id)
        raise e

# ---------- Bot UI (Web App + fallback menu) ----------
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Create Email", callback_data="email_create")],
        [InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📞 Admin Contact", url="tg://user?id=Xricx0")],
        [InlineKeyboardButton("🔐 Login", callback_data="login")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a button to open the mini web app, plus the classic inline menu."""
    text = "👋 Fully automated Gmail creator.\nTap below to open the Web App, or use the inline menu:"
    
    buttons = []
    if WEB_APP_URL:
        buttons.append([InlineKeyboardButton("🚀 Open Creator", web_app=WebAppInfo(url=WEB_APP_URL))])
    buttons.extend(get_main_menu().inline_keyboard)
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives actions from the mini web app."""
    data = update.message.web_app_data.data
    user_id = update.effective_user.id

    # Same actions as your inline buttons
    if data == "email_create":
        await update.message.reply_text("Enter desired username (without @gmail.com):")
        return EMAIL_USERNAME
    elif data == "wallet":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await update.message.reply_text("Please /login first.")
            return
        await update.message.reply_text(f"💰 Balance: ₹{user['balance']:.2f}")
    elif data == "admin_contact":
        await update.message.reply_text("Contact admin: @Xricx0")
    elif data == "login":
        DB.execute("INSERT OR IGNORE INTO users (user_id,username,first_name) VALUES (?,?,?)",
                   (user_id, update.effective_user.username, update.effective_user.first_name))
        DB.commit()
        await update.message.reply_text("✅ Logged in!")
    else:
        await update.message.reply_text("Unknown action.")
    return ConversationHandler.END

# ---------- Inline button handler (unchanged) ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    if data == "login":
        DB.execute("INSERT OR IGNORE INTO users (user_id,username,first_name) VALUES (?,?,?)",
                   (uid, query.from_user.username, query.from_user.first_name))
        DB.commit()
        await query.edit_message_text("✅ Logged in!", reply_markup=get_main_menu())
    elif data == "wallet":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if not user:
            await query.answer("Login first", show_alert=True); return
        kbd = [[InlineKeyboardButton("📥 Deposit", callback_data="deposit")],
               [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await query.edit_message_text(f"💰 Balance: ₹{user['balance']:.2f}", reply_markup=InlineKeyboardMarkup(kbd))
    elif data == "deposit":
        user = DB.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if not user:
            await query.answer("Login first", show_alert=True); return
        await query.edit_message_text("Enter amount:")
        return DEPOSIT_AMOUNT
    elif data == "email_create":
        await query.edit_message_text("Enter desired username (without @gmail.com):")
        return EMAIL_USERNAME
    elif data == "main_menu":
        await query.edit_message_text("Main Menu:", reply_markup=get_main_menu())
    elif data.startswith("appdep_") or data.startswith("rejdep_"):
        await handle_admin_deposits(query, context, data)

# … rest of your handlers (handle_admin_deposits, deposit_amount, deposit_screenshot,
#   email_username, email_password, run_creation, main) remain exactly the same …

# ---------- Main (add web_app_data handler) ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    dep_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^deposit$")],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)],
            DEPOSIT_SCREENSHOT: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, deposit_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    )
    email_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^email_create$")],
        states={
            EMAIL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, email_username)],
            EMAIL_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, email_password)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(dep_conv)
    app.add_handler(email_conv)
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(login|wallet|main_menu|appdep_|rejdep_).*"))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data))
    app.run_polling()

if __name__ == "__main__":
    main()
