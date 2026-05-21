import os
import math
import shutil
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from flask import Flask
from threading import Thread, Timer
import time
import stat

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

# Absolute path setup for cloud file system stability
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_user_data")

# Per-user watermark preference (default: ON)
# Key: user_id (int), Value: True (watermark on) / False (watermark off)
user_watermark_settings = {}

# Per-user custom watermark text (default: "Galley-La")
# Key: user_id (int), Value: custom text string
user_watermark_text = {}

# Per-user photo batch tracking (for debounced reply)
# Key: chat_id (str), Value: count of images in current batch
user_photo_count = {}
user_photo_timers = {}

# Per-user quality preference (default: "document" for high-quality document, "photo" for compressed photo)
# Key: user_id (int), Value: "document" / "photo"
user_quality_settings = {}

# =========================================================
# 3. ADVANCED IMAGE PROCESSING (DIAGONAL WATERMARK & RAM OPTIMIZED)
# =========================================================
def apply_watermark(image, store_name="Galley-La"):
    """Adds a diagonal black watermark that actually stays inside the frame."""
    img_w, img_h = image.size
    
    # 1. Create a transparent layer the exact same size as the collage
    txt_layer = Image.new('RGBA', (img_w, img_h), (255, 255, 255, 0))
    d = ImageDraw.Draw(txt_layer)
    
    # 2. THE FIX: Scale the font based on WIDTH, not height!
    # 12% of the image width keeps it prominent but safely inside the edges.
    font_size = int(img_w * 0.12) 
    
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except IOError:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        except IOError:
            font = ImageFont.load_default()

    # 3. Calculate exact center
    bbox = d.textbbox((0, 0), store_name, font=font)
    t_w = bbox[2] - bbox[0]
    t_h = bbox[3] - bbox[1]
    
    # 4. Draw the text in pure BLACK (0, 0, 0) with 100/255 opacity (semi-transparent)
    text_x = (img_w - t_w) / 2
    text_y = (img_h - t_h) / 2
    d.text((text_x, text_y), store_name, font=font, fill=(0, 0, 0, 100))
    
    # 5. Rotate the transparent layer 45 degrees (expand=0 keeps the canvas size locked)
    rotated_txt = txt_layer.rotate(45, expand=0, resample=Image.BICUBIC)
    
    # 6. Paste the watermark over the original image
    # The 'rotated_txt' acts as its own transparency mask here
    image.paste(rotated_txt, (0, 0), rotated_txt)
    
    return image

def create_collage(image_folder, output_path, watermark_enabled=True, watermark_text="Galley-La"):
    """Builds a seamless masonry collage with no black backgrounds."""
    image_files = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    if not image_files: return None

    # Load images
    imgs = [Image.open(os.path.join(image_folder, f)).convert("RGB") for f in image_files]
    n = len(imgs)
    
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
        collage = apply_watermark(collage, watermark_text)
    
    # Save with high quality
    collage.save(output_path, "JPEG", quality=95)
    return collage, output_path


# =========================================================
# 4. TELEGRAM BOT HANDLERS
# =========================================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if message.from_user.id not in ALLOWED_USERS:
        # Diagnostic mode: Tell authorized users who have wrong IDs what they are
        bot.reply_to(message, f"⛔ Access Denied. Your numeric Telegram ID is: {message.from_user.id}")
        return

    welcome_text = (
        "🤖 **Galley-La Bot is Online!**\n\n"
        "1. Send screenshots.\n"
        "2. Type /generate to build a collage.\n"
        "3. Use /clear to discard uploaded photos and start over.\n"
        "4. Use /watermark to toggle watermark settings.\n"
        "5. Use /quality to choose between high-quality document and fast photo output."
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode='Markdown')

