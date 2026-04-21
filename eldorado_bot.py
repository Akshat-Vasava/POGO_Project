import os
import math
import shutil
import time
import io
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from google import genai
from flask import Flask
from threading import Thread

# =========================================================
# 1. CONFIGURATION & ACCESS CONTROL (CLOUD-SECURE)
# =========================================================
# Remember to set these on Koyeb, NOT in the code!
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Hardcode your authorized numeric IDs here
ALLOWED_USERS = [123456789, 987654321] 

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY)

# Absolute path setup for cloud file system stability
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_user_data")

# Global tracker for the 15 RPM limit
api_call_timestamps = []

# =========================================================
# 2. RATE LIMITER
# =========================================================
def check_rate_limit():
    """Ensures we do not exceed 14 requests per 60 seconds."""
    global api_call_timestamps
    current_time = time.time()
    api_call_timestamps = [t for t in api_call_timestamps if current_time - t < 60]
    
    if len(api_call_timestamps) >= 14:
        wait_time = int(60 - (current_time - api_call_timestamps[0]))
        return False, wait_time
        
    return True, 0

# =========================================================
# 3. ADVANCED IMAGE PROCESSING (DIAGONAL WATERMARK & RAM OPTIMIZED)
# =========================================================
def apply_watermark(image, store_name="Galley-La"):
    """Adds a massive, semi-transparent, diagonal watermark."""
    img_w, img_h = image.size
    
    # 1. Start with a black text layer
    txt = Image.new('L', (img_w, img_h))
    d = ImageDraw.Draw(txt)
    
    # 2. Configure font (dynamic size based on image width)
    # Goal: text should be ~25% as tall as the image
    font_size = int(img_h * 0.15) 
    try:
        # Most servers don't have Arial, try different fonts, fall back to default
        font = ImageFont.truetype("arial.ttf", font_size)
    except IOError:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        except IOError:
            # Last resort: very small and ugly, but won't crash
            font = ImageFont.load_default()

    # 3. Calculate text center for centering on the canvas
    bbox = d.textbbox((0, 0), store_name, font=font)
    t_w = bbox[2] - bbox[0]
    t_h = bbox[3] - bbox[1]
    
    # Draw text centered (starts at 255/pure white which is pure opacity)
    d.text(((img_w - t_w) / 2, (img_h - t_h) / 2), store_name, font=font, fill=255)
    
    # 4. ROTATE: Turn the mask 45 degrees
    # expand=True keeps the text from getting cut off on corners
    # (Pillow's rotate with expand increases the canvas size)
    rotated_txt = txt.rotate(45, expand=1, resample=Image.BICUBIC)
    
    # 5. Crop it back down to the original image dimensions
    r_w, r_h = rotated_txt.size
    left = (r_w - img_w) / 2
    top = (r_h - img_h) / 2
    cropped_txt = rotated_txt.crop((left, top, left + img_w, top + img_h))
    
    # 6. ADJUST OPACITY (Make it semi-transparent)
    # 255 is solid. 100-120 is very transparent, similar to your example image.
    alpha = ImageEnhance.Brightness(cropped_txt).enhance(120 / 255)
    
    # 7. Apply the transparent text mask to the main image using solid white
    white = Image.new('RGB', (img_w, img_h), 'white')
    image.paste(white, (0, 0), alpha)
    
    return image

def create_collage(image_folder, output_path):
    """Stitches images one-by-one with compression to prevent Cloud OOM."""
    image_files = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    if not image_files: return None

    # Get dimensions from first image, then close
    with Image.open(os.path.join(image_folder, image_files[0])) as first_img:
        base_width, base_height = first_img.size

    # RAM SAVER: Shrink resolution by 50%
    width = int(base_width * 0.5)
    height = int(base_height * 0.5)

    cols = math.ceil(math.sqrt(len(image_files)))
    rows = math.ceil(len(image_files) / cols)
    collage = Image.new('RGB', (cols * width, rows * height), color=(0,0,0))

    # RAM SAVER: Process one image at a time
    for index, f in enumerate(image_files):
        file_path = os.path.join(image_folder, f)
        with Image.open(file_path) as img:
            img_resized = img.resize((width, height))
            row = index // cols
            col = index % cols
            collage.paste(img_resized, (col * width, row * height))

    # Apply the NEW diagonal watermark
    # Change "Eldorado Store" to whatever you want stamped!
    collage = apply_watermark(collage, "Galley-La")
    
    collage.save(output_path, quality=85)
    return collage, output_path

