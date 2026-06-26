import telebot
from telebot import types
import requests
import json
import time
import os
from datetime import datetime
import hashlib
import re

# Bot Configuration
BOT_TOKEN = "6935043231:AAFSnPWsC8ti9j3npYHFQZU8wABrN5knfDU"
ADMIN_ID = 2119464081
API_BASE_URL = "https://jiosavanapiryden.vercel.app/api"
SUPPORT_LINK = "https://t.me/Xricx0"
CHANNEL_LINK = ""

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN)

# Data storage
DATA_FILE = "bot_data.json"
bot_start_time = time.time()

# -------------------- STYLISH PRIMARY TEXT FUNCTION --------------------
def style_primary(text: str) -> str:
    """
    Convert normal text to stylish primary text format.
    First letter of each word -> normal uppercase
    Remaining letters -> Unicode small capitals (where available)
    Numbers, punctuation, emojis remain unchanged.
    """
    small_caps_map = {
        'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ', 'f': 'ꜰ',
        'g': 'ɢ', 'h': 'ʜ', 'i': 'ɪ', 'j': 'ᴊ', 'k': 'ᴋ', 'l': 'ʟ',
        'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ', 'p': 'ᴘ', 'q': 'q',  
        'r': 'ʀ', 's': 'ꜱ', 't': 'ᴛ', 'u': 'ᴜ', 'v': 'ᴠ', 'w': 'ᴡ',
        'x': 'x', 'y': 'ʏ', 'z': 'ᴢ'
    }
    
    def convert_word(word: str) -> str:
        if not word:
            return word
        first = word[0].upper() if word[0].isalpha() else word[0]
        rest = ''.join(small_caps_map.get(ch.lower(), ch) for ch in word[1:])
        return first + rest
    
    words = text.split(' ')
    converted_words = [convert_word(word) for word in words]
    return ' '.join(converted_words)

# Load data
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
            # Ensure new keys exist for backwards compatibility
            if "start_frame" not in data:
                data["start_frame"] = None
            return data
    return {
        "users": [],
        "total_downloads": 0,
        "total_searches": 0,
        "start_frame": None
    }

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

data = load_data()
user_states = {}
user_last_search = {}
RATE_LIMIT_SECONDS = 3

# Helper Functions
def add_user(user_id):
    if user_id not in data["users"]:
        data["users"].append(user_id)
        save_data(data)
        return True
    return False

def is_admin(user_id):
    return user_id == ADMIN_ID

def check_rate_limit(user_id):
    now = time.time()
    if user_id in user_last_search:
        if now - user_last_search[user_id] < RATE_LIMIT_SECONDS:
            return False
    user_last_search[user_id] = now
    return True

def search_songs(query, page=0, limit=15):
    try:
        url = f"{API_BASE_URL}/search/songs"
        params = {"query": query, "page": page, "limit": limit}
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        print(f"Search error: {e}")
        return None

def get_song_details(song_id):
    try:
        url = f"{API_BASE_URL}/songs/{song_id}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            song_data = response.json()
            if song_data.get("success") and song_data.get("data"):
                song_info = song_data["data"][0]
                download_links = song_info.get("downloadUrl", [])
                
                # Fetch High Quality Image for the Pro Audio Frame
                img_data = song_info.get("image", [])
                img_url = img_data[-1].get("url", img_data[-1].get("link", "")) if isinstance(img_data, list) and img_data else ""
                
                if download_links:
                    best_quality = download_links[-1]
                    duration_sec = song_info.get("duration", 0)
                    minutes = duration_sec // 60
                    seconds = duration_sec % 60
                    formatted_duration = f"{minutes}:{seconds:02d}"
                    
                    return {
                        "url": best_quality.get("url"),
                        "title": song_info.get("name", "Unknown"),
                        "artist": ", ".join([a.get("name", "") for a in song_info.get("artists", {}).get("primary", [])]),
                        "duration": duration_sec,
                        "duration_formatted": formatted_duration,
                        "album": song_info.get("album", {}).get("name", ""),
                        "year": song_info.get("year", "N/A"),
                        "image_url": img_url
                    }
        return None
    except Exception as e:
        print(f"Song details error: {e}")
        return None

