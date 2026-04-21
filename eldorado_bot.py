import os
import math
import shutil
import time
import io
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont
from google import genai
from flask import Flask           # <--- NEW
from threading import Thread

# =========================================================
# 1. CONFIGURATION & ACCESS CONTROL
# =========================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Only users in this list can interact with the bot. 
# Add your numeric Telegram IDs here.
ALLOWED_USERS = [5282482434, 7871741290, 1985905883, 929088783] 

# Initialize APIs
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY)

# Temporary storage for processing images (Using Absolute Paths for the Cloud)
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
    
    # Remove timestamps older than 60 seconds
    api_call_timestamps = [t for t in api_call_timestamps if current_time - t < 60]
    
    if len(api_call_timestamps) >= 14:
        wait_time = int(60 - (current_time - api_call_timestamps[0]))
        return False, wait_time
        
    return True, 0

# =========================================================
# 3. IMAGE PROCESSING & WATERMARKING
# =========================================================
def apply_watermark(image, store_name="Eldorado Store"):
    """Adds a semi-transparent watermark to the bottom right."""
    draw = ImageDraw.Draw(image, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", 50) 
    except IOError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), store_name, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    img_width, img_height = image.size
    x = img_width - text_width - 30
    y = img_height - text_height - 30
    
    draw.rectangle([x - 15, y - 15, x + text_width + 15, y + text_height + 15], fill=(0, 0, 0, 160))
    draw.text((x, y), store_name, font=font, fill=(255, 255, 255, 230))
    
    return image

def create_collage(image_folder, output_path):
    """Stitches images into a grid and applies a watermark."""
    image_files = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    if not image_files: return None

    # THE FIX: 100% decouple images from the hard drive into RAM
    images = []
    for f in image_files:
        file_path = os.path.join(image_folder, f)
        with open(file_path, 'rb') as file_data:
            img = Image.open(io.BytesIO(file_data.read()))
            images.append(img)

    cols = math.ceil(math.sqrt(len(images)))
    rows = math.ceil(len(images) / cols)

    width, height = images[0].size
    collage = Image.new('RGB', (cols * width, rows * height), color=(0,0,0))

    for index, img in enumerate(images):
        img = img.resize((width, height))
        row = index // cols
        col = index % cols
        collage.paste(img, (col * width, row * height))

    # Apply watermark before saving
    collage = apply_watermark(collage, "YOUR STORE NAME")
    collage.save(output_path, quality=95)
    return collage, output_path

