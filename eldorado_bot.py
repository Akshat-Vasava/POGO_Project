import os
import math
import shutil
import json
import telebot
from telebot import types, apihelper
from PIL import Image, ImageDraw, ImageFont
from flask import Flask
from threading import Thread, Timer
import time
import stat

# Maximum number of photos a user can upload per session (prevents RAM exhaustion)
MAX_PHOTOS_PER_SESSION = 20

def safe_delete_folder(folder_path):
    """Safely deletes a folder, handling Windows file locking and read-only permissions robustly."""
    if not os.path.exists(folder_path):
        return True

    def remove_readonly(func, path, excinfo):
        """Error handler for shutil.rmtree to handle read-only files."""
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    # Try standard rmtree with read-only handler first with retries
    for attempt in range(5):
        try:
            shutil.rmtree(folder_path, onerror=remove_readonly)
            return True
        except Exception:
            time.sleep(0.2) # Wait a tiny bit for the OS to release locks and retry

    # If that still fails, delete files individually as a fallback
    try:
        for root, dirs, files in os.walk(folder_path, topdown=False):
            for file in files:
                file_path = os.path.join(root, file)
                for attempt in range(5):
                    try:
                        os.chmod(file_path, stat.S_IWRITE)
                        os.remove(file_path)
                        break
                    except Exception:
                        time.sleep(0.1)
            for dir in dirs:
                dir_path = os.path.join(root, dir)
                for attempt in range(5):
                    try:
                        os.chmod(dir_path, stat.S_IWRITE)
                        os.rmdir(dir_path)
                        break
                    except Exception:
                        time.sleep(0.1)
        os.rmdir(folder_path)
        return True
    except Exception as e:
        print(f"[*] Final deletion fallback failed for {folder_path}: {e}")
        return False

