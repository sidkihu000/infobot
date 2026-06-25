import telebot
from telebot import types
import json
import os
import re
import requests
import sqlite3
import logging
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ==================== LOGGING ===================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== ENVIRONMENT & API KEYS ====================
load_dotenv()

API_TOKEN = "8637135798:AAEGe1b-LOyOy21soiAp8uAcuAaCf_LfO2A"
if not API_TOKEN:
    logger.critical("Bot token missing")
    raise ValueError("Bot token not set")

# All your API keys (can be overridden via .env file)
TRUECALLER_API_KEY = os.getenv('TRUECALLER_API_KEY', 'bFPQlc1129809f801461eb218b146cc5ca550')
VERIPHONE_API_KEY = os.getenv('VERIPHONE_API_KEY', '161EEE47A53242C7B8E11F414B34C23B')
IPQS_PHONE_API_KEY = os.getenv('IPQS_PHONE_API_KEY', '96d1fa6b388a44d2a789e266593b7d3f')
IPQS_IP_API_KEY = os.getenv('IPQS_IP_API_KEY', '64b6081d2e6a43ba9b75498b473c7a8e')
NUMVERIFY_API_KEY = os.getenv('NUMVERIFY_API_KEY', '60762b849a6d6a7cf4f9c63bb68514c0')

# API URLs
TRUECALLER_API_URL = "https://api.truecaller.com/v1/search"
VERIPHONE_URL = "https://api.veriphone.io/v2/verify"
IPQS_PHONE_URL = "https://www.ipqualityscore.com/api/json/phone"
IPQS_IP_URL = "https://www.ipqualityscore.com/api/json/ip"
NUMVERIFY_URL = "http://apilayer.net/api/validate"

# ==================== BOT INIT ====================
bot = telebot.TeleBot(API_TOKEN)

# ==================== DATABASE ====================
DB_FILE = 'bot_data.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT,
            access_level TEXT DEFAULT 'user',
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS lookup_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            phone TEXT,
            result_json TEXT,
            looked_up_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (telegram_id)
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

init_db()

def db_connect():
    return sqlite3.connect(DB_FILE)

def get_user(telegram_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = ?", (str(telegram_id),))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'telegram_id': row[0],
            'name': row[1],
            'phone': row[2],
            'access_level': row[3],
            'registered_at': row[4]
        }
    return None

def add_or_update_user(telegram_id, name=None, phone=None, access_level='user'):
    conn = db_connect()
    c = conn.cursor()
    existing = get_user(telegram_id)
    if existing:
        c.execute('''UPDATE users SET name = COALESCE(?, name),
                     phone = COALESCE(?, phone), access_level = ?
                     WHERE telegram_id = ?''',
                  (name, phone, access_level, str(telegram_id)))
    else:
        c.execute('''INSERT INTO users (telegram_id, name, phone, access_level)
                     VALUES (?, ?, ?, ?)''',
                  (str(telegram_id), name, phone, access_level))
    conn.commit()
    conn.close()
    logger.info(f"User {telegram_id} added/updated.")