# =========================================================
# 4. AI LISTING GENERATION
# =========================================================
def generate_listing_description(image_folder):
    """Sends the raw, uncompressed screenshots to Gemini 2.5 Flash."""
    image_files = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    
    # THE FIX: 100% decouple images from the hard drive into RAM
    raw_images = []
    for f in image_files:
        file_path = os.path.join(image_folder, f)
        with open(file_path, 'rb') as file_data:
            img = Image.open(io.BytesIO(file_data.read()))
            raw_images.append(img)
    
    prompt = """
    You are an expert Pokemon GO account seller on Eldorado and a master digital marketer. 
    Analyze these screenshots from a single Pokemon GO account and extract all visible stats, items, and rare Pokemon.
    
    CRITICAL INSTRUCTIONS FOR SCALING:
    1. ADJUST THE TITLE HYPE: Evaluate the account's tier. 
       - If Level 45+ with massive stardust/shinies: "🔥 ULTRA STACKED LEVEL [Level] ACCOUNT 🔥" and "🚀 ENDGAME ACCOUNT".
       - If Level 35-44 with a good collection: "⚡ HIGH TIER LEVEL [Level] ACCOUNT ⚡" and "🚀 MID-GAME ACCOUNT".
       - If below Level 35: "🌟 GREAT STARTER LEVEL [Level] ACCOUNT 🌟" and "🚀 BUDGET / STARTER ACCOUNT".
    2. DELETE MISSING DATA (CRITICAL): If a specific stat or category is not visible, MUST completely delete that entire bullet point or section. Do NOT leave empty brackets and do NOT write "0".
    3. ADD MISSING RARES: Dynamically add highly valuable assets not explicitly listed in the template.
    4. ZERO HALLUCINATIONS: Do not guess regional forms or costumes. Be exact based on the pixels.
    5. PRICE ESTIMATION: At the bottom, output a realistic USD selling price range based on standard market rates and one sentence explaining why.
    6. SOCIAL MEDIA COPY: Generate a catchy title, 2-3 sentence description, and 5-7 hashtags optimized for Pinterest.

    TEMPLATE TO ADAPT:

    [Insert Adaptive Title Based on Rules Above]
    🌟 [Total] XP 🌟
    💯 [Total] STARDUST 💯 | ✨ [Number]+ SHINY ✨
    🐉 [Number]+ LEGENDARY 🐉 

    ━━━━━━━━━━━━━━━━━━━━━━━

    [Insert Adaptive Account Tier Subheader]

    ━━━━━━━━━━━━━━━━━━━━━━━

    💎 CORE STATS
    • Level [Level] ⚡
    • [Total] Total XP 🌟
    • [Total] Stardust 💰
    • [Number] Pokémon Storage 📦
    • [Number] Item Storage 🎒
    • [Number] Lucky Pokémon 🍀
    • [Number] Master Balls ⚫
    • [Number] PokéCoins 💰
    • [Number] Premium Raid Passes 🎟️

    ━━━━━━━━━━━━━━━━━━━━━━━

    🐉 LEGENDARY / MYTHICAL / ULTRA BEASTS
    • [Number] Legendary
    • [Number] Mythical

    🔥 Includes:
    • [List notable Legendaries/Mythicals found in images]

    ━━━━━━━━━━━━━━━━━━━━━━━

    ✨ SHINY COLLECTION
    • [Number] Shadow Shiny 🔥
    • [Number] Costume Shiny 🎭
    • [List notable Shinies found in images]

    ━━━━━━━━━━━━━━━━━━━━━━━

    🍂 BACKGROUND COLLECTION
    • [Number] Location Background
    • [Number] Special Background
    🌍 Locations: [List locations seen]
    🔥 Highlights: [List specific Pokemon with backgrounds]

    ━━━━━━━━━━━━━━━━━━━━━━━

    ⚡ FUSED / SHUNDO / HUNDO FLEX
    • [List Shundos, Hundos, and Fused Pokemon seen in images]

    ━━━━━━━━━━━━━━━━━━━━━━━

    🌑 RARE EVENT / SHADOW / SPECIAL
    • [List rare Shadow Shinies, XXL/XXS, Dynamax, or highly specific event Pokemon/costumes]

    ━━━━━━━━━━━━━━━━━━━━━━━

    🏆 MEDALS & POSES
    • [Number] Platinum Medals 🥇
    🎭 Rare Poses: [List avatar poses seen in images]

    ━━━━━━━━━━━━━━━━━━━━━━━

    💎 EXTRA VALUE
    • Name Change Available ✏️
    • Massive trade inventory

    ━━━━━━━━━━━━━━━━━━━━━━━

    💰 ESTIMATED MARKET VALUE
    • Suggested Price: $[Low] - $[High] USD
    • Valuation Reason: [One short sentence explaining the price]

    ━━━━━━━━━━━━━━━━━━━━━━━
    📌 PINTEREST / SOCIAL MEDIA COPY
    
    **Title:** [Write a catchy, click-optimized title under 100 characters]
    **Description:** [Write 2-3 sentences driving urgency, highlighting the absolute rarest asset, and ending with a clear Call-To-Action]
    **Hashtags:** [Generate 5-7 highly relevant, high-traffic tags]
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
        bot.reply_to(message, f"⛔ Access Denied. Your actual Telegram ID is: {message.from_user.id}")
        return

    welcome_text = (
        "🤖 **Eldorado Listing Bot is Online!**\n\n"
        "**How to use me:**\n"
        "1. Send me all the screenshots of the account.\n"
        "2. Reply with the command /generate.\n"
        "3. I will build the watermarked collage and ask if you want the text generated."
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode='Markdown')

@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    if message.from_user.id not in ALLOWED_USERS: return

    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)
    
    # CRITICAL CLOUD FIX: Force the creation of the parent and user directories
    os.makedirs(user_folder, exist_ok=True)

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    file_path = os.path.join(user_folder, f"{message.photo[-1].file_id}.jpg")
    
    # Now that we guarantee the folder exists via absolute path, save the file
    with open(file_path, 'wb') as new_file:
        new_file.write(downloaded_file)

    bot.reply_to(message, "Screenshot received! Send more, or type /generate.")
    
@bot.message_handler(commands=['generate'])
def process_listing(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, f"⛔ Access Denied. Your actual Telegram ID is: {message.from_user.id}")
        return

    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)
    collage_path = os.path.join(user_folder, "final_collage.jpg")

    if not os.path.exists(user_folder) or not os.listdir(user_folder):
        bot.reply_to(message, "I don't have any screenshots to process! Please send some photos first.")
        return

    bot.send_message(message.chat.id, "⚙️ Processing images and building collage...")

    try:
        collage_result = create_collage(user_folder, collage_path)
        if not collage_result:
            bot.reply_to(message, "Error creating collage. Please try again.")
            return
            
        _, final_path = collage_result

        with open(final_path, 'rb') as photo:
            bot.send_photo(message.chat.id, photo, caption="Here is your final watermarked image!")

        markup = types.InlineKeyboardMarkup()
        btn_yes = types.InlineKeyboardButton("✅ Yes, generate text", callback_data="desc_yes")
        btn_no = types.InlineKeyboardButton("❌ No, skip it", callback_data="desc_no")
        markup.add(btn_yes, btn_no)

        bot.send_message(message.chat.id, "Do you want me to write the Eldorado & Social Media copy?", reply_markup=markup)

    except Exception as e:
        bot.reply_to(message, f"An error occurred: {str(e)}")
        # Safe Cleanup
        try:
            if os.path.exists(user_folder):
                shutil.rmtree(user_folder)
        except Exception as cleanup_error:
            print(f"[*] Minor warning: Could not delete folder {user_folder}: {cleanup_error}")

@bot.callback_query_handler(func=lambda call: call.data in ['desc_yes', 'desc_no'])
def handle_description_choice(call):
    if call.from_user.id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "⛔ Access Denied.")
        return
        
    user_id = str(call.message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)

    if call.data == "desc_yes":
        can_proceed, wait_time = check_rate_limit()
        if not can_proceed:
            bot.answer_callback_query(call.id, f"⏳ Bot is cooling down to prevent API limits. Please click Yes again in {wait_time} seconds.", show_alert=True)
            return

        api_call_timestamps.append(time.time())
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, "🧠 Analyzing high-res screenshots with Gemini API...")
        
        try:
            description = generate_listing_description(user_folder)
            bot.send_message(call.message.chat.id, f"**Eldorado Listing Text:**\n\n{description}", parse_mode='Markdown')
        except Exception as e:
            bot.send_message(call.message.chat.id, f"An error occurred with the AI: {str(e)}")
            
    elif call.data == "desc_no":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, "Skipping AI generation.")

    # THE FIX: Safe Cleanup catches random Windows permissions errors
    try:
        if os.path.exists(user_folder):
            shutil.rmtree(user_folder)
    except Exception as cleanup_error:
        print(f"[*] Minor warning: Could not delete folder {user_folder}: {cleanup_error}")
    
    bot.send_message(call.message.chat.id, "✅ Session cleared. Ready for the next account!")

# =========================================================
# 6. KOYEB KEEP-ALIVE SERVER
# =========================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Eldorado Bot is awake and running!"

def run_server():
    # Koyeb requires apps to bind to a specific port
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_awake():
    t = Thread(target=run_server)
    t.daemon = True
    t.start()

# =========================================================
# 7. EXECUTION
# =========================================================
if __name__ == "__main__":
    if not os.path.exists(TEMP_DIR): os.makedirs(TEMP_DIR)
    
    # Start the invisible web server
    keep_awake()
    
    print("[*] Eldorado Bot is securely running... Press Ctrl+C to stop.")
    bot.infinity_polling()