# =========================================================
# 1. CONFIGURATION & ACCESS CONTROL (CLOUD-SECURE)
# =========================================================
# Remember to set these on Koyeb, NOT in the code!
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Fallback: Parse local .env file if running locally without the environment variable set
if not TELEGRAM_BOT_TOKEN:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    if key.strip() == "TELEGRAM_BOT_TOKEN":
                        TELEGRAM_BOT_TOKEN = value.strip().strip("'").strip('"')
                        os.environ["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
                        break

# Hardcode your authorized numeric IDs here
ALLOWED_USERS = [5282482434, 7871741290, 1985905883, 929088783, 6201618260] 

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Set global timeouts for pyTelegramBotAPI to prevent write timeouts on slow connections
apihelper.CONNECT_TIMEOUT = 90
apihelper.READ_TIMEOUT = 90

# Absolute path setup for cloud file system stability
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_user_data")
SETTINGS_FILE = os.path.join(BASE_DIR, "user_settings.json")

# Available watermark colors with their RGBA representations (including semi-transparency)
WATERMARK_COLORS = {
    "black": {"name": "⚫ Black", "rgba": (0, 0, 0, 100)},
    "white": {"name": "⚪ White", "rgba": (255, 255, 255, 130)},
    "red": {"name": "🔴 Red", "rgba": (255, 0, 0, 100)},
    "yellow": {"name": "🟡 Yellow", "rgba": (255, 255, 0, 100)}
}

# Per-user watermark preference (default: ON)
# Key: user_id (int), Value: True (watermark on) / False (watermark off)
user_watermark_settings = {}

# Per-user custom watermark text (default: "Galley-La")
# Key: user_id (int), Value: custom text string
user_watermark_text = {}

# Per-user watermark color preference (default: "black")
# Key: user_id (int), Value: color key string
user_watermark_colors = {}

# Per-user photo batch tracking (for debounced reply)
# Key: chat_id (str), Value: count of images in current batch
user_photo_count = {}
user_photo_timers = {}
user_photo_receiving_msg = {}  # Tracks the "Receiving images..." message object per user for cleanup

# Per-user quality preference (default: "document" for high-quality document, "photo" for compressed photo)
# Key: user_id (int), Value: "document" / "photo"
user_quality_settings = {}

# Per-user layout preference (default: "auto" for dynamic layout, "vertical" / "horizontal" / "grid")
# Key: user_id (int), Value: "auto" / "vertical" / "horizontal" / "grid"
user_layout_settings = {}

# =========================================================
# 2. SETTINGS PERSISTENCE (Survive Restarts)
# =========================================================
def save_user_settings():
    """Persists all user preferences to a JSON file so they survive bot restarts."""
    data = {
        "watermark_settings": {str(k): v for k, v in user_watermark_settings.items()},
        "watermark_text": {str(k): v for k, v in user_watermark_text.items()},
        "watermark_colors": {str(k): v for k, v in user_watermark_colors.items()},
        "quality_settings": {str(k): v for k, v in user_quality_settings.items()},
        "layout_settings": {str(k): v for k, v in user_layout_settings.items()},
    }
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[*] Warning: Could not save user settings: {e}")

def load_user_settings():
    """Loads all user preferences from the JSON file on startup."""
    global user_watermark_settings, user_watermark_text, user_watermark_colors
    global user_quality_settings, user_layout_settings
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        # Convert string keys back to int keys
        user_watermark_settings = {int(k): v for k, v in data.get("watermark_settings", {}).items()}
        user_watermark_text = {int(k): v for k, v in data.get("watermark_text", {}).items()}
        user_watermark_colors = {int(k): v for k, v in data.get("watermark_colors", {}).items()}
        user_quality_settings = {int(k): v for k, v in data.get("quality_settings", {}).items()}
        user_layout_settings = {int(k): v for k, v in data.get("layout_settings", {}).items()}
        print(f"[*] Loaded user settings for {len(user_watermark_settings)} user(s).")
    except Exception as e:
        print(f"[*] Warning: Could not load user settings: {e}")

# =========================================================
# 3. ADVANCED IMAGE PROCESSING (DIAGONAL WATERMARK & RAM OPTIMIZED)
# =========================================================
def apply_watermark(image, store_name="Galley-La", color_rgba=(0, 0, 0, 100)):
    """Adds a diagonal watermark with a custom color centered and scaled to fit without clipping."""
    img_w, img_h = image.size
    
    # 1. Dynamically scale font size relative to the minimum dimension to prevent overflow
    base_dim = min(img_w, img_h)
    font_size = int(base_dim * 0.12)
    font_size = max(40, min(font_size, 200)) # Keep font bounds safe
    
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except IOError:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        except IOError:
            font = ImageFont.load_default()

    # Measure exact text dimensions
    dummy = Image.new('RGBA', (1, 1), (0, 0, 0, 0))
    dummy_draw = ImageDraw.Draw(dummy)
    bbox = dummy_draw.textbbox((0, 0), store_name, font=font)
    t_w = bbox[2] - bbox[0]
    t_h = bbox[3] - bbox[1]
    
    # Add a small padding margin around the text
    padding = 20
    t_w_padded = t_w + padding
    t_h_padded = t_h + padding

    # 2. Create a square that can hold the rotated text diagonal perfectly
    diagonal = int(math.ceil(math.sqrt(t_w_padded**2 + t_h_padded**2)))
    
    # Create the square transparent image
    watermark_sq = Image.new('RGBA', (diagonal, diagonal), (255, 255, 255, 0))
    d_sq = ImageDraw.Draw(watermark_sq)
    
    # Draw text perfectly centered in the square (accounting for potential font offsets)
    text_x = (diagonal - t_w) / 2 - bbox[0]
    text_y = (diagonal - t_h) / 2 - bbox[1]
    d_sq.text((text_x, text_y), store_name, font=font, fill=color_rgba)
    
    # 3. Rotate the square watermark by 45 degrees
    # Since it is a square and fits the text diagonal, rotation will not clip it
    rotated_sq = watermark_sq.rotate(45, expand=0, resample=Image.BICUBIC)
    
    # 4. Paste the rotated square centered onto the main canvas
    paste_x = int((img_w - diagonal) / 2)
    paste_y = int((img_h - diagonal) / 2)
    
    image.paste(rotated_sq, (paste_x, paste_y), rotated_sq)
    
    return image

def create_collage(image_folder, output_path, watermark_enabled=True, watermark_text="Galley-La", layout_style="auto", watermark_color="black"):
    """Builds a seamless masonry collage with no black backgrounds."""
    image_files = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg')) and f != "final_collage.jpg"]
    if not image_files: return None

    # Load images
    imgs = [Image.open(os.path.join(image_folder, f)).convert("RGB") for f in image_files]
    n = len(imgs)
    
    # Calculate layout list based on user preference
    if layout_style == "vertical":
        layout = [1] * n
    elif layout_style == "horizontal":
        layout = [n]
    elif layout_style == "grid":
        # Balanced Grid layout
        if n <= 3:
            layout = [n]
        elif n == 4:
            layout = [2, 2]
        elif n == 5:
            layout = [3, 2]
        elif n == 6:
            layout = [3, 3]
        elif n == 7:
            layout = [3, 2, 2]
        elif n == 8:
            layout = [3, 3, 2]
        elif n == 9:
            layout = [3, 3, 3]
        else:
            # Fallback for > 9 images: split as evenly as possible into rows of size sqrt(n)
            rows_count = math.ceil(math.sqrt(n))
            base = n // rows_count
            extra = n % rows_count
            layout = [base] * rows_count
            for i in range(extra):
                layout[i] += 1
    else: # "auto"
        # CUSTOM LAYOUT LOGIC: Force a 3-2 grid for 5 images
        if n == 5:
            layout = [3, 2] # 3 on top, 2 on the bottom
        elif n <= 4:
            # 1 to 4 images stay in 1 or 2 rows
            layout = [math.ceil(n/2), n // 2] if n > 1 else [1]
        else:
            # 6 or more images get safely split into 3 rows
            rows_count = 3
            base = n // rows_count
            extra = n % rows_count
            layout = [base] * rows_count
            for i in range(extra): 
                layout[i] += 1

    # Base width set to 2000px: High quality but safe for 512MB RAM servers
    canvas_width = 2000 
    idx = 0
    rows_data = []

    # The Core Engine: Resize to match heights, then scale to canvas width
    for count in layout:
        row_imgs = imgs[idx:idx + count]
        idx += count
        
        target_h = min([i.height for i in row_imgs])
        
        resized = []
        total_w = 0
        for img in row_imgs:
            ratio = target_h / img.height
            new_w = int(img.width * ratio)
            new_h = int(target_h)
            r = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            resized.append(r)
            total_w += new_w
            
        scale = canvas_width / total_w
        final_row = []
        row_h = 0
        for img in resized:
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            r = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            final_row.append(r)
            row_h = new_h
            
        rows_data.append((final_row, row_h))

    # Build the final canvas without gaps
    total_height = sum(h for _, h in rows_data)
    collage = Image.new("RGB", (canvas_width, total_height), (255, 255, 255))

    y = 0
    for row, h in rows_data:
        x = 0
        for img in row:
            collage.paste(img, (x, y))
            x += img.width
        y += h

    # Free up memory
    for img in imgs: 
        img.close()

    # Apply the perfectly scaled diagonal watermark (if enabled)
    if watermark_enabled:
        color_info = WATERMARK_COLORS.get(watermark_color, WATERMARK_COLORS["black"])
        collage = apply_watermark(collage, watermark_text, color_rgba=color_info["rgba"])
    
    # Save with high quality
    collage.save(output_path, "JPEG", quality=95)
    return collage, output_path


# =========================================================
# 4. TELEGRAM BOT HANDLERS
# =========================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, f"⛔ Access Denied. Your numeric Telegram ID is: {message.from_user.id}")
        return

    user_name = message.from_user.first_name or "Nakama"
    welcome_text = (
        f"🏴‍☠️ **Welcome aboard, {user_name}!**\n\n"
        "I'm the *Galley-La Bot* — your personal collage builder! 🛠️\n\n"
        "Just send me your screenshots and I'll stitch them into a clean, "
        "professional collage with watermarks, custom layouts, and more.\n\n"
        "Ready to set sail? 📸 Send your first photos!\n\n"
        "💡 _Type /help anytime to see all available commands._"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['help'])
def send_help(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, f"⛔ Access Denied. Your numeric Telegram ID is: {message.from_user.id}")
        return

    help_text = (
        "📖 **Command Reference**\n\n"
        "📸 *Send photos* — Upload screenshots to the bot.\n"
        "⚙️ /generate — Build a collage from your uploaded photos.\n"
        "🗑️ /clear — Discard uploaded photos and start over.\n"
        "🔖 /watermark — Toggle watermark on/off, set custom text & color.\n"
        "⚡ /quality — Choose between high-quality document or fast photo output.\n"
        "📐 /layout — Pick collage layout style (Auto, Grid, Vertical, Horizontal).\n\n"
        f"📌 _Photo limit: {MAX_PHOTOS_PER_SESSION} images per session._"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown')

@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    if message.from_user.id not in ALLOWED_USERS: return
    
    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)
    # CLOUD FIX: Absolute path path guaranteed folder creation
    os.makedirs(user_folder, exist_ok=True)

    # Enforce photo upload limit to prevent RAM exhaustion
    existing_photos = [f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg')) and f != "final_collage.jpg"]
    if len(existing_photos) >= MAX_PHOTOS_PER_SESSION:
        bot.reply_to(message, f"⚠️ Maximum limit of {MAX_PHOTOS_PER_SESSION} photos reached! Use /generate to build your collage or /clear to start over.")
        return

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    file_path = os.path.join(user_folder, f"{message.photo[-1].file_id}.jpg")
    with open(file_path, 'wb') as new_file:
        new_file.write(downloaded_file)

    # Increment photo count for this user
    user_photo_count[user_id] = user_photo_count.get(user_id, 0) + 1

    # Send an instant "Receiving images..." message on the first photo of a batch
    if user_photo_count[user_id] == 1:
        try:
            receiving_msg = bot.send_message(message.chat.id, "📥 _Receiving images..._", parse_mode='Markdown')
            user_photo_receiving_msg[user_id] = receiving_msg
        except Exception:
            pass

    # Cancel any existing timer for this user (debounce)
    if user_id in user_photo_timers:
        user_photo_timers[user_id].cancel()

    # Start a new 2-second timer; fires only after user stops sending photos
    chat_id = message.chat.id
    def send_batch_reply():
        count = user_photo_count.pop(user_id, 0)
        user_photo_timers.pop(user_id, None)
        # Delete the "Receiving images..." message
        receiving_msg = user_photo_receiving_msg.pop(user_id, None)
        if receiving_msg:
            try:
                bot.delete_message(receiving_msg.chat.id, receiving_msg.message_id)
            except Exception:
                pass
        if count > 0:
            bot.send_message(chat_id, f"📸 {count} Images received! Send more, or type /generate.")

    timer = Timer(2.0, send_batch_reply)
    user_photo_timers[user_id] = timer
    timer.start()

@bot.message_handler(commands=['watermark'])
def toggle_watermark(message):
    """Lets the user turn watermark on or off via inline buttons."""
    if message.from_user.id not in ALLOWED_USERS: return

    user_id = message.from_user.id
    current = user_watermark_settings.get(user_id, True)
    current_text = user_watermark_text.get(user_id, "Galley-La")
    current_color_key = user_watermark_colors.get(user_id, "black")
    color_info = WATERMARK_COLORS.get(current_color_key, WATERMARK_COLORS["black"])
    current_color_name = color_info["name"]
    status = "✅ ON" if current else "❌ OFF"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Turn ON", callback_data="wm_on"),
        types.InlineKeyboardButton("❌ Turn OFF", callback_data="wm_off")
    )
    markup.add(
        types.InlineKeyboardButton("✏️ Custom Text", callback_data="wm_text"),
        types.InlineKeyboardButton("🎨 Choose Color", callback_data="wm_color_menu")
    )
    bot.send_message(
        message.chat.id,
        f"🔖 **Watermark Settings**\n\nCurrent status: {status}\nCurrent text: `{current_text}`\nCurrent color: {current_color_name}\n\nChoose an option below:",
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.message_handler(commands=['quality'])
def toggle_quality(message):
    """Lets the user toggle between Document (high quality) and Photo (compressed) output."""
    if message.from_user.id not in ALLOWED_USERS: return

    user_id = message.from_user.id
    current = user_quality_settings.get(user_id, "document")
    status = "📄 Document (High Quality)" if current == "document" else "🖼️ Photo (Compressed/Fast)"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📄 Document", callback_data="q_document"),
        types.InlineKeyboardButton("🖼️ Photo", callback_data="q_photo")
    )
    bot.send_message(
        message.chat.id,
        f"⚡ **Image Quality & Format Settings**\n\nCurrent mode: {status}\n\nChoose how you want the bot to send your collage:",
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.message_handler(commands=['clear'])
def clear_session(message):
    """Clears the currently uploaded photos and resets the session."""
    if message.from_user.id not in ALLOWED_USERS: return

    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)

    # Cancel any active batch timers
    if user_id in user_photo_timers:
        try:
            user_photo_timers[user_id].cancel()
        except: pass
        user_photo_timers.pop(user_id, None)
    
    user_photo_count.pop(user_id, None)

    if os.path.exists(user_folder):
        if safe_delete_folder(user_folder):
            bot.reply_to(message, "🗑️ Your uploaded photos have been cleared. You can start sending new ones.")
        else:
            bot.reply_to(message, "❌ Failed to clear photos. Some files may be temporarily locked by the operating system.")
    else:
        bot.reply_to(message, "🤷 No photos found to clear.")

@bot.message_handler(commands=['layout'])
def toggle_layout(message):
    """Lets the user choose their collage layout style via inline buttons."""
    if message.from_user.id not in ALLOWED_USERS: return

    user_id = message.from_user.id
    current = user_layout_settings.get(user_id, "auto")
    
    style_names = {
        "auto": "🤖 Auto (Default)",
        "vertical": "↕️ Single Column (Vertical)",
        "horizontal": "↔️ Single Row (Horizontal)",
        "grid": "🔲 Balanced Grid"
    }
    status = style_names.get(current, "🤖 Auto (Default)")

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🤖 Auto", callback_data="l_auto"),
        types.InlineKeyboardButton("🔲 Balanced Grid", callback_data="l_grid")
    )
    markup.add(
        types.InlineKeyboardButton("↕️ Vertical Column", callback_data="l_vertical"),
        types.InlineKeyboardButton("↔️ Horizontal Row", callback_data="l_horizontal")
    )
    
    bot.send_message(
        message.chat.id,
        f"📐 **Collage Layout Settings**\n\nCurrent style: *{status}*\n\nChoose a layout format for your collage:",
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data in [
    'wm_on', 'wm_off', 'wm_text', 'wm_color_menu', 'wm_back',
    'wmc_black', 'wmc_white', 'wmc_red', 'wmc_yellow',
    'q_document', 'q_photo',
    'l_auto', 'l_grid', 'l_vertical', 'l_horizontal'
])
def handle_callback_query(call):
    """Handles all inline button clicks for watermark, quality, and layout settings."""
    if call.from_user.id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "⛔ Denied.")
        return

    user_id = call.from_user.id

    if call.data == "wm_on":
        user_watermark_settings[user_id] = True
        save_user_settings()
        current_text = user_watermark_text.get(user_id, "Galley-La")
        current_color_key = user_watermark_colors.get(user_id, "black")
        color_info = WATERMARK_COLORS.get(current_color_key, WATERMARK_COLORS["black"])
        current_color_name = color_info["name"]
        bot.edit_message_text(
            f"🔖 **Watermark Settings**\n\nCurrent status: ✅ ON\nCurrent text: `{current_text}`\nCurrent color: {current_color_name}\n\n_Watermark will be applied to your collages._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Watermark turned ON ✅")
    elif call.data == "wm_off":
        user_watermark_settings[user_id] = False
        save_user_settings()
        current_text = user_watermark_text.get(user_id, "Galley-La")
        current_color_key = user_watermark_colors.get(user_id, "black")
        color_info = WATERMARK_COLORS.get(current_color_key, WATERMARK_COLORS["black"])
        current_color_name = color_info["name"]
        bot.edit_message_text(
            f"🔖 **Watermark Settings**\n\nCurrent status: ❌ OFF\nCurrent text: `{current_text}`\nCurrent color: {current_color_name}\n\n_Collages will be generated without watermark._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Watermark turned OFF ❌")
    elif call.data == "wm_color_menu":
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("⚫ Black", callback_data="wmc_black"),
            types.InlineKeyboardButton("⚪ White", callback_data="wmc_white")
        )
        markup.add(
            types.InlineKeyboardButton("🔴 Red", callback_data="wmc_red"),
            types.InlineKeyboardButton("🟡 Yellow", callback_data="wmc_yellow")
        )
        markup.add(
            types.InlineKeyboardButton("⬅️ Back", callback_data="wm_back")
        )
        
        current_color_key = user_watermark_colors.get(user_id, "black")
        color_info = WATERMARK_COLORS.get(current_color_key, WATERMARK_COLORS["black"])
        current_color_name = color_info["name"]
        
        bot.edit_message_text(
            f"🎨 **Select Watermark Color**\n\nCurrent color: {current_color_name}\n\nChoose a color that stands out on your images:",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
    elif call.data == "wm_back":
        current = user_watermark_settings.get(user_id, True)
        current_text = user_watermark_text.get(user_id, "Galley-La")
        current_color_key = user_watermark_colors.get(user_id, "black")
        color_info = WATERMARK_COLORS.get(current_color_key, WATERMARK_COLORS["black"])
        current_color_name = color_info["name"]
        status = "✅ ON" if current else "❌ OFF"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Turn ON", callback_data="wm_on"),
            types.InlineKeyboardButton("❌ Turn OFF", callback_data="wm_off")
        )
        markup.add(
            types.InlineKeyboardButton("✏️ Custom Text", callback_data="wm_text"),
            types.InlineKeyboardButton("🎨 Choose Color", callback_data="wm_color_menu")
        )
        bot.edit_message_text(
            f"🔖 **Watermark Settings**\n\nCurrent status: {status}\nCurrent text: `{current_text}`\nCurrent color: {current_color_name}\n\nChoose an option below:",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
    elif call.data.startswith("wmc_"):
        color_key = call.data.split("_")[1]
        user_watermark_colors[user_id] = color_key
        save_user_settings()
        
        color_info = WATERMARK_COLORS.get(color_key, WATERMARK_COLORS["black"])
        color_name = color_info["name"]
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("⚫ Black", callback_data="wmc_black"),
            types.InlineKeyboardButton("⚪ White", callback_data="wmc_white")
        )
        markup.add(
            types.InlineKeyboardButton("🔴 Red", callback_data="wmc_red"),
            types.InlineKeyboardButton("🟡 Yellow", callback_data="wmc_yellow")
        )
        markup.add(
            types.InlineKeyboardButton("⬅️ Back", callback_data="wm_back")
        )
        bot.edit_message_text(
            f"🎨 **Select Watermark Color**\n\nCurrent color: {color_name}\n\nChoose a color that stands out on your images:",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id, f"Watermark color set to {color_name} 🎨")
    elif call.data == "wm_text":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        msg = bot.send_message(
            call.message.chat.id,
            "🔤 Reply with the text for your custom watermark:",
        )
        bot.register_next_step_handler(msg, receive_custom_watermark_text)
        bot.answer_callback_query(call.id, "Enter your custom text ✏️")
    elif call.data == "q_document":
        user_quality_settings[user_id] = "document"
        save_user_settings()
        bot.edit_message_text(
            "⚡ **Image Quality & Format Settings**\n\nCurrent mode: 📄 Document (High Quality)\n\n_Collages will be sent as uncompressed files to keep maximum detail._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Document mode 📄")
    elif call.data == "q_photo":
        user_quality_settings[user_id] = "photo"
        save_user_settings()
        bot.edit_message_text(
            "⚡ **Image Quality & Format Settings**\n\nCurrent mode: 🖼️ Photo (Compressed/Fast)\n\n_Collages will be sent as standard images for quick in-chat previews and easy sharing._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Photo mode 🖼️")
    elif call.data == "l_auto":
        user_layout_settings[user_id] = "auto"
        save_user_settings()
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: 🤖 Auto (Default)\n\n_The bot will dynamically pick the best layout for your screenshots._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Auto layout 🤖")
    elif call.data == "l_grid":
        user_layout_settings[user_id] = "grid"
        save_user_settings()
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: 🔲 Balanced Grid\n\n_Images will be distributed evenly in a grid (e.g. 2x2, 3x3)._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Balanced Grid 🔲")
    elif call.data == "l_vertical":
        user_layout_settings[user_id] = "vertical"
        save_user_settings()
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: ↕️ Single Column (Vertical)\n\n_Every image will be placed in a single row stacked vertically._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Vertical Column ↕️")
    elif call.data == "l_horizontal":
        user_layout_settings[user_id] = "horizontal"
        save_user_settings()
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: ↔️ Single Row (Horizontal)\n\n_All images will be placed next to each other in a single horizontal row._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Horizontal Row ↔️")