def create_main_keyboard(chat_type="private"):
    # Remove navigation keyboards if it's a group or supergroup
    if chat_type in ['group', 'supergroup']:
        return types.ReplyKeyboardRemove()
        
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("🎵 𝐒𝐄𝐀𝐑𝐂𝐇 𝐌𝐔𝐒𝐈𝐂"),
        types.KeyboardButton("📊 𝐒𝐓𝐀𝐓𝐒"),
        types.KeyboardButton("ℹ️ 𝐀𝐁𝐎𝐔𝐓"),
        types.KeyboardButton("📢 𝐒𝐇𝐀𝐑𝐄 𝐁𝐎𝐓"),
        types.KeyboardButton("⚙️ 𝐇𝐄𝐋𝐏")
    ]
    keyboard.add(*buttons)
    return keyboard

def format_duration(seconds):
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    return f"{minutes}:{remaining_seconds:02d}"

def format_uptime(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    parts = []
    if days > 0: parts.append(f"{days}d")
    if hours > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    if secs > 0 or not parts: parts.append(f"{secs}s")
    return " ".join(parts)

# ==================== COMMAND HANDLERS ====================

@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Music Lover"
    add_user(user_id)
    
    welcome_text = f"""🎵 MELODY STREAM PRO 🎵

✨ Hello {first_name}! ✨

Send 'find [song]' or 'search [song]' to download high-quality music instantly. 🎧

👨‍💻 Developer: @Xricx0"""
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_dev = types.InlineKeyboardButton("👨‍💻 Developer", url=SUPPORT_LINK)
    if CHANNEL_LINK:
        btn_channel = types.InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK)
        markup.add(btn_dev, btn_channel)
    else:
        markup.add(btn_dev)
    
    btn_search = types.InlineKeyboardButton("🎵 𝐒𝐄𝐀𝐑𝐂𝐇 𝐌𝐔𝐒𝐈𝐂", callback_data="quick_search")
    btn_stats = types.InlineKeyboardButton("📊 Bot Stats", callback_data="quick_stats")
    markup.add(btn_search, btn_stats)
    
    start_frame = data.get("start_frame")
    
    # Try sending video frame first
    if start_frame:
        try:
            bot.send_video(message.chat.id, start_frame, caption=style_primary(welcome_text), reply_markup=markup)
            bot.send_message(message.chat.id, style_primary("👇 Use the buttons below 👇"), reply_markup=create_main_keyboard(message.chat.type))
            return
        except Exception as e:
            print(f"Failed to send start frame: {e}")
            # Fallback to text below
            
    bot.send_message(message.chat.id, style_primary(welcome_text), reply_markup=markup)
    bot.send_message(message.chat.id, style_primary("👇 Use the buttons below 👇"), reply_markup=create_main_keyboard(message.chat.type))

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = """📖 MELODY STREAM - HELP GUIDE 📖

━━━━━━━━━━━━━━━━
🎯 QUICK COMMANDS 🎯
━━━━━━━━━━━━━━━━

/start - Restart the bot
/help - Show this guide
/about - About Melody Stream
/stats - View bot statistics
/share - Share this bot

━━━━━━━━━━━━━━━━
🔍 SEARCH TIPS 🔍
━━━━━━━━━━━━━━━━

• Start your message with 'find ' or 'search '
• Include artist name for better results
• Use correct spelling
• Example: find Shape of You

━━━━━━━━━━━━━━━━
⚡ FEATURES ⚡
━━━━━━━━━━━━━━━━

✓ High Quality Audio (320kbps)
✓ Album Cover Integration
✓ Unlimited Searches
✓ Free Forever!

━━━━━━━━━━━━━━━━
🆘 NEED HELP? Contact: @Xricx0

Enjoy your music journey! 🎧"""

    bot.send_message(message.chat.id, style_primary(help_text), reply_markup=create_main_keyboard(message.chat.type))

