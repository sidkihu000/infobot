import os, json, logging, re, time, hashlib
from datetime import datetime
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
import requests
import sqlite3

# ---- Configuration ----
TOKEN = os.getenv("BOT_TOKEN", "6248614957:AAGWzd37KASqv6u3OZRxt3gPaqkkdpmRNHg")
ADMIN_ID = 2119464081
API_BASE_URL = "https://jiosavanapiryden.vercel.app/api"
SUPPORT_LINK = "https://t.me/Xricx0"
CHANNEL_LINK = ""
DB_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DB_FILE = os.path.join(DB_PATH, "bot_data.db")
bot_start_time = time.time()

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Database Setup ----
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admin_videos (chat_id TEXT PRIMARY KEY, file_id TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER)''')
    conn.commit()
    conn.close()

init_db()

# ---- Helper Functions ----
def get_admin_video(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT file_id FROM admin_videos WHERE chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_admin_video(chat_id, file_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO admin_videos (chat_id, file_id) VALUES (?, ?)", (str(chat_id), file_id))
    conn.commit()
    conn.close()

def add_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (str(user_id),))
    conn.commit()
    conn.close()

def get_users_count():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_stat(key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM stats WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_stat(key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)", (key,))
    c.execute("UPDATE stats SET value = value + 1 WHERE key = ?", (key,))
    conn.commit()
    conn.close()

def format_duration(seconds):
    if not seconds:
        return "0:00"
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

# ---- Music Search Functions ----
def search_songs(query, page=0, limit=15):
    """Search songs using JioSaavan API"""
    try:
        url = f"{API_BASE_URL}/search/songs"
        params = {"query": query, "page": page, "limit": limit}
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        logger.error(f"Search error: {e}")
        return None

def get_song_details(song_id):
    """Get song details with download URL"""
    try:
        url = f"{API_BASE_URL}/songs/{song_id}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            song_data = response.json()
            if song_data.get("success") and song_data.get("data"):
                song_info = song_data["data"][0]
                download_links = song_info.get("downloadUrl", [])
                
                # Get high quality image
                img_data = song_info.get("image", [])
                img_url = img_data[-1].get("url", img_data[-1].get("link", "")) if isinstance(img_data, list) and img_data else ""
                
                if download_links:
                    best_quality = download_links[-1]
                    duration_sec = song_info.get("duration", 0)
                    
                    return {
                        "url": best_quality.get("url"),
                        "title": song_info.get("name", "Unknown"),
                        "artist": ", ".join([a.get("name", "") for a in song_info.get("artists", {}).get("primary", [])]),
                        "duration": duration_sec,
                        "duration_formatted": format_duration(duration_sec),
                        "album": song_info.get("album", {}).get("name", ""),
                        "year": song_info.get("year", "N/A"),
                        "image_url": img_url
                    }
        return None
    except Exception as e:
        logger.error(f"Song details error: {e}")
        return None

# ---- Inline Menus ----
def get_main_menu():
    """Main inline menu for groups and private chats"""
    keyboard = [
        [
            InlineKeyboardButton("🎵 Search Music", callback_data="music_search"),
            InlineKeyboardButton("📊 Stats", callback_data="stats")
        ],
        [
            InlineKeyboardButton("ℹ️ About", callback_data="about"),
            InlineKeyboardButton("📢 Share", callback_data="share")
        ],
        [
            InlineKeyboardButton("🎬 Set Video", callback_data="set_video_info"),
            InlineKeyboardButton("❓ Help", callback_data="help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_search_results_menu(songs, search_query):
    """Create inline menu for search results"""
    keyboard = []
    
    for idx, song in enumerate(songs[:15], 1):
        song_name = song.get("name", "Unknown")[:35]
        artists = song.get("artists", {}).get("primary", [])
        artist_names = ", ".join([a.get("name", "") for a in artists[:2]])[:25]
        duration = format_duration(song.get("duration", 0))
        
        if artist_names:
            button_text = f"{idx}. {song_name} - {artist_names} [{duration}]"
        else:
            button_text = f"{idx}. {song_name} [{duration}]"
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"song_{song.get('id')}")])
    
    keyboard.append([InlineKeyboardButton("🔄 New Search", callback_data="music_search")])
    
    return InlineKeyboardMarkup(keyboard)

# ---- Command Handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or "Music Lover"
    add_user(user_id)
    
    msg = f"""🎵 **MELODY STREAM PRO** 🎵

✨ Hello {first_name}! ✨

━━━━━━━━━━━━━━━━
🌟 FEATURES 🌟
━━━━━━━━━━━━━━━━

🎧 320kbps Quality Audio
⚡ Instant Music Delivery
🔍 Smart Search System
🖼️ Album Art Framing
📥 Unlimited Downloads

━━━━━━━━━━━━━━━━
📖 HOW TO USE 📖
━━━━━━━━━━━━━━━━

• Type `find song_name` for music
• Use buttons below for quick access
• /setvideo - Set background video
• /stats - View bot statistics

━━━━━━━━━━━━━━━━
👨‍💻 Developer: @Xricx0
━━━━━━━━━━━━━━━━"""
    
    await update.message.reply_text(
        msg,
        reply_markup=get_main_menu(),
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users = get_users_count()
    total_searches = get_stat("total_searches")
    total_downloads = get_stat("total_downloads")
    uptime = format_uptime(int(time.time() - bot_start_time))
    
    stats_text = f"""📊 **BOT STATISTICS**

━━━━━━━━━━━━━━━━
📈 USAGE METRICS
━━━━━━━━━━━━━━━━

👥 Total Users: {total_users:,}
🔍 Total Searches: {total_searches:,}
📥 Total Downloads: {total_downloads:,}
⏱️ Bot Uptime: {uptime}

━━━━━━━━━━━━━━━━
⚡ PERFORMANCE
━━━━━━━━━━━━━━━━

💾 Database: Active
🌐 API Status: Online
🎵 Music Source: JioSaavn
📦 Quality: Up to 320kbps

━━━━━━━━━━━━━━━━
👨‍💻 Developer: @Xricx0"""
    
    await update.message.reply_text(
        stats_text,
        reply_markup=get_main_menu(),
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = f"""📖 **HELP GUIDE**

━━━━━━━━━━━━━━━━
🎯 COMMANDS
━━━━━━━━━━━━━━━━

/start - Restart the bot
/help - Show this guide
/stats - View statistics
/setvideo - Set background video

━━━━━━━━━━━━━━━━
🔍 SEARCH TIPS
━━━━━━━━━━━━━━━━

• Type `find song_name`
• Include artist name for better results
• Example: `find Shape of You`
• Example: `find Believer`

━━━━━━━━━━━━━━━━
🆘 Support: @Xricx0"""
    
    await update.message.reply_text(
        help_text,
        reply_markup=get_main_menu(),
        parse_mode='Markdown'
    )

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    about_text = """🎵 **ABOUT MELODY STREAM PRO**

━━━━━━━━━━━━━━━━
✨ VISION
━━━━━━━━━━━━━━━━

Bringing high-quality music to everyone, everywhere, completely free.

━━━━━━━━━━━━━━━━
⚙️ TECHNOLOGY
━━━━━━━━━━━━━━━━

• Advanced Search Algorithm
• 320kbps Audio Quality
• 24/7 Availability
• Album Art Integration

━━━━━━━━━━━━━━━━
👨‍💻 DEVELOPER
━━━━━━━━━━━━━━━━

Name: Xricx0
Contact: @Xricx0"""
    
    await update.message.reply_text(
        about_text,
        reply_markup=get_main_menu(),
        parse_mode='Markdown'
    )

async def set_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Only admin can set the background video.",
            reply_markup=get_main_menu()
        )
        return
    
    if update.message.reply_to_message and update.message.reply_to_message.video:
        file_id = update.message.reply_to_message.video.file_id
        set_admin_video(update.effective_chat.id, file_id)
        await update.message.reply_text(
            "✅ **Background video set successfully!**\n\nThis video will be used for animations.",
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "📹 **Set Background Video:**\n\n1. Upload a video\n2. Reply to it with /setvideo\n\nOnly admin can use this command.",
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ Access Denied!")
        return
    
    start_time = time.time()
    msg = await update.message.reply_text("🏓 Pinging...")
    end_time = time.time()
    
    response_time = round((end_time - start_time) * 1000, 2)
    uptime = format_uptime(int(time.time() - bot_start_time))
    
    ping_text = f"""🏓 **PONG!**

📡 Latency: {response_time}ms
⏱️ Uptime: {uptime}
✅ Status: Online

👨‍💻 @Xricx0"""
    
    await msg.edit_text(ping_text, parse_mode='Markdown')

# ---- Callback Handlers ----
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "music_search":
        await query.edit_message_text(
            "🎵 **SEARCH MUSIC**\n\nType your search query starting with `find`:\n\nExample: `find shape of you`\nExample: `find believer`\n\nSend /cancel to cancel",
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )
    
    elif query.data == "stats":
        total_users = get_users_count()
        total_searches = get_stat("total_searches")
        total_downloads = get_stat("total_downloads")
        uptime = format_uptime(int(time.time() - bot_start_time))
        
        stats_text = f"""📊 **BOT STATISTICS**

👥 Users: {total_users:,}
🔍 Searches: {total_searches:,}
📥 Downloads: {total_downloads:,}
⏱️ Uptime: {uptime}

🎵 Source: JioSaavn
✅ Status: Online"""
        
        await query.edit_message_text(
            stats_text,
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )
    
    elif query.data == "about":
        about_text = """🎵 **MELODY STREAM PRO**

High-quality music bot powered by JioSaavn API.

✨ Features:
• 320kbps Audio
• Instant Delivery
• Album Art
• Unlimited Downloads

👨‍💻 Developer: @Xricx0"""
        
        await query.edit_message_text(
            about_text,
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )
    
    elif query.data == "share":
        bot_username = (await context.bot.get_me()).username
        share_text = f"🎵 Discover Melody Stream Pro!\n\n✅ Free High-Quality Music\n✅ Instant Downloads\n\nTry it: t.me/{bot_username}"
        
        share_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Share on Telegram", url=f"https://t.me/share/url?url=https://t.me/{bot_username}&text={share_text}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
        ])
        
        await query.edit_message_text(
            "📢 **SHARE WITH FRIENDS**",
            reply_markup=share_markup,
            parse_mode='Markdown'
        )
    
    elif query.data == "set_video_info":
        if user_id == ADMIN_ID:
            msg = "🎬 **SET BACKGROUND VIDEO**\n\nReply to a video with /setvideo to set it as background for animations."
        else:
            msg = "🎬 **Background Video**\n\nOnly admin can set the video. Contact @Xricx0"
        
        await query.edit_message_text(
            msg,
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )
    
    elif query.data == "help":
        help_text = """📖 **QUICK HELP**

🔍 Search: Type `find song_name`
📊 Stats: /stats
🎬 Video: /setvideo (admin)
❓ More: /help

Support: @Xricx0"""
        
        await query.edit_message_text(
            help_text,
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )
    
    elif query.data == "back_to_main":
        await query.edit_message_text(
            "🎵 **MELODY STREAM PRO**\n\nSelect an option:",
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("song_"):
        await handle_song_selection(update, context)

async def handle_song_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    song_id = query.data.replace("song_", "")
    
    await query.answer("🔄 Downloading...")
    
    processing_msg = await query.message.reply_text("🎵 **Processing...**", parse_mode='Markdown')
    
    try:
        song_details = get_song_details(song_id)
        
        if not song_details or not song_details.get("url"):
            await processing_msg.edit_text(
                "❌ Download failed! Please try again.",
                reply_markup=get_main_menu()
            )
            return
        
        # Download audio
        audio_response = requests.get(song_details["url"], timeout=45)
        
        if audio_response.status_code != 200:
            await processing_msg.edit_text(
                "❌ Server error! Please try again.",
                reply_markup=get_main_menu()
            )
            return
        
        # Save audio file
        temp_filename = f"music_{hashlib.md5(song_details['title'].encode()).hexdigest()[:8]}.mp3"
        with open(temp_filename, 'wb') as f:
            f.write(audio_response.content)
        
        # Download thumbnail
        thumb_file = None
        if song_details.get("image_url"):
            try:
                img_response = requests.get(song_details["image_url"], timeout=10)
                if img_response.status_code == 200:
                    thumb_file = f"thumb_{song_id}.jpg"
                    with open(thumb_file, 'wb') as tf:
                        tf.write(img_response.content)
            except:
                pass
        
        # Prepare caption
        caption = f"""🎵 **{song_details['title']}**

━━━━━━━━━━━━━━━━
✨ TRACK DETAILS
━━━━━━━━━━━━━━━━

🎤 Artist: {song_details['artist']}
⏱️ Duration: {song_details['duration_formatted']}
💿 Album: {song_details.get('album', 'Single')}
📅 Year: {song_details.get('year', 'N/A')}
📊 Quality: 320kbps MP3

━━━━━━━━━━━━━━━━
👨‍💻 Developer: @Xricx0

🎧 Enjoy your music!"""
        
        # Send audio
        with open(temp_filename, 'rb') as audio:
            if thumb_file and os.path.exists(thumb_file):
                with open(thumb_file, 'rb') as thumb:
                    await query.message.reply_audio(
                        audio=audio,
                        title=song_details['title'][:60],
                        performer=song_details['artist'][:60],
                        duration=song_details['duration'],
                        caption=caption,
                        thumb=thumb,
                        reply_markup=get_main_menu(),
                        parse_mode='Markdown'
                    )
            else:
                await query.message.reply_audio(
                    audio=audio,
                    title=song_details['title'][:60],
                    performer=song_details['artist'][:60],
                    duration=song_details['duration'],
                    caption=caption,
                    reply_markup=get_main_menu(),
                    parse_mode='Markdown'
                )
        
        # Cleanup
        try:
            os.remove(temp_filename)
            if thumb_file and os.path.exists(thumb_file):
                os.remove(thumb_file)
        except:
            pass
        
        # Update stats
        increment_stat("total_downloads")
        
        # Delete processing message
        try:
            await processing_msg.delete()
        except:
            pass
        
    except Exception as e:
        logger.error(f"Song selection error: {e}")
        await processing_msg.edit_text(
            "❌ Error occurred! Please try again.",
            reply_markup=get_main_menu()
        )

# ---- Message Handler ----
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return
    
    text_lower = text.lower()
    
    # Fast music search with 'find', 'search', or 'play' keywords
    if text_lower.startswith("find ") or text_lower.startswith("search ") or text_lower.startswith("play "):
        # Extract query
        if text_lower.startswith("find "):
            song_query = text[5:].strip()
        elif text_lower.startswith("search "):
            song_query = text[7:].strip()
        else:
            song_query = text[5:].strip()
        
        if len(song_query) < 2:
            await update.message.reply_text(
                "❌ Please enter at least 2 characters.\n\nExample: `find believer`",
                reply_markup=get_main_menu(),
                parse_mode='Markdown'
            )
            return
        
        # Send searching message
        status_msg = await update.message.reply_text(
            f"🔍 **Searching:** {song_query}...",
            parse_mode='Markdown'
        )
        
        # Search songs
        results = search_songs(song_query, page=0, limit=15)
        
        if not results or not results.get("success"):
            await status_msg.edit_text(
                f"❌ No results found for **{song_query}**\n\nTry different spelling or add artist name.",
                reply_markup=get_main_menu(),
                parse_mode='Markdown'
            )
            return
        
        songs = results.get("data", {}).get("results", [])
        
        if not songs:
            await status_msg.edit_text(
                f"❌ No songs found for **{song_query}**",
                reply_markup=get_main_menu(),
                parse_mode='Markdown'
            )
            return
        
        # Update stats
        increment_stat("total_searches")
        
        # If only one result, download directly
        if len(songs) == 1:
            song_id = songs[0].get("id")
            if song_id:
                song_details = get_song_details(song_id)
                
                if song_details and song_details.get("url"):
                    audio_response = requests.get(song_details["url"], timeout=45)
                    
                    if audio_response.status_code == 200:
                        temp_filename = f"music_{hashlib.md5(song_details['title'].encode()).hexdigest()[:8]}.mp3"
                        with open(temp_filename, 'wb') as f:
                            f.write(audio_response.content)
                        
                        thumb_file = None
                        if song_details.get("image_url"):
                            try:
                                img_response = requests.get(song_details["image_url"], timeout=10)
                                if img_response.status_code == 200:
                                    thumb_file = f"thumb_{song_id}.jpg"
                                    with open(thumb_file, 'wb') as tf:
                                        tf.write(img_response.content)
                            except:
                                pass
                        
                        caption = f"""🎵 **{song_details['title']}**

🎤 {song_details['artist']}
⏱️ {song_details['duration_formatted']} | 📊 320kbps
👨‍💻 @Xricx0"""
                        
                        with open(temp_filename, 'rb') as audio:
                            if thumb_file and os.path.exists(thumb_file):
                                with open(thumb_file, 'rb') as thumb:
                                    await update.message.reply_audio(
                                        audio=audio,
                                        title=song_details['title'][:60],
                                        performer=song_details['artist'][:60],
                                        duration=song_details['duration'],
                                        caption=caption,
                                        thumb=thumb,
                                        reply_markup=get_main_menu(),
                                        parse_mode='Markdown'
                                    )
                            else:
                                await update.message.reply_audio(
                                    audio=audio,
                                    title=song_details['title'][:60],
                                    performer=song_details['artist'][:60],
                                    duration=song_details['duration'],
                                    caption=caption,
                                    reply_markup=get_main_menu(),
                                    parse_mode='Markdown'
                                )
                        
                        try:
                            os.remove(temp_filename)
                            if thumb_file and os.path.exists(thumb_file):
                                os.remove(thumb_file)
                        except:
                            pass
                        
                        increment_stat("total_downloads")
                        await status_msg.delete()
                        return
        
        # Show search results
        await status_msg.edit_text(
            f"🎵 **SEARCH RESULTS**\n\n🔍 Query: {song_query}\n📊 Found: {len(songs)} songs\n\nSelect a song:",
            reply_markup=get_search_results_menu(songs, song_query),
            parse_mode='Markdown'
        )
        return
    
    # Invalid input
    if not text.startswith('/'):
        await update.message.reply_text(
            "🎵 **MELODY STREAM PRO**\n\nType `find song_name` to search music!\n\nExample: `find shape of you`",
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ---- Main Function ----
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("setvideo", set_video))
    application.add_handler(CommandHandler("ping", ping_command))
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    logger.info("🎵 Melody Stream Pro is starting...")
    logger.info(f"👑 Admin ID: {ADMIN_ID}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
