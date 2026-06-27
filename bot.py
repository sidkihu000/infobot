import asyncio
import logging
import os
import re
import random
import sqlite3
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ---------- Load environment ----------
load_dotenv()
BOT_TOKEN = "6067177575:AAEUVOteOiERUHE5v75iudEdHAGiCRXBGus"
ADMIN_ID = int(os.getenv("ADMIN_ID", "2119464081"))
SMS_API_KEY = os.getenv("SMS_API_KEY", "eyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9...")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "6LczKzgtAAAAAHjfrXwbQghhKiCOpYfmNhNMi9Nf")
PROXY_URL = os.getenv("PROXY_URL", "")

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO   # Change to logging.DEBUG for more detail
)
logger = logging.getLogger(__name__)

# ---------- Database ----------
DB = sqlite3.connect("bot_data.db", check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    balance REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    screenshot_file_id TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS email_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    desired_email TEXT,
    password TEXT,
    status TEXT DEFAULT 'processing',
    cost REAL DEFAULT 10.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
DB.commit()

# ---------- Number Panel (5sim) Helpers ----------
SIM_API_BASE = "https://5sim.net/v1/user"

async def buy_activation() -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SIM_API_BASE}/buy/activation/google/any/any",
                                headers={"Authorization": f"Bearer {SMS_API_KEY}"})
        data = resp.json()
        if "id" not in data:
            logger.error(f"5sim buy response: {data}")
            raise Exception("No numbers available")
        return {"id": data["id"], "phone": data["phone"]}

async def get_sms(activation_id: str) -> str:
    for _ in range(10):
        await asyncio.sleep(20)
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{SIM_API_BASE}/check/{activation_id}",
                                    headers={"Authorization": f"Bearer {SMS_API_KEY}"})
            data = resp.json()
            if data.get("status") == "RECEIVED" and data.get("sms"):
                return data["sms"][0]["code"]
    raise TimeoutError("SMS not received in time")

async def cancel_activation(activation_id: str):
    async with httpx.AsyncClient() as client:
        await client.get(f"{SIM_API_BASE}/cancel/{activation_id}",
                         headers={"Authorization": f"Bearer {SMS_API_KEY}"})

# ---------- CAPTCHA Solver (2Captcha) with enhanced injection ----------
async def solve_captcha(page) -> str:
    if not CAPTCHA_API_KEY:
        logger.warning("CAPTCHA_API_KEY not set – cannot solve captcha")
        return None

    url = page.url
    sitekey = await page.evaluate('''() => {
        const iframes = document.querySelectorAll('iframe');
        for (let f of iframes) {
            const src = f.src;
            if (src.includes('google.com/recaptcha')) {
                const match = src.match(/[?&]k=([^&]+)/);
                if (match) return match[1];
            }
        }
        const elems = document.querySelectorAll('[data-sitekey]');
        if (elems.length > 0) return elems[0].getAttribute('data-sitekey');
        return null;
    }''')
    if not sitekey:
        logger.info("No reCAPTCHA sitekey found")
        return None

    async with httpx.AsyncClient() as client:
        # Create 2Captcha task
        resp = await client.get("https://2captcha.com/in.php", params={
            "key": CAPTCHA_API_KEY,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": url,
            "json": 1,
        })
        result = resp.json()
        if result.get("status") != 1:
            logger.error(f"2Captcha create error: {result}")
            return None
        task_id = result["request"]

        # Poll for solution
        for _ in range(20):
            await asyncio.sleep(5)
            resp = await client.get("https://2captcha.com/res.php", params={
                "key": CAPTCHA_API_KEY,
                "action": "get",
                "id": task_id,
                "json": 1,
            })
            data = resp.json()
            if data.get("status") == 1:
                token = data["request"]
                # Inject token and trigger callback
                await page.evaluate(f'''
                    const textarea = document.getElementById('g-recaptcha-response');
                    if (textarea) textarea.value = "{token}";
                    const callback = document.getElementById('g-recaptcha-response').getAttribute('data-callback');
                    if (callback && typeof window[callback] === 'function') {{
                        window[callback]("{token}");
                    }} else {{
                        // Try to submit the form
                        const form = document.querySelector('form');
                        if (form) form.requestSubmit();
                    }}
                ''')
                logger.info("CAPTCHA solved and token injected")
                return token
            if data.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                logger.error("Captcha unsolvable")
                return None
    return None