@bot.message_handler(commands=['about'])
def about_command(message):
    about_text = """🎵 ABOUT MELODY STREAM PRO 🎵

━━━━━━━━━━━━━━━━
✨ VISION ✨
━━━━━━━━━━━━━━━━

Bringing high-quality music to everyone, everywhere, completely free.

━━━━━━━━━━━━━━━━
⚙️ TECHNOLOGY ⚙️
━━━━━━━━━━━━━━━━

• Advanced Search Algorithm
• 320KBPS Audio Quality
• 24/7 Availability

━━━━━━━━━━━━━━━━
👨‍💻 DEVELOPER 👨‍💻
━━━━━━━━━━━━━━━━

Name: Xricx0 
Contact: @Xricx0

━━━━━━━━━━━━━━━━
📊 BOT STATS 📊
━━━━━━━━━━━━━━━━

Use /stats to view bot statistics

Keep vibing with Melody Stream! 🎧"""

    bot.send_message(message.chat.id, style_primary(about_text), reply_markup=create_main_keyboard(message.chat.type))

@bot.message_handler(commands=['stats'])
def stats_command(message):
    user_id = message.from_user.id
    
    total_users = len(data["users"])
    total_searches = data.get("total_searches", 0)
    total_downloads = data.get("total_downloads", 0)
    uptime = time.time() - bot_start_time
    uptime_str = format_uptime(uptime)
    
    stats_text = f"""📊 MELODY STREAM PRO STATISTICS 📊

━━━━━━━━━━━━━━━━
📈 USAGE METRICS 📈
━━━━━━━━━━━━━━━━

👥 Total Users: {total_users:,}
🔍 Total Searches: {total_searches:,}
📥 Total Downloads: {total_downloads:,}
⏱️ Bot Uptime: {uptime_str}

━━━━━━━━━━━━━━━━
⚡ PERFORMANCE ⚡
━━━━━━━━━━━━━━━━

💾 Database: Active
🌐 API Status: Online
🎵 Music Source: JioSaavn
📦 Quality: Up to 320kbps

━━━━━━━━━━━━━━━━

Thanks for using Melody Stream! 🎧

— @Xricx0"""
    
    if is_admin(user_id):
        stats_text += f"""

━━━━━━━━━━━━━━━━
👑 ADMIN INFO 👑
━━━━━━━━━━━━━━━━

👤 Admin ID: {ADMIN_ID}
✅ Status: Active"""
    
    bot.send_message(message.chat.id, style_primary(stats_text), reply_markup=create_main_keyboard(message.chat.type))

@bot.message_handler(commands=['share'])
def share_command(message):
    bot_username = bot.get_me().username
    share_text = f"🎵 Discover Melody Stream Pro - The Ultimate Music Bot!\n\n✅ Free High-Quality Music\n✅ Instant Downloads\n✅ Unlimited Searches\n\nTry it now: t.me/{bot_username}\n\nCreated by @Xricx0"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    tg_share_url = f"https://t.me/share/url?url=https://t.me/{bot_username}&text={requests.utils.quote(share_text)}"
    wa_share_url = f"https://wa.me/?text={requests.utils.quote(share_text)}"
    
    tg_btn = types.InlineKeyboardButton("📱 Telegram", url=tg_share_url)
    wa_btn = types.InlineKeyboardButton("💬 WhatsApp", url=wa_share_url)
    
    markup.add(tg_btn, wa_btn)
    
    bot.send_message(message.chat.id, style_primary("📢 SHARE MELODY STREAM\n\nShare with friends 👇"), reply_markup=markup)

# ==================== BUTTON HANDLERS ====================

@bot.message_handler(func=lambda message: message.text == "🎵 𝐒𝐄𝐀𝐑𝐂𝐇 𝐌𝐔𝐒𝐈𝐂")
def search_music_button(message):
    user_id = message.from_user.id
    user_states[user_id] = "waiting_for_song"
    
    search_prompt = """🎵 SEARCH FOR MUSIC 🎵

━━━━━━━━━━━━━━━━
✨ SEARCH TIPS ✨
━━━━━━━━━━━━━━━━

🎤 By Artist: "find Arijit Singh"
🎧 By Song: "search Shape of You"
🎬 By Movie: "find Animal songs"

━━━━━━━━━━━━━━━━
📝 EXAMPLES 📝
━━━━━━━━━━━━━━━━

→ find Believer
→ search Tere Bina Na Gujara
→ find Pal Pal by Talwinder

━━━━━━━━━━━━━━━━

Type your search query below starting with 'find' or 'search': 👇

Send /cancel to cancel ❌"""

    bot.send_message(message.chat.id, style_primary(search_prompt))

