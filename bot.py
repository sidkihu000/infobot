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
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "6LcBR0EtAAAAAHihWHAE4fPcaFHLKHLWDAhIlciQ")
PROXY_URL = os.getenv("PROXY_URL", "")

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

# ---------- 5sim API (Your OTP Provider) ----------
SIM_API_BASE = "https://5sim.net/v1/user"

async def buy_activation() -> dict:
    """Buy a Google activation number. Returns {id, phone}."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{SIM_API_BASE}/buy/activation/google/any/any",
            headers={"Authorization": f"Bearer {SIMS_API_KEY}"}
        )
        data = resp.json()
        if "id" not in data:
            raise Exception(f"5sim buy failed: {data}")
        return {"id": data["id"], "phone": data["phone"]}

async def get_sms(activation_id: str) -> str:
    """Poll 5sim for SMS code up to 10 times (30s interval)."""
    for _ in range(10):
        await asyncio.sleep(30)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{SIM_API_BASE}/check/{activation_id}",
                headers={"Authorization": f"Bearer {SIMS_API_KEY}"}
            )
            data = resp.json()
            if data.get("status") == "RECEIVED" and data.get("sms"):
                return data["sms"][0]["code"]
    raise TimeoutError("SMS not received in 5 minutes")

async def cancel_activation(activation_id: str):
    """Cancel the activation to avoid wasting balance."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.get(
                f"{SIM_API_BASE}/cancel/{activation_id}",
                headers={"Authorization": f"Bearer {SIMS_API_KEY}"}
            )
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
        # create task
        r = await cl.get("https://2captcha.com/in.php", params={
            "key": CAPTCHA_API_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": url, "json": 1
        })
        res = r.json()
        if res.get("status")!=1: return None
        task_id = res["request"]
        # poll
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

# ---------- Gmail Creator (fully automated) ----------
async def create_gmail_auto(username: str, password: str) -> str:
    """Rent number via 5sim, solve captcha, get SMS, create account. Returns email:password."""
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

            # stealth
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

            # captcha
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

            # phone
            await page.wait_for_selector('input[type="tel"]', timeout=30000)
            await page.fill('input[type="tel"]', phone)
            await page.click('button:has-text("Next")')

            # wait for SMS
            logger.info("Waiting for SMS...")
            code = await get_sms(activation_id)
            logger.info(f"Got code: {code}")
            await page.wait_for_selector('input[type="tel"]', timeout=30000)
            await page.fill('input[type="tel"]', code)
            await page.click('button:has-text("Next")')

            # final steps
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

        # success
        return f"{username}@gmail.com:{password}"

    except Exception as e:
        await cancel_activation(activation_id)
        raise e

# ---------- Bot UI (unchanged) ----------
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Create Email", callback_data="email_create")],
        [InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📞 Admin Contact", url="tg://user?id=Xricx0")],
        [InlineKeyboardButton("🔐 Login", callback_data="login")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Fully automated Gmail creator.\nUse menu:", reply_markup=get_main_menu())

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

async def handle_admin_deposits(query, context, data):
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True); return
    action, dep_id = data.split("_")
    dep_id = int(dep_id)
    dep = DB.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not dep or dep["status"] != "pending":
        await query.edit_message_caption(caption="Already processed."); return
    if action == "appdep":
        DB.execute("UPDATE deposits SET status='approved' WHERE id=?", (dep_id,))
        DB.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (dep["amount"], dep["user_id"]))
        DB.commit()
        await query.edit_message_caption(caption=f"✅ Deposit #{dep_id} approved.")
        await context.bot.send_message(dep["user_id"], f"✅ ₹{dep['amount']} added!")
    else:
        DB.execute("UPDATE deposits SET status='rejected' WHERE id=?", (dep_id,))
        DB.commit()
        await query.edit_message_caption(caption=f"❌ Deposit #{dep_id} rejected.")
        await context.bot.send_message(dep["user_id"], "❌ Deposit rejected.")

# Conversation states
DEPOSIT_AMOUNT, DEPOSIT_SCREENSHOT = range(2)
EMAIL_USERNAME, EMAIL_PASSWORD = range(2)

