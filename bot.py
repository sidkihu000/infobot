import os, json, logging, re, asyncio, tempfile, sqlite3, time, hashlib
from datetime import datetime, timedelta
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
import google.generativeai as genai
import aiohttp
import requests

# ---- Configuration ----
TOKEN = os.getenv("BOT_TOKEN", "6935043231:AAFSnPWsC8ti9j3npYHFQZU8wABrN5knfDU")
OWNER_ID = int(os.getenv("OWNER_ID", "2119464081"))
ADMIN_ID = 2119464081
GEMINI_API_KEY = "AQ.Ab8RN6KBW9XnJZTH2LP0-s39-BPJHZKVQGrJw42vpGwELUftZA"
API_BASE_URL = "https://jiosavanapiryden.vercel.app/api"
SUPPORT_LINK = "https://t.me/Xricx0"
CHANNEL_LINK = ""
DB_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DB_FILE = os.path.join(DB_PATH, "bot_data.db")
bot_start_time = time.time()

# ---- Configure Gemini ----
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro')

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Database setup ----
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_modes (
            chat_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'ai'
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_videos (
            chat_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---- Helper functions ----
def get_mode(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT mode FROM chat_modes WHERE chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "ai"

def set_mode(chat_id, mode):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO chat_modes (chat_id, mode) VALUES (?, ?)",
              (str(chat_id), mode))
    conn.commit()
    conn.close()

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
    c.execute("INSERT OR REPLACE INTO admin_videos (chat_id, file_id) VALUES (?, ?)",
              (str(chat_id), file_id))
    conn.commit()
    conn.close()

def format_duration(seconds):
    if not seconds:
        return "0:00"
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    return f"{minutes}:{remaining_seconds:02d}"

# ---- Music Search Functions (from Melody Stream Pro) ----
def search_songs_sync(query, page=0, limit=15):
    """Synchronous search for speed"""
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

def get_song_details_sync(song_id):
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

async def fast_music_search(query):
    """Main music search function - tries to download directly"""
    try:
        # Step 1: Search for songs
        results = search_songs_sync(query, page=0, limit=5)
        
        if not results or not results.get("success"):
            return None
        
        songs = results.get("data", {}).get("results", [])
        if not songs:
            return None
        
        # Step 2: Get first song details
        first_song = songs[0]
        song_id = first_song.get("id")
        
        if not song_id:
            return None
        
        # Step 3: Get download URL
        song_details = get_song_details_sync(song_id)
        
        if not song_details or not song_details.get("url"):
            return None
        
        # Step 4: Download the audio file
        download_url = song_details["url"]
        audio_response = requests.get(download_url, timeout=45)
        
        if audio_response.status_code == 200:
            # Save to temp file
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
            
            return {
                'file_path': temp_filename,
                'thumb_path': thumb_file,
                'title': song_details['title'],
                'artist': song_details['artist'],
                'duration': song_details['duration'],
                'duration_formatted': song_details.get('duration_formatted', '0:00'),
                'album': song_details.get('album', ''),
                'year': song_details.get('year', ''),
                'image_url': song_details.get('image_url', '')
            }
        
        return None
        
    except Exception as e:
        logger.error(f"Fast music search error: {e}")
        return None

# ---- Gemini AI integration ----
async def get_gemini_response(question):
    try:
        prompt = f"User: {question}\n\nAssistant (adumusic):"
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "I'm having trouble thinking right now. Please try again."

# ---- Premium inline menu ----
def get_premium_menu():
    keyboard = [
        [
            InlineKeyboardButton("💎 Premium", callback_data="premium_info"),
            InlineKeyboardButton("🎵 Search Music", callback_data="music_search")
        ],
        [
            InlineKeyboardButton("🤖 AI Chat", callback_data="ai_info"),
            InlineKeyboardButton("📊 Stats", callback_data="quick_stats")
        ],
        [
            InlineKeyboardButton("ℹ️ Support", url=SUPPORT_LINK),
            InlineKeyboardButton("⭐ Upgrade", callback_data="upgrade")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---- Command handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or "Music Lover"
    
    msg = f"""🎵 **ADUMUSIC PRO** 🎵

✨ Hello {first_name}! ✨

━━━━━━━━━━━━━━━━
🌟 FEATURES 🌟
━━━━━━━━━━━━━━━━

🎧 High Quality Audio
⚡ Instant Music Delivery
🤖 Gemini AI Assistant
🔍 Smart Search System

━━━━━━━━━━━━━━━━
📖 HOW TO USE 📖
━━━━━━━━━━━━━━━━

• Type `find song_name` for instant music
• Mention 'adumusic' to chat with AI
• /switch - Toggle AI/Music mode
• /animate @name - Create animations

━━━━━━━━━━━━━━━━
👨‍💻 Developer: @Xricx0
━━━━━━━━━━━━━━━━"""

    await update.message.reply_text(
        msg,
        reply_markup=get_premium_menu(),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "premium_info":
        msg = "💎 **Premium Features:**\n• Unlimited songs\n• HD audio quality\n• Priority support\n• No ads\n\nPrice: $5/month"
    
    elif query.data == "music_search":
        msg = "🎵 **Music Search:**\nType `find song_name` to get instant music!\n\nExample: `find shape of you`"
        await query.edit_message_text(
            msg,
            reply_markup=get_premium_menu(),
            parse_mode='Markdown'
        )
        return
    
    elif query.data == "ai_info":
        msg = "🤖 **AI Chat:**\nMention 'adumusic' to chat with Gemini AI!"
    
    elif query.data == "quick_stats":
        uptime = int(time.time() - bot_start_time)
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        msg = f"📊 **BOT STATISTICS**\n\n⏱️ Uptime: {hours}h {minutes}m\n✅ Status: Online\n🎵 Source: JioSaavan\n\n👨‍💻 Developer: @Xricx0"
    
    elif query.data == "upgrade":
        msg = "⭐ **Upgrade to Premium:**\nContact @Xricx0 to upgrade!"
    
    await query.edit_message_text(
        msg,
        reply_markup=get_premium_menu(),
        parse_mode='Markdown'
    )

async def switch_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current = get_mode(chat_id)
    new_mode = "ai" if current == "music" else "music"
    set_mode(chat_id, new_mode)
    await update.message.reply_text(
        f"🔄 Switched to {new_mode.upper()} mode.",
        reply_markup=get_premium_menu()
    )

async def set_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only bot admin can set the video.")
        return
    
    if update.message.reply_to_message and update.message.reply_to_message.video:
        file_id = update.message.reply_to_message.video.file_id
        set_admin_video(chat.id, file_id)
        await update.message.reply_text(
            "✅ Background video updated successfully!",
            reply_markup=get_premium_menu()
        )
    else:
        await update.message.reply_text(
            "📹 Reply to a video with /setvideo to set it as background.",
            reply_markup=get_premium_menu()
        )

async def delete_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can delete messages.")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /del <number> (max 100)")
        return
    try:
        count = int(args[0])
        if count < 1 or count > 100:
            await update.message.reply_text("Number must be between 1 and 100.")
            return
    except ValueError:
        await update.message.reply_text("Invalid number.")
        return
    
    await update.message.delete()
    await update.message.reply_text(f"🗑 Deleted {count} messages successfully!")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return
    
    text_lower = text.lower()
    logger.info(f"Received: {text}")
    
    # MUSIC SEARCH - Fast path using 'find' keyword
    if text_lower.startswith("find ") or text_lower.startswith("search ") or text_lower.startswith("play "):
        # Extract song query
        if text_lower.startswith("find "):
            song_query = text[5:].strip()
        elif text_lower.startswith("search "):
            song_query = text[7:].strip()
        else:
            song_query = text[5:].strip()
        
        if len(song_query) < 2:
            await update.message.reply_text(
                "❌ Please enter a valid song name (at least 2 characters).\n\nExample: `find shape of you`",
                reply_markup=get_premium_menu()
            )
            return
        
        # Send instant searching message
        status_msg = await update.message.reply_text(
            f"🔍 Searching for: **{song_query}**...",
            reply_markup=get_premium_menu(),
            parse_mode='Markdown'
        )
        
        # Fast music search
        result = await fast_music_search(song_query)
        
        if result and os.path.exists(result['file_path']):
            try:
                # Prepare caption
                caption = f"""🎵 **{result['title']}**

━━━━━━━━━━━━━━━━
✨ TRACK DETAILS ✨
━━━━━━━━━━━━━━━━

🎤 Artist: {result['artist']}
⏱️ Duration: {result['duration_formatted']}
💿 Album: {result['album']}
📅 Year: {result['year']}
📊 Quality: 320kbps MP3

━━━━━━━━━━━━━━━━
👨‍💻 Developer: @Xricx0
🎧 Powered by adumusic"""

                # Send audio with thumbnail
                with open(result['file_path'], 'rb') as audio:
                    if result['thumb_path'] and os.path.exists(result['thumb_path']):
                        with open(result['thumb_path'], 'rb') as thumb:
                            await update.message.reply_audio(
                                audio=audio,
                                title=result['title'][:60],
                                performer=result['artist'][:60],
                                duration=result['duration'],
                                caption=caption,
                                thumb=thumb,
                                reply_markup=get_premium_menu(),
                                parse_mode='Markdown'
                            )
                    else:
                        await update.message.reply_audio(
                            audio=audio,
                            title=result['title'][:60],
                            performer=result['artist'][:60],
                            duration=result['duration'],
                            caption=caption,
                            reply_markup=get_premium_menu(),
                            parse_mode='Markdown'
                        )
                
                # Cleanup
                try:
                    os.remove(result['file_path'])
                    if result['thumb_path'] and os.path.exists(result['thumb_path']):
                        os.remove(result['thumb_path'])
                except:
                    pass
                
                # Delete status message
                try:
                    await status_msg.delete()
                except:
                    pass
                
                return
                
            except Exception as e:
                logger.error(f"Error sending audio: {e}")
                await status_msg.edit_text(
                    f"❌ Error sending audio. Please try again.",
                    reply_markup=get_premium_menu()
                )
        else:
            await status_msg.edit_text(
                f"❌ Sorry, couldn't find **'{song_query}'**.\n\n💡 Tips:\n• Check spelling\n• Try artist name + song\n• Use only song name\n\nExample: `find believer`",
                reply_markup=get_premium_menu(),
                parse_mode='Markdown'
            )
        
        return
    
    # AI CHAT - Only respond when "adumusic" is mentioned
    is_mentioned = "adumusic" in text_lower
    
    if is_mentioned or update.effective_chat.type == "private":
        # Only respond in groups when mentioned
        if update.effective_chat.type in ["group", "supergroup"] and not is_mentioned:
            return
        
        # Clean text for AI
        clean_text = re.sub(r'(?i)adumusic\s*', '', text).strip()
        if not clean_text:
            clean_text = "Hello"
        
        # Get AI response
        response = await get_gemini_response(clean_text)
        
        await update.message.reply_text(
            f"🤖 {response}",
            reply_markup=get_premium_menu()
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    application = Application.builder().token(TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("switch", switch_mode))
    application.add_handler(CommandHandler("setvideo", set_video))
    application.add_handler(CommandHandler("del", delete_messages))
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    logger.info("adumusic Pro bot is starting with fast music delivery...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