def delete_user(telegram_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE telegram_id = ?", (str(telegram_id),))
    c.execute("DELETE FROM lookup_history WHERE user_id = ?", (str(telegram_id),))
    conn.commit()
    conn.close()
    logger.info(f"User {telegram_id} deleted.")

def get_all_users():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT telegram_id, name, phone, access_level, registered_at FROM users")
    rows = c.fetchall()
    conn.close()
    return [{'telegram_id': r[0], 'name': r[1], 'phone': r[2],
             'access_level': r[3], 'registered_at': r[4]} for r in rows]

def add_lookup_history(user_id, phone, result):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO lookup_history (user_id, phone, result_json)
                 VALUES (?, ?, ?)''', (str(user_id), phone, json.dumps(result)))
    conn.commit()
    conn.close()
    logger.info(f"Lookup history added for user {user_id}.")

def get_user_history(user_id, limit=5):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''SELECT phone, result_json, looked_up_at FROM lookup_history
                 WHERE user_id = ? ORDER BY looked_up_at DESC LIMIT ?''',
              (str(user_id), limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_total_users():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_total_lookups():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM lookup_history")
    count = c.fetchone()[0]
    conn.close()
    return count

def is_admin(user_id):
    user = get_user(user_id)
    return user and user['access_level'] == 'admin'

# ==================== RATE LIMITING ====================
rate_limit_store = {}
def is_rate_limited(user_id, limit=10, period=60):
    now = time.time()
    timestamps = rate_limit_store.get(user_id, [])
    timestamps = [t for t in timestamps if now - t < period]
    if len(timestamps) >= limit:
        return True
    timestamps.append(now)
    rate_limit_store[user_id] = timestamps
    return False

# ==================== CACHE ====================
phone_cache = {}
CACHE_TTL = 3600

def get_cached_lookup(phone):
    if phone in phone_cache:
        data, timestamp = phone_cache[phone]
        if time.time() - timestamp < CACHE_TTL:
            return data
        else:
            del phone_cache[phone]
    return None

def set_cached_lookup(phone, data):
    phone_cache[phone] = (data, time.time())

# ==================== REAL API CALLS ====================

def truecaller_lookup(phone):
    """Owner name, address, etc. via Truecaller"""
    if not TRUECALLER_API_KEY:
        return None
    try:
        headers = {
            'Authorization': f'Bearer {TRUECALLER_API_KEY}',
            'User-Agent': 'Truecaller/13.57.6 (Android)',
            'Accept': 'application/json'
        }
        params = {'q': phone, 'countryCode': '', 'type': 1}
        resp = requests.get(TRUECALLER_API_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Truecaller can return a list or a dict; we take the first result.
        if isinstance(data, list) and len(data) > 0:
            entry = data[0]
            return {
                'name': entry.get('name', ''),
                'alternate_names': entry.get('alternateNames', []),
                'address': entry.get('address', ''),
                'city': entry.get('city', ''),
                'country': entry.get('country', ''),
                'carrier': entry.get('carrier', ''),
                'line_type': entry.get('lineType', ''),
                'ip': entry.get('ip', ''),
                'isp': entry.get('isp', ''),
                'latitude': entry.get('latitude'),
                'longitude': entry.get('longitude')
            }
        elif isinstance(data, dict):
            return {
                'name': data.get('name', ''),
                'alternate_names': data.get('alternateNames', []),
                'address': data.get('address', ''),
                'city': data.get('city', ''),
                'country': data.get('country', ''),
                'carrier': data.get('carrier', ''),
                'line_type': data.get('lineType', ''),
                'ip': data.get('ip', ''),
                'isp': data.get('isp', ''),
                'latitude': data.get('latitude'),
                'longitude': data.get('longitude')
            }
        return None
    except Exception as e:
        logger.error(f"Truecaller API error: {e}")
        return None

def numverify_lookup(phone):
    try:
        params = {'access_key': NUMVERIFY_API_KEY, 'number': phone, 'format': 1}
        resp = requests.get(NUMVERIFY_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get('valid'):
            return {
                'valid': True,
                'country': data.get('country_name', ''),
                'carrier': data.get('carrier', ''),
                'line_type': data.get('line_type', ''),
                'location': data.get('location', '')
            }
        return {'valid': False}
    except Exception as e:
        logger.error(f"NumVerify error: {e}")
        return None

def veriphone_lookup(phone):
    try:
        params = {'key': VERIPHONE_API_KEY, 'phone': phone}
        resp = requests.get(VERIPHONE_URL, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {
            'valid': data.get('phone_valid', False),
            'country': data.get('country', ''),
            'carrier': data.get('carrier', ''),
            'line_type': data.get('phone_type', '')
        }
    except Exception as e:
        logger.error(f"Veriphone error: {e}")
        return None

def ipqs_phone_lookup(phone):
    try:
        url = f"{IPQS_PHONE_URL}/{IPQS_PHONE_API_KEY}/{phone}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            return None
        return {
            'valid': True,
            'carrier': data.get('carrier', ''),
            'line_type': data.get('line_type', ''),
            'country': data.get('country', ''),
            'city': data.get('city', ''),
            'region': data.get('region', ''),
            'zip_code': data.get('zip_code', ''),
            'latitude': data.get('latitude'),
            'longitude': data.get('longitude'),
            'fraud_score': data.get('fraud_score', 0),
            'isp': data.get('isp', '')
        }
    except Exception as e:
        logger.error(f"IPQS Phone error: {e}")
        return None

def ipqs_ip_lookup(ip):
    try:
        url = f"{IPQS_IP_URL}/{IPQS_IP_API_KEY}/{ip}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            return None
        return {
            'ip': ip,
            'country': data.get('country_code', ''),
            'city': data.get('city', ''),
            'region': data.get('region', ''),
            'isp': data.get('ISP', ''),
            'organization': data.get('organization', ''),
            'latitude': data.get('latitude'),
            'longitude': data.get('longitude'),
            'fraud_score': data.get('fraud_score', 0),
            'proxy': data.get('proxy', False),
            'vpn': data.get('vpn', False),
            'tor': data.get('tor', False)
        }
    except Exception as e:
        logger.error(f"IPQS IP error: {e}")
        return None

# ==================== COMBINED PHONE LOOKUP ====================
def lookup_phone_number(phone):
    phone = re.sub(r'\s+', '', phone)
    if not phone.startswith('+'):
        phone = '+' + phone

    cached = get_cached_lookup(phone)
    if cached:
        return cached

    # 1. Truecaller – primary for owner name/address
    tc_data = truecaller_lookup(phone)

    # 2. Enrichment APIs
    num = numverify_lookup(phone)
    veri = veriphone_lookup(phone)
    ipqs = ipqs_phone_lookup(phone)

    final = {
        'valid': False,
        'name': 'N/A',
        'alternate_names': [],
        'address': 'N/A',
        'city': 'N/A',
        'region': '',
        'country': 'N/A',
        'zip_code': '',
        'carrier': 'N/A',
        'line_type': 'N/A',
        'ip': '',
        'isp': '',
        'latitude': None,
        'longitude': None,
        'fraud_score': None
    }

    if tc_data:
        final['valid'] = True
        final['name'] = tc_data.get('name') or 'N/A'
        final['alternate_names'] = tc_data.get('alternate_names', [])
        final['address'] = tc_data.get('address') or 'N/A'
        final['city'] = tc_data.get('city') or 'N/A'
        final['country'] = tc_data.get('country') or 'N/A'
        final['carrier'] = tc_data.get('carrier') or 'N/A'
        final['line_type'] = tc_data.get('line_type') or 'N/A'
        final['ip'] = tc_data.get('ip', '')
        final['isp'] = tc_data.get('isp', '')
        final['latitude'] = tc_data.get('latitude')
        final['longitude'] = tc_data.get('longitude')
        # Enrich with IPQS/Veriphone where Truecaller may lack details
        if ipqs:
            if not final['latitude']: final['latitude'] = ipqs.get('latitude')
            if not final['longitude']: final['longitude'] = ipqs.get('longitude')
            final['fraud_score'] = ipqs.get('fraud_score')
            final['zip_code'] = ipqs.get('zip_code', '')
            final['region'] = ipqs.get('region', '')
            if not final['isp']: final['isp'] = ipqs.get('isp', '')
        if num and not final['country']: final['country'] = num.get('country', '')
    else:
        # Fallback chain: IPQS -> Veriphone -> NumVerify
        if ipqs:
            final['valid'] = True
            final['carrier'] = ipqs.get('carrier') or (veri.get('carrier') if veri else (num.get('carrier') if num else 'N/A'))
            final['line_type'] = ipqs.get('line_type') or (veri.get('line_type') if veri else (num.get('line_type') if num else 'N/A'))
            final['country'] = ipqs.get('country') or (veri.get('country') if veri else (num.get('country') if num else 'N/A'))
            final['city'] = ipqs.get('city', '')
            final['region'] = ipqs.get('region', '')
            final['zip_code'] = ipqs.get('zip_code', '')
            final['latitude'] = ipqs.get('latitude')
            final['longitude'] = ipqs.get('longitude')
            final['fraud_score'] = ipqs.get('fraud_score')
            final['isp'] = ipqs.get('isp', '')
        elif veri and veri.get('valid'):
            final['valid'] = True
            final['carrier'] = veri.get('carrier') or (num.get('carrier') if num else 'N/A')
            final['line_type'] = veri.get('line_type') or (num.get('line_type') if num else 'N/A')
            final['country'] = veri.get('country') or (num.get('country') if num else 'N/A')
        elif num and num.get('valid'):
            final['valid'] = True
            final['carrier'] = num.get('carrier', 'N/A')
            final['line_type'] = num.get('line_type', 'N/A')
            final['country'] = num.get('country', 'N/A')
        else:
            final['error'] = 'Number invalid or API unavailable'

    set_cached_lookup(phone, final)
    return final

def validate_phone_number(phone):
    phone = re.sub(r'\s+', '', phone)
    if re.match(r'^\+\d{7,15}$', phone):
        return phone
    elif re.match(r'^\d{7,15}$', phone):
        return '+' + phone
    return None

# ==================== USER STATE ====================
user_states = {}

# ==================== BOT COMMANDS ====================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_info = types.InlineKeyboardButton("ℹ️ Bot Info", callback_data='bot_info')
    btn_help = types.InlineKeyboardButton("❓ Help", callback_data='help')
    btn_admin = types.InlineKeyboardButton("👤 Contact Admin", url='https://t.me/Xricx0')
    btn_website = types.InlineKeyboardButton("🌐 Website", url='https://your-website.com')
    btn_search = types.InlineKeyboardButton("🔍 Search User", callback_data='search_user')
    btn_list = types.InlineKeyboardButton("📋 List All Users", callback_data='list_users')
    btn_lookup = types.InlineKeyboardButton("📞 Lookup Number", callback_data='lookup_number')
    markup.add(btn_info, btn_help, btn_admin, btn_website, btn_search, btn_list, btn_lookup)
    bot.reply_to(message,
        f"Welcome {message.from_user.first_name}!\nChoose an option:",
        reply_markup=markup
    )
    logger.info(f"User {message.from_user.id} started bot.")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if call.data == 'bot_info':
        user_states[call.from_user.id] = 'waiting_for_phone'
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
            "📱 **Phone Number Lookup**\n\n"
            "Please send me a phone number (international format).\n"
            "Example: `+1234567890`\n\n"
            "Type /cancel to cancel.",
            parse_mode='Markdown'
        )
    elif call.data == 'help':
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
            "📚 **Help**\n"
            "- /start – main menu\n"
            "- /search <telegram_id> – find user by ID\n"
            "- /list – all users (admin only)\n"
            "- /lookup <phone> – full owner info (Truecaller + more)\n"
            "- /ip <ip> – IP intelligence\n"
            "- /add <id> <name> <phone> – add user (admin only)\n"
            "- /delete <id> – remove user (admin only)\n"
            "- /cancel – cancel pending operation\n"
            "- /history – your last 5 lookups\n"
            "- /myinfo – your stored data\n"
            "- /stats – bot stats (admin only)\n"
            "- /export – CSV of users (admin only)\n"
            "- /reset – reset your pending state\n\n"
            "🔎 **Search by username:** Click 'Search User' and send a Telegram username.",
            parse_mode='Markdown'
        )
    elif call.data == 'search_user':
        user_states[call.from_user.id] = 'waiting_for_username'
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
            "🔍 **Search User by Telegram Username**\n\n"
            "Please send the **username** (e.g., `@JohnDoe` or `JohnDoe`).\n"
            "Type /cancel to cancel.",
            parse_mode='Markdown'
        )
    elif call.data == 'list_users':
        bot.answer_callback_query(call.id)
        list_all_users(call.message)
    elif call.data == 'lookup_number':
        user_states[call.from_user.id] = 'waiting_for_phone'
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
            "📞 **Phone Number Lookup**\n"
            "Please send a phone number (international format).",
            parse_mode='Markdown'
        )

@bot.message_handler(func=lambda message: True)
def handle_phone_lookup(message):
    user_id = message.from_user.id

    if user_states.get(user_id) == 'waiting_for_phone':
        user_states[user_id] = None
        if message.text.lower() in ['/cancel', 'cancel', 'stop']:
            bot.reply_to(message, "❌ Cancelled.")
            return

        if is_rate_limited(user_id, limit=10, period=60):
            bot.reply_to(message, "⏳ Rate limit exceeded. Please wait.")
            return

        phone = validate_phone_number(message.text.strip())
        if not phone:
            bot.reply_to(message, "❌ Invalid format. Use e.g., `+1234567890`.", parse_mode='Markdown')
            return

        try:
            info = lookup_phone_number(phone)
            add_lookup_history(user_id, phone, info)
        except Exception as e:
            logger.error(f"Lookup error: {e}")
            bot.reply_to(message, "❌ API error. Try again later.")
            return

        # Format the response
        response = f"✅ **Phone Lookup Results**\n\n📞 `{phone}`\n"
        if not info.get('valid'):
            response += "❌ **Invalid / not found**\n"
        else:
            if info.get('name', 'N/A') != 'N/A':
                response += f"👤 **Owner:** {info['name']}\n"
                if info.get('alternate_names'):
                    response += f"📛 **Also known as:** {', '.join(info['alternate_names'])}\n"
            response += f"🏠 **Address:** {info.get('address', 'N/A')}\n"
            loc_parts = [info.get('city', ''), info.get('region', ''), info.get('country', '')]
            response += f"📍 **Location:** {', '.join(filter(None, loc_parts))}\n"
            if info.get('zip_code'):
                response += f"📮 **ZIP:** {info['zip_code']}\n"
            response += f"📶 **Carrier:** {info.get('carrier', 'N/A')}\n"
            response += f"📱 **Line Type:** {info.get('line_type', 'N/A')}\n"
            if info.get('isp'):
                response += f"🌐 **ISP:** {info['isp']}\n"
            if info.get('ip'):
                response += f"🔢 **IP:** `{info['ip']}`\n"
            if info.get('latitude') and info.get('longitude'):
                response += f"🗺️ **Coordinates:** {info['latitude']}, {info['longitude']}\n"
            if info.get('fraud_score') is not None:
                response += f"⚠️ **Fraud Score:** {info['fraud_score']}/100\n"
        bot.reply_to(message, response, parse_mode='Markdown')
        logger.info(f"Lookup performed for user {user_id}, phone {phone}")

    elif user_states.get(user_id) == 'waiting_for_username':
        user_states[user_id] = None
        if message.text.lower() in ['/cancel', 'cancel', 'stop']:
            bot.reply_to(message, "❌ Cancelled.")
            return

        raw_username = message.text.strip()
        username = raw_username.lstrip('@')
        if not username:
            bot.reply_to(message, "❌ Please provide a valid username.")
            return

        try:
            chat = bot.get_chat('@' + username)
            tg_id = str(chat.id)
            user = get_user(tg_id)
            if user and user.get('phone'):
                bot.reply_to(message,
                    f"✅ **User Found**\n"
                    f"👤 @{username}\n"
                    f"🆔 ID: `{tg_id}`\n"
                    f"📞 Phone: `{user['phone']}`\n\n"
                    f"🔍 Use /lookup {user['phone']} for detailed info.",
                    parse_mode='Markdown')
            else:
                bot.reply_to(message,
                    f"⚠️ @{username} (ID: `{tg_id}`) found, but no phone on file.",
                    parse_mode='Markdown')
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 400 and "chat not found" in str(e).lower():
                bot.reply_to(message, f"❌ Username @{username} not found.")
            else:
                bot.reply_to(message, f"❌ Telegram API error: {e.description}")
        except Exception as e:
            logger.error(f"Username lookup error: {e}")
            bot.reply_to(message, "❌ Unexpected error.")
        return

    else:
        if message.text.startswith('/'):
            allowed_commands = ['/start','/search','/list','/add','/delete','/lookup','/cancel','/history','/myinfo','/stats','/export','/reset','/ip']
            if message.text.lower() not in allowed_commands:
                bot.reply_to(message, "❓ Unknown command. Use /start.")
        else:
            bot.reply_to(message, "Use /start to see options.")

@bot.message_handler(commands=['lookup'])
def lookup_command(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /lookup <phone>")
            return

        if is_rate_limited(message.from_user.id):
            bot.reply_to(message, "⏳ Rate limit exceeded.")
            return

        phone = validate_phone_number(parts[1].strip())
        if not phone:
            bot.reply_to(message, "❌ Invalid format.")
            return

        info = lookup_phone_number(phone)
        add_lookup_history(message.from_user.id, phone, info)

        response = f"✅ **Phone Lookup**\n\n📞 `{phone}`\n"
        if not info.get('valid'):
            response += "❌ Invalid / not found\n"
        else:
            if info.get('name') != 'N/A':
                response += f"👤 Owner: {info['name']}\n"
            response += f"🏠 Address: {info.get('address','N/A')}\n"
            loc = ', '.join(filter(None, [info.get('city',''), info.get('region',''), info.get('country','')]))
            response += f"📍 Location: {loc or 'N/A'}\n"
            if info.get('zip_code'): response += f"📮 ZIP: {info['zip_code']}\n"
            response += f"📶 Carrier: {info.get('carrier','N/A')}\n"
            response += f"📱 Line Type: {info.get('line_type','N/A')}\n"
            if info.get('isp'): response += f"🌐 ISP: {info['isp']}\n"
            if info.get('ip'): response += f"🔢 IP: `{info['ip']}`\n"
            if info.get('latitude') and info.get('longitude'):
                response += f"🗺️ Coordinates: {info['latitude']}, {info['longitude']}\n"
            if info.get('fraud_score') is not None:
                response += f"⚠️ Fraud Score: {info['fraud_score']}/100\n"
        bot.reply_to(message, response, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Lookup command error: {e}")
        bot.reply_to(message, "❌ Error.")

@bot.message_handler(commands=['ip'])
def ip_lookup_command(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /ip <ip_address>")
            return
        ip = parts[1].strip()
        if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
            bot.reply_to(message, "❌ Invalid IP format.")
            return
        info = ipqs_ip_lookup(ip)
        if not info:
            bot.reply_to(message, "❌ Unable to fetch IP info.")
            return
        response = f"🌍 **IP Intelligence**\n\n🔢 `{ip}`\n"
        response += f"🏳️ Country: {info.get('country','N/A')}\n"
        response += f"🏙️ City/Region: {info.get('city','N/A')}, {info.get('region','N/A')}\n"
        response += f"🌐 ISP: {info.get('isp','N/A')}\n"
        response += f"🏢 Organization: {info.get('organization','N/A')}\n"
        response += f"🗺️ Coordinates: {info.get('latitude','?')}, {info.get('longitude','?')}\n"
        response += f"⚠️ Fraud Score: {info.get('fraud_score','?')}/100\n"
        flags = []
        if info.get('proxy'): flags.append('Proxy')
        if info.get('vpn'): flags.append('VPN')
        if info.get('tor'): flags.append('Tor')
        if flags:
            response += f"🚩 Flags: {', '.join(flags)}\n"
        bot.reply_to(message, response, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"IP lookup error: {e}")
        bot.reply_to(message, "❌ Error.")

# --- Remaining commands (history, myinfo, etc.) unchanged ---
@bot.message_handler(commands=['history'])
def history_command(message):
    user_id = message.from_user.id
    rows = get_user_history(user_id, limit=5)
    if not rows:
        bot.reply_to(message, "📭 No history.")
        return
    response = "📜 **Last 5 lookups:**\n\n"
    for phone, result_json, looked_up_at in rows:
        result = json.loads(result_json)
        response += f"📞 `{phone}` – {result.get('name','N/A')} ({result.get('country','?')})\n"
        response += f"   🕒 {looked_up_at}\n\n"
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['myinfo'])
def myinfo_command(message):
    user = get_user(message.from_user.id)
    if not user:
        bot.reply_to(message, "ℹ️ Not registered.")
        return
    response = f"👤 **Your Info**\n\n🆔 `{user['telegram_id']}`\n👤 {user['name'] or 'N/A'}\n📞 {user['phone'] or 'N/A'}\n🔑 {user['access_level']}\n📅 {user['registered_at']}"
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['reset'])
def reset_command(message):
    user_id = message.from_user.id
    if user_states.get(user_id) in ('waiting_for_phone', 'waiting_for_username'):
        user_states[user_id] = None
        bot.reply_to(message, "✅ Reset.")
    else:
        bot.reply_to(message, "ℹ️ Nothing to reset.")

@bot.message_handler(commands=['add'])
def add_user_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 4:
            bot.reply_to(message, "Usage: /add <telegram_id> <name> <phone>")
            return
        tg_id, name, phone = parts[1].strip(), parts[2].strip(), parts[3].strip()
        if not tg_id.isdigit():
            bot.reply_to(message, "❌ Invalid ID.")
            return
        if get_user(tg_id):
            bot.reply_to(message, f"⚠️ User `{tg_id}` already exists.")
            return
        add_or_update_user(tg_id, name, phone, 'user')
        bot.reply_to(message, f"✅ User `{tg_id}` added.")
    except Exception as e:
        logger.error(f"Add error: {e}")
        bot.reply_to(message, "❌ Error.")

@bot.message_handler(commands=['delete'])
def delete_user_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /delete <telegram_id>")
            return
        tg_id = parts[1].strip()
        if not tg_id.isdigit():
            bot.reply_to(message, "❌ Invalid ID.")
            return
        if not get_user(tg_id):
            bot.reply_to(message, f"❌ User `{tg_id}` not found.")
            return
        delete_user(tg_id)
        bot.reply_to(message, f"✅ User `{tg_id}` deleted.")
    except Exception as e:
        logger.error(f"Delete error: {e}")
        bot.reply_to(message, "❌ Error.")

@bot.message_handler(commands=['list'])
def list_all_users(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    users = get_all_users()
    if not users:
        bot.reply_to(message, "📭 No users.")
        return
    user_list = users[:10]
    total = len(users)
    response = f"📋 Showing {len(user_list)} of {total}\n\n"
    for u in user_list:
        response += f"🆔 `{u['telegram_id']}` - {u['name'] or 'N/A'} ({u['access_level']})\n"
    if total > 10:
        response += "\n... /list_full (not implemented)"
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['search'])
def search_user_command(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /search <telegram_id>")
            return
        tg_id = parts[1].strip()
        if not tg_id.isdigit():
            bot.reply_to(message, "❌ Invalid ID.")
            return
        user = get_user(tg_id)
        if user:
            response = f"✅ **User Found**\n\n🆔 `{tg_id}`\n👤 {user['name'] or 'N/A'}\n📞 {user['phone'] or 'N/A'}\n🔑 {user['access_level']}"
            bot.reply_to(message, response, parse_mode='Markdown')
        else:
            bot.reply_to(message, f"❌ User `{tg_id}` not found.")
    except Exception as e:
        logger.error(f"Search error: {e}")
        bot.reply_to(message, "❌ Error.")

@bot.message_handler(commands=['cancel'])
def cancel_command(message):
    user_id = message.from_user.id
    if user_states.get(user_id) in ('waiting_for_phone', 'waiting_for_username'):
        user_states[user_id] = None
        bot.reply_to(message, "✅ Cancelled.")
    else:
        bot.reply_to(message, "ℹ️ No active operation.")

@bot.message_handler(commands=['stats'])
def stats_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    total_users = get_total_users()
    total_lookups = get_total_lookups()
    response = f"📊 **Bot Stats**\n\n👥 Users: {total_users}\n🔍 Lookups: {total_lookups}\n📦 Cache: {len(phone_cache)}"
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['export'])
def export_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    users = get_all_users()
    if not users:
        bot.reply_to(message, "No users to export.")
        return
    import io
    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Telegram ID', 'Name', 'Phone', 'Access Level', 'Registered At'])
    for u in users:
        writer.writerow([u['telegram_id'], u['name'], u['phone'], u['access_level'], u['registered_at']])
    csv_data = output.getvalue()
    output.close()
    try:
        bot.send_document(message.chat.id, document=('users_export.csv', csv_data), caption="📁 Users export")
        logger.info(f"Admin {message.from_user.id} exported users.")
    except Exception as e:
        logger.error(f"Export error: {e}")
        bot.reply_to(message, "❌ Failed to send file.")

# ==================== MAIN ====================
if __name__ == '__main__':
    logger.info("Bot started. Truecaller + IPQS + Veriphone + NumVerify integrated.")
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.critical(f"Bot crashed: {e}")