def receive_custom_watermark_text(message):
    """Captures the user's custom watermark text from the next message."""
    if message.from_user.id not in ALLOWED_USERS: return

    user_id = message.from_user.id
    custom_text = message.text.strip() if message.text else None

    if not custom_text:
        bot.reply_to(message, "❌ Invalid input. Please use /watermark and try again.")
        return

    user_watermark_text[user_id] = custom_text
    # Also auto-enable watermark when setting custom text
    user_watermark_settings[user_id] = True
    save_user_settings()
    bot.reply_to(
        message,
        f"✅ Custom watermark set to: `{custom_text}`\n\n_Watermark has been turned ON automatically._",
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['generate'])
def process_listing(message):
    if message.from_user.id not in ALLOWED_USERS: return

    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)
    collage_path = os.path.join(user_folder, "final_collage.jpg")

    # Double check folder and content existence
    if not os.path.exists(user_folder) or not os.listdir(user_folder):
        bot.reply_to(message, "Send photos first, then /generate.")
        return

    # Count the photos in the user's folder (exclude any previously generated collage)
    image_files = [f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg')) and f != "final_collage.jpg"]
    num_images = len(image_files)

    caption_text = f"⚙️ Building collage from {num_images} images..."
    loading_gif = "https://www.gifcen.com/wp-content/uploads/2022/11/one-piece-gif-1.gif" # One Piece loading spinner

    # Send the loading animation (or fallback to message)
    try:
        m = bot.send_animation(message.chat.id, loading_gif, caption=caption_text)
    except Exception:
        m = bot.send_message(message.chat.id, caption_text)

    try:
        watermark_on = user_watermark_settings.get(message.from_user.id, True)
        wm_text = user_watermark_text.get(message.from_user.id, "Galley-La")
        layout_pref = user_layout_settings.get(message.from_user.id, "auto")
        wm_color = user_watermark_colors.get(message.from_user.id, "black")
        collage_result = create_collage(
            user_folder, 
            collage_path, 
            watermark_enabled=watermark_on, 
            watermark_text=wm_text, 
            layout_style=layout_pref,
            watermark_color=wm_color
        )
        if not collage_result:
            try:
                bot.delete_message(m.chat.id, m.message_id)
            except: pass
            bot.send_message(message.chat.id, "Error building collage.")
            return
            
        _, final_path = collage_result
        
        # Send collage based on user's format preference
        quality_pref = user_quality_settings.get(message.from_user.id, "document")
        wm_label = "watermarked " if watermark_on else ""

        with open(final_path, 'rb') as file_data:
            if quality_pref == "photo":
                bot.send_photo(
                    message.chat.id,
                    file_data,
                    caption=f"Here's your {wm_label}collage!",
                    timeout=90
                )
            else:
                bot.send_document(
                    message.chat.id,
                    file_data,
                    caption=f"Here's your high-quality {wm_label}image!",
                    visible_file_name="Galley_La_Collage.jpg",
                    timeout=90
                )

        # Free up loading message to prevent chat clutter after collage has been sent
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except: pass

    except Exception as e:
        bot.reply_to(message, f"An error occurred: {str(e)}")
        # Delete the loading message if it was sent
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except: pass
        # Safe Cleanup on collage failure
        try:
            if os.path.exists(user_folder):
                safe_delete_folder(user_folder)
        except: pass
        return  # Stop here — do not send misleading "Session cleared" after an error

    # CLOUD FIX: Safe Cleanup prevents random Windows permissions/ghost errors from locking folders
    try:
        if os.path.exists(user_folder):
            safe_delete_folder(user_folder)
    except Exception as e:
        # Prints to Koyeb console, does not notify user
        print(f"[*] Cleanup warning for {user_id}: {e}")
    
    bot.send_message(message.chat.id, "✅ Session cleared.")

# =========================================================
# 5. KOYEB KEEP-ALIVE SERVER (WEBSERVER FOR HEALTH CHECKS)
# =========================================================
app = Flask(__name__)

@app.route('/')
def home():
    # UptimeRobot checks this URL to prevent Koyeb sleep mode
    return "Eldorado Bot is awake and running!"

def run_server():
    # Koyeb requires apps to bind to a specific port environment variable
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_awake():
    # Run the web server in a background thread so the main bot isn't blocked
    t = Thread(target=run_server)
    t.daemon = True
    t.start()

# =========================================================
# 6. EXECUTION
# =========================================================
if __name__ == "__main__":
    # Ensure root temp directory exists on cloud start
    if not os.path.exists(TEMP_DIR): os.makedirs(TEMP_DIR)
    
    # Load persisted user settings from previous sessions
    load_user_settings()
    
    # Start the keep-awake web server
    keep_awake()
    
    print("[*] Galley-La Bot is securely running... Press Ctrl+C to stop.")
    
    # ADVANCED FIX: Sever any ghost connections from previous deployments
    bot.remove_webhook() 
    bot.infinity_polling()
