import asyncio
import logging
import os
import re
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

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SMS_API_KEY = os.getenv("SMS_API_KEY")

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

# ---------- Number Panel (5sim) Helpers ----------
SIM_API_BASE = "https://5sim.net/v1/user"

async def buy_activation() -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SIM_API_BASE}/buy/activation/google/any/any",
                                headers={"Authorization": f"Bearer {SMS_API_KEY}"})
        data = resp.json()
        if "id" not in data:
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

# ---------- Gmail Creation Engine (Playwright) ----------
async def create_gmail_account(desired_username: str, password: str) -> str:
    activation = await buy_activation()
    phone_number = activation["phone"]
    activation_id = activation["id"]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            await page.goto("https://accounts.google.com/signup/v2/webcreateaccount?flowName=GlifWebSignIn&flowEntry=SignUp")
            await page.wait_for_load_state("networkidle")

            await page.fill('input[name="firstName"]', "John")
            await page.fill('input[name="lastName"]', "Doe")
            await page.fill('input[name="Username"]', desired_username)
            await page.fill('input[name="Passwd"]', password)
            await page.fill('input[name="ConfirmPasswd"]', password)

            await page.click('button:has-text("Next")')
            await page.wait_for_timeout(3000)

            phone_input = await page.wait_for_selector('input[type="tel"]', timeout=15000)
            await phone_input.fill(phone_number)
            await page.click('button:has-text("Next")')

            code = await get_sms(activation_id)
            code_input = await page.wait_for_selector('input[type="tel"]', timeout=15000)
            await code_input.fill(code)
            await page.click('button:has-text("Next")')

            try:
                await page.click('button:has-text("I agree")') 
                await page.click('button:has-text("Next")')
            except:
                pass

            await page.wait_for_timeout(5000)
            await browser.close()

        async with httpx.AsyncClient() as client:
            await client.get(f"{SIM_API_BASE}/finish/{activation_id}",
                             headers={"Authorization": f"Bearer {SMS_API_KEY}"})

        return f"{desired_username}@gmail.com:{password}"

    except Exception as e:
        await cancel_activation(activation_id)
        raise e

# ---------- UI Animations ----------
async def loading_animation(bot, chat_id, message_id, stop_event: asyncio.Event):
    """Creates a spinning animation by editing the message dynamically."""
    frames = ["⏳ Creating account.", "⏳ Creating account..", "⏳ Creating account..."]
    try:
        while not stop_event.is_set():
            for frame in frames:
                if stop_event.is_set():
                    break
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=frame)
                await asyncio.sleep(1)
    except Exception as e:
        logging.error(f"Animation error: {e}")

# ---------- Bot Handlers ----------
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Email Create", callback_data="email_create")],
        [InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📞 Admin Contact", url="tg://user?id=Xricx0")],
        [InlineKeyboardButton("🔐 Login", callback_data="login")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Welcome to Gmail Creator Bot!\nUse the buttons below:", reply_markup=get_main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "login":
        DB.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
                   (user_id, query.from_user.username, query.from_user.first_name))
        DB.commit()
        await query.edit_message_text("✅ Logged in successfully! Use the menu.", reply_markup=get_main_menu())

    elif data == "wallet":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Please login first.", show_alert=True)
            return
        keyboard = [
            [InlineKeyboardButton("📥 Deposit", callback_data="deposit")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
        ]
        await query.edit_message_text(f"💰 Your Balance: ₹{user['balance']:.2f}", reply_markup=InlineKeyboardMarkup(keyboard))

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

    cursor = DB.execute("INSERT INTO deposits (user_id, amount, screenshot_file_id) VALUES (?,?,?)", (user_id, amount, file_id))
    deposit_id = cursor.lastrowid
    DB.commit()

    admin_keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"appdep_{deposit_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"rejdep_{deposit_id}")]
    ]
    
    msg_text = f"Deposit #{deposit_id} by {user_id}\nAmount: ₹{amount}"
    if update.message.photo:
        await context.bot.send_photo(ADMIN_ID, file_id, caption=msg_text, reply_markup=InlineKeyboardMarkup(admin_keyboard))
    else:
        await context.bot.send_message(ADMIN_ID, f"{msg_text}\nTxID: {file_id}", reply_markup=InlineKeyboardMarkup(admin_keyboard))
    
    await update.message.reply_text("✅ Deposit submitted. Admin will verify shortly.", reply_markup=get_main_menu())
    return ConversationHandler.END

# ---------- Email Creation Conversation ----------
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
    DB.execute("INSERT INTO email_orders (user_id, desired_email, password, cost) VALUES (?,?,?,?)", (user_id, desired, password, cost))
    DB.commit()

    msg = await update.message.reply_text("⏳ Initializing setup...")
    
    stop_event = asyncio.Event()
    bot_instance = context.bot
    asyncio.create_task(loading_animation(bot_instance, update.effective_chat.id, msg.message_id, stop_event))
    asyncio.create_task(create_and_notify(bot_instance, update.effective_chat.id, msg.message_id, user_id, desired, password, stop_event))
    
    return ConversationHandler.END

async def create_and_notify(bot, chat_id, message_id, user_id: int, email: str, pwd: str, stop_event: asyncio.Event):
    try:
        credentials = await create_gmail_account(email, pwd)
        DB.execute("UPDATE email_orders SET status='completed' WHERE desired_email=? AND user_id=?", (email, user_id))
        DB.commit()
        stop_event.set()
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ Account created:\n`{credentials}`", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Creation failed: {e}")
        stop_event.set()
        DB.execute("UPDATE users SET balance = balance + 10 WHERE user_id=?", (user_id,))
        DB.execute("UPDATE email_orders SET status='failed' WHERE desired_email=? AND user_id=?", (email, user_id))
        DB.commit()
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"❌ Creation failed: {str(e)[:100]}. Refunded ₹10.")

# ---------- Main Execution ----------
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
