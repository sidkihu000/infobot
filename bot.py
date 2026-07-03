import asyncio, logging, os, re, random, sqlite3
from urllib.parse import urlparse
from typing import Optional, Union
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
OTP_DOCTOR_API_KEY = os.getenv("OTP_DOCTOR_API_KEY", "iztbgplf1l5fbwsk5fqfsrqqcweaxn2w")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "6Lf6U0EtAAAAABLduy-p5ch0aDrvBcFacxnHRKIJ")
PROXY_URL = os.getenv("PROXY_URL", "8ZdCh8tMpCpgwwPeDPJzddE2mEmpVAjBV1mVwTQ657En")
WEB_APP_URL = os.getenv("WEB_APP_URL", "")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Database ----------
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
CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR IGNORE INTO bot_config (key, value) VALUES ('maintenance', '0');
INSERT OR IGNORE INTO bot_config (key, value) VALUES ('video_file_id', '');
INSERT OR IGNORE INTO bot_config (key, value) VALUES ('qr_file_id', '');
""")
DB.commit()

# ---------- Config Helpers ----------
def get_config(key, default=None):
    row = DB.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default

def set_config(key, value):
    DB.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES (?,?)", (key, value))
    DB.commit()

def is_maintenance_on():
    return get_config('maintenance', '0') == '1'

def get_video_file_id():
    return get_config('video_file_id', '')

def get_qr_file_id():
    return get_config('qr_file_id', '')

def is_admin(user_id):
    return user_id == ADMIN_ID

# ---------- Proxy parser ----------
def parse_proxy_url(url: str) -> Optional[dict]:
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

# ---------- OTPDoctor API ----------
OTP_API_BASE = "https://otpdoctor.in/stubs/handler_api.php"
_service_cache = {}

async def _otp_request(params: dict) -> Union[dict, str]:
    params["api_key"] = OTP_DOCTOR_API_KEY
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(OTP_API_BASE, params=params)
        text = resp.text.strip()
        try:
            return resp.json()
        except:
            return text

async def get_balance() -> float:
    result = await _otp_request({"action": "getBalance"})
    if isinstance(result, str) and result.startswith("ACCESS_BALANCE:"):
        return float(result.split(":")[1])
    raise Exception(f"获取余额失败: {result}")

async def get_countries() -> dict:
    result = await _otp_request({"action": "getCountries"})
    if isinstance(result, dict):
        return result
    raise Exception(f"获取国家列表失败: {result}")

async def get_services(country: str) -> dict:
    result = await _otp_request({"action": "getServices", "country": country})
    if isinstance(result, dict):
        # Some APIs return a nested structure with a "services" key
        if "services" in result and isinstance(result["services"], dict):
            return result["services"]
        return result
    raise Exception(f"获取服务列表失败: {result}")

# ---------- IMPROVED get_service_id ----------
async def get_service_id(service_name: str, country: str = "any") -> str:
    cache_key = f"{service_name}_{country}"
    if cache_key in _service_cache:
        return _service_cache[cache_key]

    # Build list of countries to try: first hardcoded common ones, then dynamic from API
    countries_to_try = ["us", "gb", "in", "ru", "ua", "pl", "de", "fr", "es", "it"]
    if country != "any":
        countries_to_try = [country]
    else:
        try:
            countries_dict = await get_countries()
            dynamic_countries = list(countries_dict.keys())
            # Append dynamic ones not already in the list
            for c in dynamic_countries:
                if c not in countries_to_try:
                    countries_to_try.append(c)
            logger.info(f"Total countries to try: {len(countries_to_try)}")
        except Exception as e:
            logger.warning(f"Could not fetch countries from API: {e}")

    # Multiple service name variants to match
    service_variants = [service_name.lower(), "gmail", "googlemail"]

    for country_code in countries_to_try:
        try:
            services = await get_services(country_code)
            # services should be a dict: id -> name
            for sid, name in services.items():
                if not isinstance(name, str):
                    continue  # skip non-string values
                name_lower = name.lower()
                for variant in service_variants:
                    if variant in name_lower:
                        _service_cache[cache_key] = sid
                        logger.info(f"✅ Found service '{name}' (ID: {sid}) in country {country_code}")
                        return sid
        except Exception as e:
            logger.warning(f"Could not fetch services for {country_code}: {e}")
            # Avoid hitting rate limits
            await asyncio.sleep(1)
            continue

    # If we reach here, no service was found.
    # Log the services from a default country (e.g., US) to help debugging.
    try:
        services_us = await get_services("us")
        logger.info(f"Available services in US: {services_us}")
    except Exception as e:
        logger.warning(f"Could not fetch US services for debugging: {e}")

    raise Exception(f"No country found that offers service: {service_name}")

async def buy_number(service: str, max_price: float = 100) -> dict:
    if not service.isdigit():
        service = await get_service_id(service)
    result = await _otp_request({
        "action": "getNumber",
        "service": service,
        "maxPrice": str(max_price)
    })
    if isinstance(result, dict) and "id" in result and "number" in result:
        return result
    raise Exception(f"购买号码失败: {result}")

async def get_activation_status(activation_id: str) -> dict:
    result = await _otp_request({
        "action": "getStatus",
        "id": activation_id
    })
    if isinstance(result, dict):
        return result
    if isinstance(result, str) and result.startswith("STATUS_"):
        parts = result.split(":", 1)
        status = parts[0]
        code = parts[1] if len(parts) > 1 else None
        return {"status": status, "code": code}
    raise Exception(f"获取状态失败: {result}")

async def set_status(activation_id: str, status: int) -> str:
    result = await _otp_request({
        "action": "setStatus",
        "id": activation_id,
        "status": str(status)
    })
    if isinstance(result, str):
        return result
    raise Exception(f"设置状态失败: {result}")

async def get_sms(activation_id: str, timeout: int = 300) -> str:
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        await asyncio.sleep(10)
        try:
            data = await get_activation_status(activation_id)
            if data.get("status") == "STATUS_OK":
                code = data.get("code")
                if code:
                    m = re.search(r'(\d{4,8})', str(code))
                    return m.group(1) if m else str(code)
            elif data.get("status") in ["STATUS_CANCEL", "STATUS_EXPIRED"]:
                raise Exception(f"激活已取消或过期: {data.get('status')}")
        except Exception as e:
            logger.warning(f"检查SMS时出错: {e}")
            continue
    raise TimeoutError("SMS未在指定时间内收到")

async def cancel_activation(activation_id: str):
    try:
        result = await set_status(activation_id, 2)
        logger.info(f"取消激活 {activation_id}: {result}")
    except Exception as e:
        logger.warning(f"取消激活失败（非关键）: {e}")

# ---------- 2Captcha ----------
async def solve_captcha(page) -> Optional[str]:
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

# ---------- Gmail Creator ----------
async def create_gmail_auto(username: str, password: str) -> str:
    service_id = await get_service_id("google", "any")
    logger.info(f"Google服务ID: {service_id}")
    activation = await buy_number(service_id, max_price=100)
    phone = activation["number"]
    activation_id = activation["id"]
    logger.info(f"购买号码成功: {phone} (激活ID: {activation_id})")

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

            logger.info("等待SMS...")
            code = await get_sms(activation_id)
            logger.info(f"收到验证码: {code}")
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

            await set_status(activation_id, 1)   # 1 = completed
            await page.wait_for_timeout(4000)
            await browser.close()

        return f"{username}@gmail.com:{password}"

    except Exception as e:
        await cancel_activation(activation_id)
        raise e

# ---------- Bot UI ----------
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Create Email", callback_data="email_create")],
        [InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📞 Admin Contact", url="tg://user?id=Xricx0")],
        [InlineKeyboardButton("🔐 Login", callback_data="login")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_maintenance_on() and not is_admin(user_id):
        await update.message.reply_text("⚠️ Bot is under maintenance. Please try later.")
        return

    video_id = get_video_file_id()
    if video_id:
        try:
            await update.message.reply_video(video_id, caption="🎥 Welcome video")
        except:
            await update.message.reply_photo(video_id, caption="🎥 Welcome frame")
    qr_id = get_qr_file_id()
    if qr_id:
        try:
            await update.message.reply_photo(qr_id, caption="📱 Scan to pay")
        except:
            pass

    text = "👋 Fully automated Gmail creator.\nTap below to open the Web App, or use the inline menu:"
    buttons = []
    if WEB_APP_URL:
        buttons.append([InlineKeyboardButton("🚀 Open Creator", web_app=WebAppInfo(url=WEB_APP_URL))])
    buttons.extend(get_main_menu().inline_keyboard)
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_maintenance_on() and not is_admin(user_id):
        await update.message.reply_text("⚠️ Bot is under maintenance.")
        return

    data = update.message.web_app_data.data
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if is_maintenance_on() and not is_admin(user_id):
        await query.edit_message_text("⚠️ Bot is under maintenance.")
        return

    if data == "login":
        DB.execute("INSERT OR IGNORE INTO users (user_id,username,first_name) VALUES (?,?,?)",
                   (user_id, query.from_user.username, query.from_user.first_name))
        DB.commit()
        await query.edit_message_text("✅ Logged in!", reply_markup=get_main_menu())
    elif data == "wallet":
        user = DB.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            await query.answer("Login first", show_alert=True); return
        kbd = [[InlineKeyboardButton("📥 Deposit", callback_data="deposit")],
               [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await query.edit_message_text(f"💰 Balance: ₹{user['balance']:.2f}", reply_markup=InlineKeyboardMarkup(kbd))
    elif data == "deposit":
        user = DB.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
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
    elif data.startswith("admin_"):
        await handle_admin_callbacks(query, context, data)

# ---------- Admin deposit handling ----------
async def handle_admin_deposits(query, context, data):
    if not is_admin(query.from_user.id):
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

# ---------- Admin Panel ----------
ADMIN_PASSWORD = "sidhu01"
ADMIN_MAIN, ADMIN_UPLOAD_VIDEO, ADMIN_UPLOAD_QR = range(10, 13)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized.")
        return
    await update.message.reply_text("🔐 Enter admin password:")
    return ADMIN_MAIN

async def admin_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == ADMIN_PASSWORD:
        await show_admin_panel(update, context)
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ Wrong password. Try /admin again.")
        return ConversationHandler.END

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    maintenance = is_maintenance_on()
    status_text = "🟢 ON" if maintenance else "🔴 OFF"
    keyboard = [
        [InlineKeyboardButton(f"🛠 Toggle Maintenance ({status_text})", callback_data="admin_toggle")],
        [InlineKeyboardButton("🎬 Update Video Frame", callback_data="admin_upload_video")],
        [InlineKeyboardButton("📸 Update QR Code", callback_data="admin_upload_qr")],
        [InlineKeyboardButton("📊 Bot Management", callback_data="admin_stats")],
        [InlineKeyboardButton("❌ Close Panel", callback_data="admin_close")]
    ]
    await update.message.reply_text("👑 Admin Panel", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    maintenance = is_maintenance_on()
    status_text = "🟢 ON" if maintenance else "🔴 OFF"
    keyboard = [
        [InlineKeyboardButton(f"🛠 Toggle Maintenance ({status_text})", callback_data="admin_toggle")],
        [InlineKeyboardButton("🎬 Update Video Frame", callback_data="admin_upload_video")],
        [InlineKeyboardButton("📸 Update QR Code", callback_data="admin_upload_qr")],
        [InlineKeyboardButton("📊 Bot Management", callback_data="admin_stats")],
        [InlineKeyboardButton("❌ Close Panel", callback_data="admin_close")]
    ]
    await query.edit_message_text("👑 Admin Panel", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_admin_callbacks(query: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("Unauthorized", show_alert=True); return

    if data == "admin_toggle":
        current = is_maintenance_on()
        set_config('maintenance', '0' if current else '1')
        await query.answer(f"Maintenance {'ON' if not current else 'OFF'}")
        await admin_panel_edit(query, context)
    elif data == "admin_upload_video":
        await query.edit_message_text("📤 Send me a video or photo to set as the welcome frame.")
        return ADMIN_UPLOAD_VIDEO
    elif data == "admin_upload_qr":
        await query.edit_message_text("📤 Send me a photo to set as the QR code.")
        return ADMIN_UPLOAD_QR
    elif data == "admin_stats":
        total_users = DB.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_deposits = DB.execute("SELECT COUNT(*) FROM deposits").fetchone()[0]
        total_orders = DB.execute("SELECT COUNT(*) FROM email_orders").fetchone()[0]
        pending_deposits = DB.execute("SELECT COUNT(*) FROM deposits WHERE status='pending'").fetchone()[0]
        stats_text = (
            f"📊 Bot Statistics:\n"
            f"👥 Users: {total_users}\n"
            f"💰 Total Deposits: {total_deposits}\n"
            f"📧 Email Orders: {total_orders}\n"
            f"⏳ Pending Deposits: {pending_deposits}"
        )
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]))
    elif data == "admin_back":
        await admin_panel_edit(query, context)
    elif data == "admin_close":
        await query.edit_message_text("Panel closed.")
        return ConversationHandler.END

async def admin_upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END
    if update.message.video:
        file_id = update.message.video.file_id
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
    else:
        await update.message.reply_text("Please send a video or photo.")
        return ADMIN_UPLOAD_VIDEO
    set_config('video_file_id', file_id)
    await update.message.reply_text("✅ Video frame updated successfully!")
    await show_admin_panel(update, context)
    return ConversationHandler.END

async def admin_upload_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    else:
        await update.message.reply_text("Please send a photo.")
        return ADMIN_UPLOAD_QR
    set_config('qr_file_id', file_id)
    await update.message.reply_text("✅ QR code updated successfully!")
    await show_admin_panel(update, context)
    return ConversationHandler.END

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Admin action cancelled.")
    return ConversationHandler.END

# ---------- Deposit Conversation ----------
DEPOSIT_AMOUNT, DEPOSIT_SCREENSHOT = range(2)

async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_maintenance_on() and not is_admin(user_id):
        await update.message.reply_text("⚠️ Bot is under maintenance.")
        return ConversationHandler.END
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
    user_id = update.effective_user.id
    if is_maintenance_on() and not is_admin(user_id):
        await update.message.reply_text("⚠️ Bot is under maintenance.")
        return ConversationHandler.END
    amt = context.user_data.get("deposit_amount",0)
    file_id = update.message.photo[-1].file_id if update.message.photo else update.message.text
    cur = DB.execute("INSERT INTO deposits (user_id,amount,screenshot_file_id) VALUES (?,?,?)", (user_id,amt,file_id))
    dep_id = cur.lastrowid
    DB.commit()
    admin_kbd = [[InlineKeyboardButton("✅ Approve", callback_data=f"appdep_{dep_id}"),
                  InlineKeyboardButton("❌ Reject", callback_data=f"rejdep_{dep_id}")]]
    msg = f"Deposit #{dep_id} by {user_id}\nAmount: ₹{amt}"
    if update.message.photo:
        await context.bot.send_photo(ADMIN_ID, file_id, caption=msg, reply_markup=InlineKeyboardMarkup(admin_kbd))
    else:
        await context.bot.send_message(ADMIN_ID, f"{msg}\nTxID: {file_id}", reply_markup=InlineKeyboardMarkup(admin_kbd))
    await update.message.reply_text("✅ Submitted. Admin will verify.", reply_markup=get_main_menu())
    return ConversationHandler.END

# ---------- Email Creation Conversation ----------
EMAIL_USERNAME, EMAIL_PASSWORD = range(2)

async def email_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_maintenance_on() and not is_admin(user_id):
        await update.message.reply_text("⚠️ Bot is under maintenance.")
        return ConversationHandler.END
    usr = update.message.text.strip()
    if not re.match(r'^[a-zA-Z0-9._]{6,30}$', usr):
        await update.message.reply_text("Invalid username (6-30 chars)."); return EMAIL_USERNAME
    context.user_data["desired_email"] = usr
    await update.message.reply_text("Enter password (min 8 chars):")
    return EMAIL_PASSWORD

async def email_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_maintenance_on() and not is_admin(user_id):
        await update.message.reply_text("⚠️ Bot is under maintenance.")
        return ConversationHandler.END
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

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_command)],
        states={
            ADMIN_MAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_password)],
            ADMIN_UPLOAD_VIDEO: [MessageHandler(filters.VIDEO | filters.PHOTO, admin_upload_video)],
            ADMIN_UPLOAD_QR: [MessageHandler(filters.PHOTO, admin_upload_qr)],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)]
    )

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
    app.add_handler(admin_conv)
    app.add_handler(dep_conv)
    app.add_handler(email_conv)
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(login|wallet|main_menu|appdep_|rejdep_|admin_).*"))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data))
    app.run_polling()

if __name__ == "__main__":