# ---------- Gmail Creation Engine (fully enhanced) ----------
async def create_gmail_account(desired_username: str, password: str) -> str:
    activation = await buy_activation()
    phone_number = activation["phone"]
    activation_id = activation["id"]
    logger.info(f"Got phone: {phone_number} (ID: {activation_id})")

    try:
        async with async_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
            browser = await p.chromium.launch(headless=True, args=launch_args)

            context_options = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            if PROXY_URL:
                context_options["proxy"] = {"server": PROXY_URL}
            context = await browser.new_context(**context_options)
            page = await context.new_page()

            # Stealth injection
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
            """)

            # Human-like initial delay
            await asyncio.sleep(random.uniform(2, 4))

            # Go to signup
            await page.goto("https://accounts.google.com/signup/v2/webcreateaccount?flowName=GlifWebSignIn&flowEntry=SignUp", wait_until="networkidle")
            logger.info("Signup page loaded")

            # Fill in form with random pauses
            await page.fill('input[name="firstName"]', "John")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.fill('input[name="lastName"]', "Doe")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.fill('input[name="Username"]', desired_username)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.fill('input[name="Passwd"]', password)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.fill('input[name="ConfirmPasswd"]', password)
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # Move mouse randomly
            await page.mouse.move(random.randint(100, 500), random.randint(100, 400))

            # Click Next button (ensure it's enabled)
            await page.click('button:has-text("Next"):not([disabled])')
            logger.info("Clicked Next after form fill")
            await page.wait_for_timeout(4000)

            # CAPTCHA handling (retry once)
            captcha_attempts = 0
            while captcha_attempts < 2:
                if await page.is_visible('iframe[src*="google.com/recaptcha"]'):
                    logger.info("CAPTCHA detected, solving...")
                    token = await solve_captcha(page)
                    if token:
                        await page.click('button:has-text("Next"):not([disabled])')
                        await page.wait_for_timeout(3000)
                    else:
                        logger.warning("CAPTCHA solving failed, refreshing page")
                        await page.reload(wait_until="networkidle")
                        captcha_attempts += 1
                        continue
                break

            # Phone entry
            phone_input = await page.wait_for_selector('input[type="tel"]', timeout=30000)
            await phone_input.fill(phone_number)
            await page.click('button:has-text("Next"):not([disabled])')
            logger.info("Phone number submitted")

            # Wait for SMS code
            code = await get_sms(activation_id)
            logger.info(f"Got SMS code: {code}")
            code_input = await page.wait_for_selector('input[type="tel"]', timeout=30000)
            await code_input.fill(code)
            await page.click('button:has-text("Next"):not([disabled])')

            # Agree to terms / skip recovery
            try:
                await page.wait_for_selector('button:has-text("I agree")', timeout=5000)
                await page.click('button:has-text("I agree"):not([disabled])')
                await page.wait_for_timeout(2000)
            except:
                pass
            try:
                await page.click('button:has-text("Next"):not([disabled])', timeout=5000)
                await page.wait_for_timeout(2000)
            except:
                pass

            await page.wait_for_timeout(5000)
            await browser.close()
            logger.info("Browser closed successfully")

        # Finalize activation
        async with httpx.AsyncClient() as client:
            await client.get(f"{SIM_API_BASE}/finish/{activation_id}",
                             headers={"Authorization": f"Bearer {SMS_API_KEY}"})
            logger.info("Activation finished")

        return f"{desired_username}@gmail.com:{password}"

    except Exception as e:
        logger.exception("Exception during Gmail creation")
        # Try to save screenshot if page still exists
        try:
            if 'page' in locals():
                await page.screenshot(path="error_screenshot.png")
                logger.error("Error screenshot saved to error_screenshot.png")
        except:
            pass
        # Cancel activation to avoid waste
        await cancel_activation(activation_id)
        raise e

# ---------- UI Animation ----------
async def loading_animation(bot, chat_id, message_id, stop_event: asyncio.Event):
    frames = [
        "🕛 Creating account...",
        "🕒 Processing details...",
        "🕕 Fetching number...",
        "🕘 Verifying SMS..."
    ]
    try:
        while not stop_event.is_set():
            for frame in frames:
                if stop_event.is_set():
                    break
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=frame)
                await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Animation error: {e}")

# ---------- Bot Handlers (unchanged, but with enhanced logging) ----------
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Email Create", callback_data="email_create")],
        [InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📞 Admin Contact", url="tg://user?id=Xricx0")],
        [InlineKeyboardButton("🔐 Login", callback_data="login")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Welcome to Gmail Creator Bot!\nUse the buttons below:",
                                    reply_markup=get_main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "login":
        DB.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
                   (user_id, query.from_user.username, query.from_user.first_name))
        DB.commit()
        await query.edit_message_text("✅ Logged in successfully! Use the menu.",
                                      reply_markup=get_main_menu())

    elif data == "wallet":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Please login first.", show_alert=True)
            return
        keyboard = [
            [InlineKeyboardButton("📥 Deposit", callback_data="deposit")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
        ]
        await query.edit_message_text(f"💰 Your Balance: ₹{user['balance']:.2f}",
                                      reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "deposit":
        user = DB.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Please login first.", show_alert=True)
            return
        await query.edit_message_text("Send the amount you want to deposit (numeric, e.g., 100):")
        return DEPOSIT_AMOUNT

    elif data == "email_create":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Please login first.", show_alert=True)
            return
        if user["balance"] < 10:
            await query.answer("Insufficient balance. Minimum ₹10 required.", show_alert=True)
            return
        await query.edit_message_text("Enter desired email username (without @gmail.com):")
        return EMAIL_USERNAME

    elif data == "main_menu":
        await query.edit_message_text("Main Menu:", reply_markup=get_main_menu())

    elif data.startswith("appdep_") or data.startswith("rejdep_"):
        await handle_admin_deposits(query, context, data)

async def handle_admin_deposits(query, context, data):
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    action, deposit_id = data.split("_")
    deposit_id = int(deposit_id)
    dep = DB.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()

    if not dep or dep["status"] != "pending":
        await query.edit_message_caption(caption="Already processed.")
        return

    if action == "appdep":
        DB.execute("UPDATE deposits SET status='approved' WHERE id=?", (deposit_id,))
        DB.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (dep["amount"], dep["user_id"]))
        DB.commit()
        await query.edit_message_caption(caption=f"✅ Deposit #{deposit_id} approved.")
        await context.bot.send_message(dep["user_id"], f"✅ Your deposit of ₹{dep['amount']} has been approved.")
    elif action == "rejdep":
        DB.execute("UPDATE deposits SET status='rejected' WHERE id=?", (deposit_id,))
        DB.commit()
        await query.edit_message_caption(caption=f"❌ Deposit #{deposit_id} rejected.")
        await context.bot.send_message(dep["user_id"], "❌ Your deposit was rejected. Contact admin.")

# ---------- Deposit Conversation ----------
DEPOSIT_AMOUNT, DEPOSIT_SCREENSHOT = range(2)

async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text)
    except ValueError:
        await update.message.reply_text("Please send a valid numeric amount.")
        return DEPOSIT_AMOUNT

    context.user_data["deposit_amount"] = amount

    if os.path.exists("qr_code.jpg"):
        with open("qr_code.jpg", "rb") as f:
            await update.message.reply_photo(f, caption=f"Scan to pay ₹{amount}. Then send screenshot or transaction ID.")
    else:
        await update.message.reply_text(f"Please transfer ₹{amount} and reply with the Transaction ID.")
    return DEPOSIT_SCREENSHOT

async def deposit_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    amount = context.user_data.get("deposit_amount", 0)
    file_id = update.message.photo[-1].file_id if update.message.photo else update.message.text

    cursor = DB.execute("INSERT INTO deposits (user_id, amount, screenshot_file_id) VALUES (?,?,?)",
                        (user_id, amount, file_id))
    deposit_id = cursor.lastrowid
    DB.commit()

    admin_keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"appdep_{deposit_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"rejdep_{deposit_id}")]
    ]

    msg_text = f"Deposit #{deposit_id} by {user_id}\nAmount: ₹{amount}"
    if update.message.photo:
        await context.bot.send_photo(ADMIN_ID, file_id, caption=msg_text,
                                     reply_markup=InlineKeyboardMarkup(admin_keyboard))
    else:
        await context.bot.send_message(ADMIN_ID, f"{msg_text}\nTxID: {file_id}",
                                       reply_markup=InlineKeyboardMarkup(admin_keyboard))

    await update.message.reply_text("✅ Deposit submitted. Admin will verify shortly.",
                                    reply_markup=get_main_menu())
    return ConversationHandler.END

# ---------- Email Creation Conversation ----------
EMAIL_USERNAME, EMAIL_PASSWORD = range(2)

async def email_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = update.message.text.strip()
    if not re.match(r"^[a-zA-Z0-9._]{6,30}$", username):
        await update.message.reply_text("Invalid username. Use 6-30 letters, numbers, dots.")
        return EMAIL_USERNAME
    context.user_data["desired_email"] = username
    await update.message.reply_text("Enter the password for the account (min 8 chars):")
    return EMAIL_PASSWORD

async def email_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    if len(password) < 8:
        await update.message.reply_text("Password too short, min 8 characters. Try again:")
        return EMAIL_PASSWORD

    user_id = update.effective_user.id
    desired = context.user_data["desired_email"]
    cost = 10.0

    DB.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (cost, user_id))
    DB.execute("INSERT INTO email_orders (user_id, desired_email, password, cost) VALUES (?,?,?,?)",
               (user_id, desired, password, cost))
    DB.commit()

    msg = await update.message.reply_text("⏳ Initializing setup...")

    stop_event = asyncio.Event()
    bot_instance = context.bot
    asyncio.create_task(loading_animation(bot_instance, update.effective_chat.id, msg.message_id, stop_event))
    asyncio.create_task(create_and_notify(bot_instance, update.effective_chat.id, msg.message_id,
                                          user_id, desired, password, stop_event))

    return ConversationHandler.END

async def create_and_notify(bot, chat_id, message_id, user_id: int, email: str, pwd: str,
                            stop_event: asyncio.Event):
    try:
        credentials = await create_gmail_account(email, pwd)
        DB.execute("UPDATE email_orders SET status='completed' WHERE desired_email=? AND user_id=?",
                   (email, user_id))
        DB.commit()
        stop_event.set()
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    text=f"✅ Account created:\n`{credentials}`",
                                    parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Creation failed for {email}: {e}")
        stop_event.set()
        # Refund
        DB.execute("UPDATE users SET balance = balance + 10 WHERE user_id=?", (user_id,))
        DB.execute("UPDATE email_orders SET status='failed' WHERE desired_email=? AND user_id=?",
                   (email, user_id))
        DB.commit()
        # Send error message with first 200 chars of exception
        error_msg = str(e)[:200]
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    text=f"❌ Creation failed: {error_msg}. Refunded ₹10.")

# ---------- Main ----------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^deposit$")],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)],
            DEPOSIT_SCREENSHOT: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, deposit_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    email_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^email_create$")],
        states={
            EMAIL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, email_username)],
            EMAIL_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, email_password)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(deposit_conv)
    application.add_handler(email_conv)
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(login|wallet|main_menu|appdep_|rejdep_).*"))

    application.run_polling()

if __name__ == "__main__":
    main()
