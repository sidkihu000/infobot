import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime
from io import BytesIO

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

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SMS_API_KEY = os.getenv("SMS_API_KEY")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY")

# States for conversations
DEPOSIT_AMOUNT, DEPOSIT_SCREENSHOT = range(2)
EMAIL_USERNAME, EMAIL_PASSWORD = range(2)

# Database setup
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

# Enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# ---------- Number Panel (5sim) Helpers (using httpx) ----------
SIM_API_BASE = "https://5sim.net/v1/user"

async def get_balance() -> float:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SIM_API_BASE}/profile",
                                headers={"Authorization": f"Bearer {SMS_API_KEY}"})
        data = resp.json()
        return float(data.get("balance", 0))

async def buy_activation() -> dict:
    """Buy a Google activation, return {id, phone}"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SIM_API_BASE}/buy/activation/google/any/any",
                                headers={"Authorization": f"Bearer {SMS_API_KEY}"})
        data = resp.json()
        if "id" not in data:
            raise Exception("No numbers available")
        return {"id": data["id"], "phone": data["phone"]}

async def get_sms(activation_id: str) -> str:
    """Poll for SMS code up to 10 times (20s interval)"""
    for _ in range(10):
        await asyncio.sleep(20)
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{SIM_API_BASE}/check/{activation_id}",
                                    headers={"Authorization": f"Bearer {SMS_API_KEY}"})
            data = resp.json()
            if data.get("status") == "RECEIVED" and data.get("sms"):
                return data["sms"][0]["code"]
    raise TimeoutError("SMS not received")

async def cancel_activation(activation_id: str):
    async with httpx.AsyncClient() as client:
        await client.get(f"{SIM_API_BASE}/cancel/{activation_id}",
                         headers={"Authorization": f"Bearer {SMS_API_KEY}"})

# ---------- Gmail Creation Engine (Playwright) ----------
async def solve_captcha(page) -> None:
    """Placeholder – integrate 2captcha here. For now we just log."""
    logging.info("CAPTCHA solving needed (not implemented)")

async def create_gmail_account(desired_username: str, password: str) -> str:
    """
    Automates Gmail sign-up. Returns 'email@gmail.com:password'.
    Uses Playwright + 5sim for phone verification.
    """
    activation = await buy_activation()
    phone_number = activation["phone"]
    activation_id = activation["id"]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)  # set headless=False for debug
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            # Go to sign-up
            await page.goto("https://accounts.google.com/signup/v2/webcreateaccount?flowName=GlifWebSignIn&flowEntry=SignUp")
            await page.wait_for_load_state("networkidle")

            # Fill name
            await page.fill('input[name="firstName"]', "John")
            await page.fill('input[name="lastName"]', "Doe")

            # Fill username
            await page.fill('input[name="Username"]', desired_username)
            # Fill password
            await page.fill('input[name="Passwd"]', password)
            await page.fill('input[name="ConfirmPasswd"]', password)

            # Click Next
            await page.click('button:has-text("Next")')
            await page.wait_for_timeout(3000)

            # Phone entry
            phone_input = await page.wait_for_selector('input[type="tel"]', timeout=15000)
            await phone_input.fill(phone_number)
            await page.click('button:has-text("Next")')

            # Wait for code retrieval
            code = await get_sms(activation_id)
            code_input = await page.wait_for_selector('input[type="tel"]', timeout=15000)
            await code_input.fill(code)
            await page.click('button:has-text("Next")')

            # Finish setup (skipping recovery options, etc.)
            try:
                await page.click('button:has-text("I agree")')  # privacy
                await page.click('button:has-text("Next")')     # final steps
            except:
                pass

            await page.wait_for_timeout(5000)
            await browser.close()

        # Report success to 5sim (finish activation)
        async with httpx.AsyncClient() as client:
            await client.get(f"{SIM_API_BASE}/finish/{activation_id}",
                             headers={"Authorization": f"Bearer {SMS_API_KEY}"})

        return f"{desired_username}@gmail.com:{password}"

    except Exception as e:
        await cancel_activation(activation_id)
        raise e

# ---------- Bot Handlers (unchanged logic) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📧 Email Create", callback_data="email_create")],
        [InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📞 Admin Contact", url="tg://user?id=Xricx0")],
        [InlineKeyboardButton("🔐 Login", callback_data="login")],
    ]
    await update.message.reply_text(
        "Welcome to Gmail Creator Bot!\nUse the buttons below:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "login":
        DB.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
                   (user_id, query.from_user.username, query.from_user.first_name))
        DB.commit()
        await query.edit_message_text("✅ Logged in successfully! Use the menu.")

    elif data == "wallet":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Please /login first.", show_alert=True)
            return
        keyboard = [
            [InlineKeyboardButton("📥 Deposit", callback_data="deposit")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
        ]
        await query.edit_message_text(
            f"💰 Your Balance: ₹{user['balance']:.2f}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "deposit":
        user = DB.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Please /login first.", show_alert=True)
            return
        await query.edit_message_text("Send the amount you want to deposit (numeric, e.g., 100):")
        return DEPOSIT_AMOUNT

    elif data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("📧 Email Create", callback_data="email_create")],
            [InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
            [InlineKeyboardButton("📞 Admin Contact", url="tg://user?id=Xricx0")],
            [InlineKeyboardButton("🔐 Login", callback_data="login")],
        ]
        await query.edit_message_text("Main Menu:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "email_create":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Please /login first.", show_alert=True)
            return
        if user["balance"] < 10:
            await query.answer("Insufficient balance. Deposit first.", show_alert=True)
            return
        await query.edit_message_text("Enter desired email username (without @gmail.com):")
        return EMAIL_USERNAME

    elif data.startswith("appdep_"):
        # Admin approval
        if query.from_user.id != ADMIN_ID:
            await query.answer("Unauthorized", show_alert=True)
            return
        deposit_id = int(data.split("_")[1])
        dep = DB.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        if not dep or dep["status"] != "pending":
            await query.edit_message_caption(caption="Already processed.")
            return
        DB.execute("UPDATE deposits SET status='approved' WHERE id=?", (deposit_id,))
        DB.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (dep["amount"], dep["user_id"]))
        DB.commit()
        await query.edit_message_caption(caption=f"✅ Deposit #{deposit_id} approved.")
        await context.bot.send_message(dep["user_id"], f"✅ Your deposit of ₹{dep['amount']} has been approved and credited.")

    elif data.startswith("rejdep_"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Unauthorized", show_alert=True)
            return
        deposit_id = int(data.split("_")[1])
        DB.execute("UPDATE deposits SET status='rejected' WHERE id=?", (deposit_id,))
        DB.commit()
        await query.edit_message_caption(caption=f"❌ Deposit #{deposit_id} rejected.")
        dep = DB.execute("SELECT user_id, amount FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        await context.bot.send_message(dep["user_id"], "❌ Your deposit was rejected. Contact admin.")

# ---------- Deposit Conversation ----------
async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text)
    except ValueError:
        await update.message.reply_text("Please send a valid number.")
        return DEPOSIT_AMOUNT
    context.user_data["deposit_amount"] = amount
    # Send QR code
    try:
        with open("qr_code.jpg", "rb") as f:
            await update.message.reply_photo(f, caption=f"Scan to pay ₹{amount}. Then send screenshot/transaction ID.")
    except FileNotFoundError:
        await update.message.reply_text("QR code missing. Contact admin.")
        return ConversationHandler.END
    return DEPOSIT_SCREENSHOT

async def deposit_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    amount = context.user_data.get("deposit_amount", 0)
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    else:
        file_id = update.message.text  # fallback, user sent text as ID
    cursor = DB.execute("INSERT INTO deposits (user_id, amount, screenshot_file_id) VALUES (?,?,?)",
                        (user_id, amount, file_id))
    deposit_id = cursor.lastrowid
    DB.commit()

    # Notify admin
    admin_keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"appdep_{deposit_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"rejdep_{deposit_id}")]
    ]
    if update.message.photo:
        await context.bot.send_photo(ADMIN_ID, file_id,
                                     caption=f"Deposit #{deposit_id} by {user_id}\nAmount: ₹{amount}",
                                     reply_markup=InlineKeyboardMarkup(admin_keyboard))
    else:
        await context.bot.send_message(ADMIN_ID,
                                       f"Deposit #{deposit_id} by {user_id}\nAmount: ₹{amount}\nMessage: {file_id}",
                                       reply_markup=InlineKeyboardMarkup(admin_keyboard))
    await update.message.reply_text("✅ Deposit submitted. Admin will verify shortly.")
    return ConversationHandler.END

# ---------- Email Creation Conversation ----------
async def email_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = update.message.text.strip()
    if not re.match(r"^[a-zA-Z0-9._]{6,30}$", username):
        await update.message.reply_text("Invalid username. Use 6-30 letters, numbers, dots. Try again.")
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

    # Deduct balance
    DB.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (cost, user_id))
    DB.execute("INSERT INTO email_orders (user_id, desired_email, password, cost) VALUES (?,?,?,?)",
               (user_id, desired, password, cost))
    DB.commit()

    await update.message.reply_text("⏳ Creating Gmail account… This may take up to 2 minutes.")
    # Run in background
    asyncio.create_task(create_and_notify(user_id, desired, password))
    return ConversationHandler.END

async def create_and_notify(user_id: int, email: str, pwd: str):
    try:
        credentials = await create_gmail_account(email, pwd)
        DB.execute("UPDATE email_orders SET status='completed' WHERE desired_email=? AND user_id=?", (email, user_id))
        DB.commit()
        await application.bot.send_message(user_id, f"✅ Account created:\n`{credentials}`", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Creation failed: {e}")
        # Refund
        DB.execute("UPDATE users SET balance = balance + 10 WHERE user_id=?", (user_id,))
        DB.execute("UPDATE email_orders SET status='failed' WHERE desired_email=? AND user_id=?", (email, user_id))
        DB.commit()
        await application.bot.send_message(user_id, f"❌ Creation failed: {str(e)[:200]}. Refunded ₹10.")

# ---------- Main ----------
def main():
    global application
    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation: deposit
    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^deposit$")],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)],
            DEPOSIT_SCREENSHOT: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, deposit_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    # Conversation: email creation
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
    # Catch other callback data not part of conversations
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(login|wallet|main_menu|appdep_|rejdep_).*"))

    application.run_polling()

if __name__ == "__main__":
    main()