# =========================================================
# 4. AI LISTING GENERATION
# =========================================================
def generate_listing_description(image_folder):
    """Sends AI-optimized thumbnails to Gemini to save cloud RAM."""
    image_files = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    
    # RAM SAVER: Decouple from hard drive and compress to 1024px before holding in RAM
    raw_images = []
    for f in image_files:
        file_path = os.path.join(image_folder, f)
        # Decouple instantly from hard drive
        with open(file_path, 'rb') as file_data:
            with Image.open(io.BytesIO(file_data.read())) as img:
                # Shrink for AI payload, save hundreds of megabytes of server RAM
                img.thumbnail((1024, 1024))
                raw_images.append(img.copy())
            
    prompt = """
    You are an expert Pokemon GO account seller on Eldorado and a master digital marketer...
    # (Keep your entire long prompt text exactly the same here!)
    """
    
    content_to_send = [prompt] + raw_images
    response = client.models.generate_content(
        model="gemini-2.5-flash", 
        contents=content_to_send
    )
    return response.text

# =========================================================
# 5. TELEGRAM BOT HANDLERS
# =========================================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if message.from_user.id not in ALLOWED_USERS:
        # Diagnostic mode: Tell authorized users who have wrong IDs what they are
        bot.reply_to(message, f"⛔ Access Denied. Your numeric Telegram ID is: {message.from_user.id}")
        return

    welcome_text = (
        "🤖 **Eldorado Listing Bot (Cloud Optimized) is Online!**\n\n"
        "1. Send screenshots.\n"
        "2. Type /generate.\n"
        "3. I'll build a memory-safe collage and prompt for AI text."
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

    # Simple reply to not spam their chat while uploading 10 images
    bot.reply_to(message, "Screenshot received!")

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

    m = bot.send_message(message.chat.id, "⚙️ Building memory-safe collage...")

    try:
        collage_result = create_collage(user_folder, collage_path)
        if not collage_result:
            bot.edit_message_text("Error building collage.", m.chat.id, m.message_id)
            return
            
        _, final_path = collage_result
        
        # Free up 'm' message to prevent chat clutter
        bot.delete_message(m.chat.id, m.message_id)

        # Decouple collage file instantly from hard drive before sending
        with open(final_path, 'rb') as file_data:
            # We send directly without Pillow to save even more RAM
            bot.send_photo(message.chat.id, file_data, caption="Here's your watermarked image! Generate AI text?")

        # Prompt for AI text generation
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Yes", callback_data="desc_yes"))
        markup.add(types.InlineKeyboardButton("❌ No", callback_data="desc_no"))
        bot.send_message(message.chat.id, "Generate Eldorado & Social Media copy?", reply_markup=markup)

    except Exception as e:
        bot.reply_to(message, f"An error occurred: {str(e)}")
        # Safe Cleanup on collage failure
        try:
            if os.path.exists(user_folder):
                shutil.rmtree(user_folder)
        except: pass

@bot.callback_query_handler(func=lambda call: call.data in ['desc_yes', 'desc_no'])
def handle_description_choice(call):
    if call.from_user.id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "⛔ Denied.")
        return
        
    user_id = str(call.message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)

    if call.data == "desc_yes":
        can_proceed, wait_time = check_rate_limit()
        if not can_proceed:
            bot.answer_callback_query(call.id, f"⏳ Cooldown. Try again in {wait_time}s.", show_alert=True)
            return

        api_call_timestamps.append(time.time())
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, "🧠 Analyzing compressed thumbnails...")
        
        try:
            description = generate_listing_description(user_folder)
            bot.send_message(call.message.chat.id, f"**Eldorado Listing Text:**\n\n{description}", parse_mode='Markdown')
        except Exception as e:
            # Catch 503 errors and rate limits gracefully
            bot.send_message(call.message.chat.id, f"AI Error: {str(e)}")
            
    elif call.data == "desc_no":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, "AI text skipped.")

    # CLOUD FIX: Safe Cleanup prevents random Windows permissions/ghost errors from locking folders
    try:
        if os.path.exists(user_folder):
            shutil.rmtree(user_folder)
    except Exception as e:
        # Prints to Koyeb console, does not notify user
        print(f"[*] Cleanup warning for {user_id}: {e}")
    
    bot.send_message(call.message.chat.id, "✅ Session cleared.")

# =========================================================
# 6. KOYEB KEEP-ALIVE SERVER (WEBSERVER FOR HEALTH CHECKS)
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
# 7. EXECUTION
# =========================================================
if __name__ == "__main__":
    # Ensure root temp directory exists on cloud start
    if not os.path.exists(TEMP_DIR): os.makedirs(TEMP_DIR)
    
    # Start the keep-awake web server
    keep_awake()
    
    print("[*] Eldorado Bot is securely running... Press Ctrl+C to stop.")
    
    # ADVANCED FIX: Sever any ghost connections from previous deployments
    bot.remove_webhook() 
    bot.infinity_polling()