@bot.message_handler(func=lambda message: message.text == "📊 𝐒𝐓𝐀𝐓𝐒")
def stats_button_handler(message):
    stats_command(message)

@bot.message_handler(func=lambda message: message.text == "ℹ️ 𝐀𝐁𝐎𝐔𝐓")
def about_button_handler(message):
    about_command(message)

@bot.message_handler(func=lambda message: message.text == "📢 𝐒𝐇𝐀𝐑𝐄 𝐁𝐎𝐓")
def share_button_handler(message):
    share_command(message)

@bot.message_handler(func=lambda message: message.text == "⚙️ 𝐇𝐄𝐋𝐏")
def help_button_handler(message):
    help_command(message)

# ==================== ADMIN COMMANDS ====================

@bot.message_handler(commands=['admin'])
def admin_command(message):
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, style_primary("❌ Access Denied!\n\nYou don't have permission to access admin panel."))
        return
    
    admin_text = f"""👑 ADMIN PANEL 👑

━━━━━━━━━━━━━━━━
📋 COMMANDS 📋
━━━━━━━━━━━━━━━━

/stats - View bot statistics
/broadcast - Send message to users
/ping - Check bot status
/backup - Download user backup
/announce - Make announcement
/setframe - Set the start video frame

━━━━━━━━━━━━━━━━
📊 QUICK INFO 📊
━━━━━━━━━━━━━━━━

👤 Admin ID: {ADMIN_ID}
📁 Total Users: {len(data["users"])}
✅ Status: Active

━━━━━━━━━━━━━━━━

— @Xricx0"""
    
    bot.send_message(message.chat.id, style_primary(admin_text))

@bot.message_handler(commands=['setframe'])
def setframe_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(message.chat.id, style_primary("❌ Access Denied!"))
        return
        
    msg = bot.send_message(message.chat.id, style_primary("📹 SEND START FRAME\n\nPlease send a video or animation (GIF) to set as the small frame on the /start menu.\n\nSend /cancel to abort."))
    bot.register_next_step_handler(msg, process_setframe)

def process_setframe(message):
    if message.text == '/cancel':
        bot.send_message(message.chat.id, style_primary("❌ Start frame setup cancelled."))
        return
        
    file_id = None
    if message.video:
        file_id = message.video.file_id
    elif message.animation:
        file_id = message.animation.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith('video/'):
        file_id = message.document.file_id
        
    if file_id:
        data["start_frame"] = file_id
        save_data(data)
        bot.send_message(message.chat.id, style_primary("✅ Start frame successfully updated! New users will now see this video/animation."))
    else:
        bot.send_message(message.chat.id, style_primary("❌ Invalid file type. Please send a valid Video or GIF. Try /setframe again."))

@bot.message_handler(commands=['ping'])
def ping_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(message.chat.id, style_primary("❌ Access Denied!"))
        return
    
    start = time.time()
    msg = bot.send_message(message.chat.id, style_primary("🏓 Pinging..."))
    end = time.time()
    
    response_time = round((end - start) * 1000, 2)
    uptime = time.time() - bot_start_time
    
    ping_text = f"""🏓 PONG!

━━━━━━━━━━━━━━━━
⚡ RESPONSE TIME ⚡
━━━━━━━━━━━━━━━━

📡 Latency: {response_time}ms
⏱️ Uptime: {format_uptime(uptime)}
✅ Status: Online & Healthy

— @Xricx0"""
    
    bot.edit_message_text(style_primary(ping_text), message.chat.id, msg.message_id)

@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(message.chat.id, style_primary("❌ Access Denied!"))
        return
    
    msg = bot.send_message(message.chat.id, style_primary("📢 BROADCAST MESSAGE\n\nSend the message you want to broadcast to all users.\n\nSend /cancel to cancel."))
    bot.register_next_step_handler(msg, process_broadcast)