async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
    except:
        await update.message.reply_text("Invalid number"); return DEPOSIT_AMOUNT
    context.user_data["deposit_amount"] = amt
    if os.path.exists("qr_code.jpg"):
        with open("qr_code.jpg", "rb") as f:
            await update.message.reply_photo(f, caption=f"Scan ₹{amt}. Send screenshot.")
    else:
        await update.message.reply_text(f"Transfer ₹{amt} and send TXID.")
    return DEPOSIT_SCREENSHOT

async def deposit_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    amt = context.user_data.get("deposit_amount",0)
    file_id = update.message.photo[-1].file_id if update.message.photo else update.message.text
    cur = DB.execute("INSERT INTO deposits (user_id,amount,screenshot_file_id) VALUES (?,?,?)", (uid,amt,file_id))
    dep_id = cur.lastrowid
    DB.commit()
    admin_kbd = [[InlineKeyboardButton("✅ Approve", callback_data=f"appdep_{dep_id}"),
                  InlineKeyboardButton("❌ Reject", callback_data=f"rejdep_{dep_id}")]]
    msg = f"Deposit #{dep_id} by {uid}\nAmount: ₹{amt}"
    if update.message.photo:
        await context.bot.send_photo(ADMIN_ID, file_id, caption=msg, reply_markup=InlineKeyboardMarkup(admin_kbd))
    else:
        await context.bot.send_message(ADMIN_ID, f"{msg}\nTxID: {file_id}", reply_markup=InlineKeyboardMarkup(admin_kbd))
    await update.message.reply_text("✅ Submitted. Admin will verify.", reply_markup=get_main_menu())
    return ConversationHandler.END

# Email creation conversation (only 2 steps)
async def email_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usr = update.message.text.strip()
    if not re.match(r'^[a-zA-Z0-9._]{6,30}$', usr):
        await update.message.reply_text("Invalid username (6-30 chars)."); return EMAIL_USERNAME
    context.user_data["desired_email"] = usr
    await update.message.reply_text("Enter password (min 8 chars):")
    return EMAIL_PASSWORD

async def email_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    if len(pwd) < 8:
        await update.message.reply_text("Too short (min 8)."); return EMAIL_PASSWORD
    uid = update.effective_user.id
    desired = context.user_data["desired_email"]
    cost = 10.0

    user = DB.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    if not user:
        await update.message.reply_text("❌ Login first.", reply_markup=get_main_menu()); return ConversationHandler.END
    if user["balance"] < cost:
        await update.message.reply_text(f"❌ Insufficient balance (₹{cost}). Deposit please.", reply_markup=get_main_menu()); return ConversationHandler.END

    # Deduct & insert
    DB.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (cost, uid))
    DB.execute("INSERT INTO email_orders (user_id,desired_email,password,cost) VALUES (?,?,?,?)",
               (uid, desired, pwd, cost))
    DB.commit()

    msg = await update.message.reply_text("⏳ Creating your Gmail... (this takes ~2 min)")
    asyncio.create_task(run_creation(context.bot, update.effective_chat.id, msg.message_id, uid, desired, pwd))
    return ConversationHandler.END

async def run_creation(bot, chat_id, msg_id, uid, email, pwd):
    try:
        creds = await create_gmail_auto(email, pwd)
        DB.execute("UPDATE email_orders SET status='completed' WHERE desired_email=? AND user_id=?", (email, uid))
        DB.commit()
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                    text=f"✅ Created:\n`{creds}`", parse_mode="Markdown")
    except Exception as e:
        logger.exception("Auto creation failed")
        DB.execute("UPDATE users SET balance = balance + 10 WHERE user_id=?", (uid,))
        DB.execute("UPDATE email_orders SET status='failed' WHERE desired_email=? AND user_id=?", (email, uid))
        DB.commit()
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                    text=f"❌ Failed: {str(e)[:200]}\nRefunded ₹10.")

# ---------- Main ----------
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
    app.run_polling()

if __name__ == "__main__":
    main()
