import asyncio
import logging
import os
import re
import random
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

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
# SMSPool API key - get from https://smspool.net/account/api
SMSPOOL_API_KEY = os.getenv("SMSPOOL_API_KEY", "6xACy0wYLn4Sz548sTBeC216IEZ2OICB")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "6LczKzgtAAAAAHjfrXwbQghhKiCOpYfmNhNMi9Nf")
PROXY_URL = os.getenv("PROXY_URL", "")  # Optional residential proxy

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
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

# ---------- Proxy parser ----------
def parse_proxy_url(url: str) -> dict | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
        if parsed.username:
            config["username"] = parsed.username
        if parsed.password:
            config["password"] = parsed.password
        return config
    except Exception:
        return None

# ---------- Safe JSON helper ----------
async def safe_json_response(response) -> dict:
    text = await response.text()
    if not text.strip():
        logger.error(f"Empty response from {response.url}")
        raise Exception("API returned empty response")
    try:
        return response.json()
    except Exception:
        logger.error(f"Invalid JSON from {response.url}: {text[:500]}")
        raise Exception("API returned invalid JSON")

# =====================================================================
#  SMSPool.NET Integration (Replaces 5sim)
# =====================================================================
SMSPOOL_BASE = "https://api.smspool.net"

async def rent_smspool_number() -> dict:
    """
    Rent a USA number for Google verification.
    Returns { "order_id": "abc123", "number": "+1 234 567 8900" }
    """
    params = {
        "key": SMSPOOL_API_KEY,
        "country": "United States",   # Best success rate for Google
        "service": "Google",
        "pool": "1",                  # 1 = fast pool
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{SMSPOOL_BASE}/purchase/1", data=params)
        data = await safe_json_response(resp)
        if data.get("success") != 1:
            logger.error(f"SMSPool rent error: {data}")
            raise Exception(f"Failed to rent number: {data.get('message', 'Unknown error')}")
        return {
            "order_id": data["order_id"],
            "number": data["number"].replace(" ", ""),  # remove spaces
        }

async def get_smspool_sms(order_id: str) -> str:
    """Poll for SMS code up to 15 times (20s interval)"""
    for _ in range(15):
        await asyncio.sleep(20)
        params = {
            "key": SMSPOOL_API_KEY,
            "orderid": order_id,
            "smstype": "1",  # Get only first SMS
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{SMSPOOL_BASE}/sms/1", params=params)
            data = await safe_json_response(resp)
            if data.get("status") == 1 and data.get("sms"):
                # SMSPool returns number in format "+1234567890"
                sms_text = data["sms"]
                # Extract OTP code (usually 6 digits)
                match = re.search(r'(\d{4,8})', sms_text)
                if match:
                    return match.group(1)
                return sms_text  # fallback – return whole SMS
    raise TimeoutError("SMS not received within 5 minutes")

async def cancel_smspool_number(order_id: str):
    """Cancel the rented number to save money"""
    params = {
        "key": SMSPOOL_API_KEY,
        "orderid": order_id,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            await client.get(f"{SMSPOOL_BASE}/cancel/1", params=params)
            logger.info(f"Cancelled SMSPool order {order_id}")
        except Exception as e:
            logger.warning(f"Failed to cancel SMSPool order {order_id}: {e}")

# ---------- CAPTCHA Solver (2Captcha) ----------
async def solve_captcha(page) -> str:
    if not CAPTCHA_API_KEY or "your_2captcha" in CAPTCHA_API_KEY:
        logger.warning("CAPTCHA_API_KEY not set or using placeholder")
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

    async with httpx.AsyncClient(timeout=120) as client:
        # Create task
        params = {
            "key": CAPTCHA_API_KEY,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": url,
            "json": 1,
        }
        resp = await client.get("https://2captcha.com/in.php", params=params)
        result = await safe_json_response(resp)
        if result.get("status") != 1:
            logger.error(f"2Captcha create error: {result}")
            return None
        task_id = result["request"]
        logger.info(f"2Captcha task created: {task_id}")

        # Poll for solution
        for attempt in range(30):
            await asyncio.sleep(10)
            params = {
                "key": CAPTCHA_API_KEY,
                "action": "get",
                "id": task_id,
                "json": 1,
            }
            resp = await client.get("https://2captcha.com/res.php", params=params)
            data = await safe_json_response(resp)
            if data.get("status") == 1:
                token = data["request"]
                logger.info("CAPTCHA solved successfully")
                # Inject token into page
                try:
                    await page.evaluate(f'''
                        var textarea = document.getElementById('g-recaptcha-response');
                        if (textarea) {{
                            textarea.value = "{token}";
                            textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                        var callback = document.getElementById('g-recaptcha-response').getAttribute('data-callback');
                        if (callback && typeof window[callback] === 'function') {{
                            window[callback]("{token}");
                        }}
                    ''')
                    logger.info("CAPTCHA token injected")
                except Exception as e:
                    logger.exception("CAPTCHA injection error")
                return token
            if data.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                logger.error("CAPTCHA marked unsolvable by 2Captcha")
                return None
    return None

# =====================================================================
#  Gmail Creation Engine (Hybrid: SMSPool + Playwright)
# =====================================================================
async def create_gmail_account(desired_username: str, password: str) -> str:
    """
    Creates a Gmail account using:
    1. SMSPool for phone verification
    2. Playwright for form automation
    3. 2Captcha for CAPTCHA solving
    4. Optional proxy for IP rotation
    """
    # Step 1: Rent a phone number from SMSPool
    rental = await rent_smspool_number()
    phone_number = rental["number"]
    order_id = rental["order_id"]
    logger.info(f"Rented phone number from SMSPool: {phone_number} (Order: {order_id})")

    try:
        async with async_playwright() as p:
            # Browser launch with stealth arguments
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
            browser = await p.chromium.launch(headless=True, args=launch_args)

            context_options = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }

            # Apply proxy if configured
            proxy_config = parse_proxy_url(PROXY_URL)
            if proxy_config:
                context_options["proxy"] = proxy_config
                logger.info(f"Using proxy: {proxy_config['server']}")

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

            # Navigate to Google signup
            logger.info("Navigating to Google signup page...")
            await page.goto(
                "https://accounts.google.com/signup/v2/webcreateaccount?flowName=GlifWebSignIn&flowEntry=SignUp",
                wait_until="networkidle",
                timeout=60000
            )
            await asyncio.sleep(random.uniform(2, 4))
            logger.info("Signup page loaded")

            # Fill personal info
            await page.fill('input[name="firstName"]', "John")
            await asyncio.sleep(random.uniform(0.8, 1.5))
            await page.fill('input[name="lastName"]', "Smith")
            await asyncio.sleep(random.uniform(0.8, 1.5))
            logger.info(f"Filling username: {desired_username}")

            # Fill username
            await page.fill('input[name="Username"]', desired_username)
            await asyncio.sleep(random.uniform(1, 2))

            # Fill password
            await page.fill('input[name="Passwd"]', password)
            await asyncio.sleep(random.uniform(0.5, 1))
            await page.fill('input[name="ConfirmPasswd"]', password)
            await asyncio.sleep(random.uniform(0.5, 1))

            # Click Next
            await page.click('button:has-text("Next")')
            await page.wait_for_timeout(4000)
            logger.info("Clicked Next after form fill")

            # CAPTCHA handling
            for attempt in range(2):
                if await page.is_visible('iframe[src*="google.com/recaptcha"]'):
                    logger.info(f"CAPTCHA detected (attempt {attempt + 1}/2)...")
                    token = await solve_captcha(page)
                    if token:
                        await page.click('button:has-text("Next")')
                        await page.wait_for_timeout(3000)
                        break
                    else:
                        logger.warning("CAPTCHA solving failed, refreshing...")
                        await page.reload(wait_until="networkidle")
                        await asyncio.sleep(3)
                        continue
                else:
                    logger.info("No CAPTCHA detected")
                    break

            # Phone number entry
            logger.info(f"Entering phone number: {phone_number}")
            try:
                phone_input = await page.wait_for_selector('input[type="tel"]', timeout=30000)
                await phone_input.fill(phone_number)
                await page.click('button:has-text("Next")')
                logger.info("Phone number submitted")
            except Exception as e:
                logger.error(f"Phone entry failed: {e}")
                # Try alternative selector
                phone_input = await page.wait_for_selector('input[type="tel"], input[aria-label*="phone"]', timeout=10000)
                await phone_input.fill(phone_number)
                await page.click('button:has-text("Next")')

            # Wait for SMS code from SMSPool
            logger.info("Waiting for SMS code from SMSPool...")
            code = await get_smspool_sms(order_id)
            logger.info(f"Received SMS code: {code}")

            # Enter verification code
            code_input = await page.wait_for_selector('input[type="tel"]', timeout=30000)
            await code_input.fill(code)
            await page.click('button:has-text("Next")')
            logger.info("Verification code submitted")

            # Handle post-verification screens
            try:
                # "I agree" button (privacy/terms)
                await page.wait_for_selector('button:has-text("I agree")', timeout=5000)
                await page.click('button:has-text("I agree")')
                await page.wait_for_timeout(2000)
                logger.info("Clicked 'I agree'")
            except Exception:
                logger.info("No 'I agree' button")

            try:
                # Final Next button
                await page.wait_for_selector('button:has-text("Next")', timeout=5000)
                await page.click('button:has-text("Next")')
                await page.wait_for_timeout(3000)
                logger.info("Clicked final Next")
            except Exception:
                logger.info("No final Next button")

            # Check if account was created successfully
            await page.wait_for_timeout(5000)
            current_url = page.url
            if "myaccount.google.com" in current_url or "accounts.google.com/signin" in current_url:
                logger.info("Account creation successful!")
            else:
                logger.warning(f"Unexpected URL after creation: {current_url}")

            await browser.close()
            logger.info("Browser closed")

        # Finalize SMSPool order
        logger.info(f"Gmail account created: {desired_username}@gmail.com")
        return f"{desired_username}@gmail.com:{password}"

    except Exception as e:
        logger.exception("Gmail creation failed")
        # Cancel SMSPool number to avoid charges
        await cancel_smspool_number(order_id)
        raise e

# ---------- UI Animation ----------
async def loading_animation(bot, chat_id, message_id, stop_event: asyncio.Event):
    frames = [
        "🕛 Creating account...",
        "🕒 Processing details...",
        "🕕 Renting phone number...",
        "🕘 Verifying SMS code..."
    ]
    try:
        while not stop_event.is_set():
            for frame in frames:
                if stop_event.is_set():
                    break
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=frame)
                await asyncio.sleep(1.2)
    except Exception as e:
        logger.error(f"Animation error: {e}")

# ---------- Bot Handlers ----------
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

    user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        await update.message.reply_text("❌ You are not logged in. Please login first.", reply_markup=get_main_menu())
        return ConversationHandler.END
    if user["balance"] < cost:
        await update.message.reply_text(f"❌ Insufficient balance. You need ₹{cost}. Please deposit.", reply_markup=get_main_menu())
        return ConversationHandler.END

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
                                    text=f"✅ Account created successfully:\n`{credentials}`",
                                    parse_mode="Markdown")
    except Exception as e:
        logger.exception("Account creation failed")
        stop_event.set()
        DB.execute("UPDATE users SET balance = balance + 10 WHERE user_id=?", (user_id,))
        DB.execute("UPDATE email_orders SET status='failed' WHERE desired_email=? AND user_id=?",
                   (email, user_id))
        DB.commit()
        error_msg = str(e)[:200]
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    text=f"❌ Creation failed: {error_msg}\nRefunded ₹10.",
                                    reply_markup=get_main_menu())

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