def process_broadcast(message):
    if message.text == '/cancel':
        bot.send_message(message.chat.id, style_primary("❌ Broadcast cancelled."))
        return
    
    broadcast_text = message.text
    status_msg = bot.send_message(message.chat.id, style_primary("📤 Broadcasting message...\n\n⏳ Please wait..."))
    
    success = 0
    failed = 0
    
    for user_id in data["users"]:
        try:
            bot.send_message(user_id, style_primary(f"📢 ANNOUNCEMENT\n\n━━━━━━━━━━━━━━━━\n\n{broadcast_text}\n\n━━━━━━━━━━━━━━━━\n\n— @Xricx0"))
            success += 1
            time.sleep(0.05)
        except Exception as e:
            failed += 1
            print(f"Failed to send to {user_id}: {e}")
    
    result_text = f"""✅ BROADCAST COMPLETE ✅

━━━━━━━━━━━━━━━━
📊 STATISTICS 📊
━━━━━━━━━━━━━━━━

✅ Successful: {success}
❌ Failed: {failed}
👥 Total Users: {len(data["users"])}

— @Xricx0"""
    
    bot.edit_message_text(style_primary(result_text), message.chat.id, status_msg.message_id)

@bot.message_handler(commands=['backup'])
def backup_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(message.chat.id, style_primary("❌ Access Denied!"))
        return
    
    try:
        backup_data = {
            "total_users": len(data["users"]),
            "users": data["users"],
            "total_searches": data.get("total_searches", 0),
            "total_downloads": data.get("total_downloads", 0),
            "backup_date": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        backup_file = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(backup_file, 'w') as f:
            json.dump(backup_data, f, indent=2)
        
        caption = style_primary(f"💾 USER BACKUP\n\n👥 Total Users: {len(data['users'])}\n📅 Date: {backup_data['backup_date']}")
        with open(backup_file, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=caption)
        
        os.remove(backup_file)
        
    except Exception as e:
        bot.send_message(message.chat.id, style_primary(f"❌ Backup Failed!\n\nError: {str(e)}"))

@bot.message_handler(commands=['announce'])
def announce_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(message.chat.id, style_primary("❌ Access Denied!"))
        return
    
    msg = bot.send_message(message.chat.id, style_primary("📢 MAKE AN ANNOUNCEMENT\n\nSend your announcement message. It will be pinned!\n\nSend /cancel to cancel."))
    bot.register_next_step_handler(msg, process_announcement)

def process_announcement(message):
    if message.text == '/cancel':
        bot.send_message(message.chat.id, style_primary("❌ Announcement cancelled."))
        return
    
    announce_text = message.text
    
    sent_msg = bot.send_message(message.chat.id, style_primary(f"📢 ANNOUNCEMENT\n\n━━━━━━━━━━━━━━━━\n\n{announce_text}\n\n━━━━━━━━━━━━━━━━\n\n— @Xricx0"))
    
    try:
        bot.pin_chat_message(message.chat.id, sent_msg.message_id)
    except Exception as e:
        print(f"Failed to pin message: {e}")
    
    bot.send_message(message.chat.id, style_primary("✅ Announcement posted and pinned!"))

# ==================== TEXT HANDLER ====================

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    
    # Handle cancel
    if message.text == '/cancel':
        if user_id in user_states:
            del user_states[user_id]
        bot.send_message(message.chat.id, style_primary("❌ Search cancelled"), reply_markup=create_main_keyboard(message.chat.type))
        return
    
    # Ignore standalone commands handled by other functions
    if message.text.startswith('/'):
        return

    text_lower = message.text.lower()
    
    # Check if the user used the 'find ' or 'search ' keyword
    if text_lower.startswith("find ") or text_lower.startswith("search "):
        if user_id in user_states:
            del user_states[user_id]
            
        if not check_rate_limit(user_id):
            bot.send_message(message.chat.id, style_primary("⏳ Please wait a few seconds before searching again!"))
            return
        
        # Extract the song name by slicing off the prefix
        if text_lower.startswith("find "):
            search_query = message.text[5:].strip()
        else:
            search_query = message.text[7:].strip()
        
        if len(search_query) < 2:
            bot.send_message(message.chat.id, style_primary("❌ Please enter a valid song name (at least 2 characters)"))
            return
        
        searching_msg = bot.send_message(message.chat.id, style_primary(f"🎵 Searching: {search_query}..."))
        
        results = search_songs(search_query, page=0, limit=15)
        
        if not results or not results.get("success"):
            bot.edit_message_text(style_primary("❌ No Results Found!\n\nPlease try different spelling or add artist name."), message.chat.id, searching_msg.message_id)
            return
        
        songs = results.get("data", {}).get("results", [])
        
        if not songs:
            bot.edit_message_text(style_primary("❌ No songs found!\n\n💡 Tips: Check spelling or try fewer words."), message.chat.id, searching_msg.message_id)
            return
        
        data["total_searches"] = data.get("total_searches", 0) + 1
        save_data(data)
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        
        for idx, song in enumerate(songs[:15], 1):
            song_name = song.get("name", "Unknown")
            artists = song.get("artists", {}).get("primary", [])
            artist_names = ", ".join([a.get("name", "") for a in artists[:2]])
            duration = song.get("duration", 0)
            duration_str = format_duration(duration)
            
            if artist_names:
                button_text = f"{idx}. {song_name[:35]} - {artist_names[:25]} [{duration_str}]"
            else:
                button_text = f"{idx}. {song_name[:45]} [{duration_str}]"
            
            callback_data = f"song_{song.get('id')}"
            markup.add(types.InlineKeyboardButton(button_text, callback_data=callback_data))
        
        bot.edit_message_text(style_primary(f"🎵 SEARCH RESULTS\n\n🔍 For: {search_query}\n📊 Found: {len(songs)} songs\n\n✨ Select a song to frame:"), message.chat.id, searching_msg.message_id, reply_markup=markup)
        
        nav_markup = types.InlineKeyboardMarkup(row_width=2)
        nav_markup.add(types.InlineKeyboardButton("🔄 New Search", callback_data="new_search"), types.InlineKeyboardButton("📊 𝐒𝐓𝐀𝐓𝐒", callback_data="quick_stats"))
        bot.send_message(message.chat.id, style_primary("👇 Need more options? 👇"), reply_markup=nav_markup)
        return
    
    # Notice: the bot will now completely ignore any text that isn't a search query. It won't send the warning message anymore.

# ==================== CALLBACK HANDLERS ====================

@bot.callback_query_handler(func=lambda call: call.data.startswith('song_'))
def song_callback(call):
    song_id = call.data.replace('song_', '')
    bot.answer_callback_query(call.id, style_primary("🔄 Initiating Pro Download..."))
    
    processing_msg = bot.send_message(call.message.chat.id, style_primary("🎵 PROCESSING PRO FRAME..."))
    
    try:
        song_details = get_song_details(song_id)
        
        if not song_details or not song_details.get("url"):
            bot.edit_message_text(style_primary("❌ Download Failed!\n\n⚠️ Unable to fetch download link. Please try again."), call.message.chat.id, processing_msg.message_id)
            return
        
        download_url = song_details["url"]
        title = song_details["title"]
        artist = song_details["artist"] or "Unknown Artist"
        duration = song_details["duration"]
        duration_formatted = song_details.get("duration_formatted", format_duration(duration))
        album = song_details.get("album", "Single")
        year = song_details.get("year", "N/A")
        
        bot.edit_message_text(style_primary(f"🎵 DOWNLOADING\n\n📀 Song: {title}\n🎤 Artist: {artist}\n\n⏳ Structuring Pro Audio Frame..."), call.message.chat.id, processing_msg.message_id)
        
        bot.send_chat_action(call.message.chat.id, 'upload_document')
        audio_response = requests.get(download_url, timeout=45)
        
        # Download Thumbnail to create the small professional frame
        thumb_file = None
        if song_details["image_url"]:
            try:
                img_response = requests.get(song_details["image_url"], timeout=10)
                if img_response.status_code == 200:
                    thumb_file = f"thumb_{song_id}.jpg"
                    with open(thumb_file, 'wb') as tf:
                        tf.write(img_response.content)
            except:
                pass
        
        if audio_response.status_code != 200:
            bot.edit_message_text(style_primary("❌ Download Failed!\n\n⚠️ Server error. Please try again."), call.message.chat.id, processing_msg.message_id)
            return
        
        temp_filename = f"temp_{song_id}_{hashlib.md5(title.encode()).hexdigest()[:8]}.mp3"
        with open(temp_filename, 'wb') as f:
            f.write(audio_response.content)
        
        bot.edit_message_text(style_primary(f"🎵 SENDING MUSIC\n\n⏳ Uploading framed audio to Telegram..."), call.message.chat.id, processing_msg.message_id)
        
        caption = style_primary(f"""🎵 {title} 🎵

━━━━━━━━━━━━━━━━
✨ TRACK DETAILS ✨
━━━━━━━━━━━━━━━━

🎤 Artist: {artist}
⏱️ Duration: {duration_formatted}
💿 Album: {album}
📅 Year: {year}
📊 Quality: 320kbps MP3

━━━━━━━━━━━━━━━━
👨‍💻 Developer: @Xricx0

🎧 Keep vibing with Melody Stream!""")
        
        # Send Audio with Thumb to generate the compact video/image frame inside chat
        with open(temp_filename, 'rb') as audio:
            if thumb_file and os.path.exists(thumb_file):
                with open(thumb_file, 'rb') as thumb:
                    bot.send_audio(call.message.chat.id, audio, title=title[:60], performer=artist[:60], duration=duration, caption=caption, thumb=thumb)
            else:
                bot.send_audio(call.message.chat.id, audio, title=title[:60], performer=artist[:60], duration=duration, caption=caption)
        
        try:
            bot.delete_message(call.message.chat.id, processing_msg.message_id)
        except:
            pass
        
        try:
            os.remove(temp_filename)
            if thumb_file and os.path.exists(thumb_file):
                os.remove(thumb_file)
        except:
            pass
        
        data["total_downloads"] = data.get("total_downloads", 0) + 1
        save_data(data)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🎵 Search Again", callback_data="new_search"), types.InlineKeyboardButton("📢 𝐒𝐇𝐀𝐑𝐄 𝐁𝐎𝐓", callback_data="share_bot"))
        bot.send_message(call.message.chat.id, style_primary("✅ Song Sent in Pro Frame Successfully!\n\nWant more music? Use the buttons below 👇"), reply_markup=markup)
        
    except Exception as e:
        print(f"Error in song_callback: {e}")
        try:
            bot.edit_message_text(style_primary("❌ Error Occurred!\n\nPlease try again later."), call.message.chat.id, processing_msg.message_id)
        except:
            pass

@bot.callback_query_handler(func=lambda call: call.data == "new_search")
def new_search_callback(call):
    user_id = call.from_user.id
    user_states[user_id] = "waiting_for_song"
    bot.answer_callback_query(call.id, style_primary("🔍 Ready to search!"))
    bot.send_message(call.message.chat.id, style_primary("🎵 Enter your search query:\n\nStart your message with 'find ' or 'search '!\n\nExample: find Believer\n\nSend /cancel to cancel"))

@bot.callback_query_handler(func=lambda call: call.data == "quick_search")
def quick_search_callback(call):
    user_id = call.from_user.id
    user_states[user_id] = "waiting_for_song"
    bot.answer_callback_query(call.id, style_primary("🔍 Type song name using 'find' keyword!"))
    bot.send_message(call.message.chat.id, style_primary("🎵 What would you like to listen to?\n\nStart your message with 'find ' or 'search '!\n\nExample: find Believer\n\nSend /cancel to cancel"))

@bot.callback_query_handler(func=lambda call: call.data == "quick_stats")
def quick_stats_callback(call):
    bot.answer_callback_query(call.id, style_primary("📊 Fetching statistics..."))
    stats_command(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "share_bot")
def share_bot_callback(call):
    bot.answer_callback_query(call.id, style_primary("📢 Sharing options..."))
    share_command(call.message)

# ==================== MAIN FUNCTION ====================

def main():
    print("=" * 50)
    print("🎵 MELODY STREAM BOT - STARTING UP 🎵")
    print("=" * 50)
    print(f"👑 Developer: @Xricx0")
    print(f"👑 Admin ID: {ADMIN_ID}")
    print(f"📊 Total Users: {len(data['users'])}")
    print(f"🔍 Total Searches: {data.get('total_searches', 0)}")
    print(f"📥 Total Downloads: {data.get('total_downloads', 0)}")
    print(f"⏱️ Bot Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    print("✅ Bot is running successfully!")
    print("✅ Admin panel command: /admin")
    print("=" * 50)
    
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"⚠️ Polling error: {e}")
            print("🔄 Restarting bot in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    main()