@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    if message.from_user.id not in ALLOWED_USERS: return
    
    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)
    # CLOUD FIX: Absolute path path guaranteed folder creation
    os.makedirs(user_folder, exist_ok=True)

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    file_path = os.path.join(user_folder, f"{message.photo[-1].file_id}.jpg")
    with open(file_path, 'wb') as new_file:
        new_file.write(downloaded_file)

    # Increment photo count for this user
    user_photo_count[user_id] = user_photo_count.get(user_id, 0) + 1

    # Cancel any existing timer for this user (debounce)
    if user_id in user_photo_timers:
        user_photo_timers[user_id].cancel()

    # Start a new 2-second timer; fires only after user stops sending photos
    chat_id = message.chat.id
    def send_batch_reply():
        count = user_photo_count.pop(user_id, 0)
        user_photo_timers.pop(user_id, None)
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
    status = "✅ ON" if current else "❌ OFF"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Turn ON", callback_data="wm_on"),
        types.InlineKeyboardButton("❌ Turn OFF", callback_data="wm_off")
    )
    markup.add(
        types.InlineKeyboardButton("✏️ Custom Text", callback_data="wm_text")
    )
    bot.send_message(
        message.chat.id,
        f"🔖 **Watermark Settings**\n\nCurrent status: {status}\nCurrent text: `{current_text}`\n\nChoose an option below:",
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

@bot.callback_query_handler(func=lambda call: call.data in ['wm_on', 'wm_off', 'wm_text', 'q_document', 'q_photo'])
def handle_callback_query(call):
    """Handles all inline button clicks for watermark and quality settings."""
    if call.from_user.id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "⛔ Denied.")
        return

    user_id = call.from_user.id

    if call.data == "wm_on":
        user_watermark_settings[user_id] = True
        bot.edit_message_text(
            "🔖 **Watermark Settings**\n\nCurrent status: ✅ ON\n\n_Watermark will be applied to your collages._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Watermark turned ON ✅")
    elif call.data == "wm_off":
        user_watermark_settings[user_id] = False
        bot.edit_message_text(
            "🔖 **Watermark Settings**\n\nCurrent status: ❌ OFF\n\n_Collages will be generated without watermark._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Watermark turned OFF ❌")
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
        bot.edit_message_text(
            "⚡ **Image Quality & Format Settings**\n\nCurrent mode: 📄 Document (High Quality)\n\n_Collages will be sent as uncompressed files to keep maximum detail._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Document mode 📄")
    elif call.data == "q_photo":
        user_quality_settings[user_id] = "photo"
        bot.edit_message_text(
            "⚡ **Image Quality & Format Settings**\n\nCurrent mode: 🖼️ Photo (Compressed/Fast)\n\n_Collages will be sent as standard images for quick in-chat previews and easy sharing._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Photo mode 🖼️")

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

    # Count the photos in the user's folder
    image_files = [f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
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
        collage_result = create_collage(user_folder, collage_path, watermark_enabled=watermark_on, watermark_text=wm_text)
        if not collage_result:
            try:
                bot.delete_message(m.chat.id, m.message_id)
            except: pass
            bot.send_message(message.chat.id, "Error building collage.")
            return
            
        _, final_path = collage_result
        
        # Free up loading message to prevent chat clutter
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except: pass

        # Send collage based on user's format preference
        quality_pref = user_quality_settings.get(message.from_user.id, "document")
        wm_label = "watermarked " if watermark_on else ""

        with open(final_path, 'rb') as file_data:
            if quality_pref == "photo":
                bot.send_photo(
                    message.chat.id,
                    file_data,
                    caption=f"Here's your {wm_label}collage!"
                )
            else:
                bot.send_document(
                    message.chat.id,
                    file_data,
                    caption=f"Here's your high-quality {wm_label}image!",
                    visible_file_name="Galley_La_Collage.jpg"
                )

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
    
    # Start the keep-awake web server
    keep_awake()
    
    print("[*] Galley-La Bot is securely running... Press Ctrl+C to stop.")
    
    # ADVANCED FIX: Sever any ghost connections from previous deployments
    bot.remove_webhook() 
    bot.infinity_polling()
