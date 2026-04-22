import os
import io
import math
import gc
import threading
import telebot
from flask import Flask
from PIL import Image, ImageDraw, ImageFont
from google import genai
from google.genai import types
# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")

bot = telebot.TeleBot(BOT_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# Dictionary to hold the tiny file_ids instead of heavy images
user_sessions = {}

# ================= FLASK KEEP-ALIVE SERVER =================
app = Flask(__name__)

@app.route('/')
def home():
    return "Galley-La Bot is awake and running!"

def run_server():
    # Koyeb requires port 8000 or 8080 usually; we bind to 0.0.0.0
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ================= GEMINI AI ENGINE =================
def generate_listing_description(collage_image):
    """Uses Gemini to analyze the stats and write the Eldorado listing."""
    try:
        # Initialize the new Google GenAI client
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        prompt = """
        You are an expert copywriter for an Eldorado.gg storefront specializing in Pokémon GO accounts.
        Analyze this collage of Pokémon GO screenshots. 
        Write a punchy, high-converting listing description highlighting the best Pokémon, CPs, and rare stats shown (like Shinies, Legendaries, or 100% IVs if visible).
        Keep it organized with bullet points, use gaming emojis, and end with a strong call to action to buy the account safely.
        """
        
        # Call the model using the new syntax
        response = client.models.generate_content(
            model='gemini-2.5-flash', # Upgraded to the latest 2.5 flash model
            contents=[prompt, collage_image]
        )
        return response.text
    except Exception as e:
        return f"⚠️ AI Generation Failed: {e}\n\n(Google's servers might be overloaded right now. You can try again later.)"

# ================= IMAGE PROCESSING =================
def apply_watermark(base_image, watermark_text="Galley-La"):
    """Applies a diagonal, transparent watermark scaled to the image width."""
    watermark = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(watermark)
    
    font_size = int(base_image.width / 10)
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except IOError:
        font = ImageFont.load_default()
        
    bbox = draw.textbbox((0, 0), watermark_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    x = (base_image.width - text_width) / 2
    y = (base_image.height - text_height) / 2
    
    draw.text((x, y), watermark_text, font=font, fill=(0, 0, 0, 128))
    watermark = watermark.rotate(30, resample=Image.BICUBIC)
    
    return Image.alpha_composite(base_image.convert('RGBA'), watermark).convert('RGB')

def create_collage(imgs):
    """Builds a seamless masonry collage with the forced 3-2 layout for 5 images."""
    n = len(imgs)
    
    # CUSTOM LAYOUT LOGIC
    if n == 5:
        layout = [3, 2]
    elif n <= 4:
        layout = [math.ceil(n/2), n // 2] if n > 1 else [1]
    else:
        rows_count = 3
        base = n // rows_count
        extra = n % rows_count
        layout = [base] * rows_count
        for i in range(extra): 
            layout[i] += 1

    canvas_width = 1500 # Safe resolution for 512MB Koyeb RAM
    idx = 0
    rows_data = []

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

    total_height = sum(h for _, h in rows_data)
    collage = Image.new("RGB", (canvas_width, total_height), (255, 255, 255))

    y = 0
    for row, h in rows_data:
        x = 0
        for img in row:
            collage.paste(img, (x, y))
            x += img.width
        y += h

    # Clean up individual raw images from memory
    for img in imgs: 
        img.close()

    return apply_watermark(collage, "Galley-La")

# ================= BOT COMMANDS =================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Welcome to Galley-La! ⚓\n\nSend me your high-res Pokémon GO screenshots (up to 5), and type /done when you are ready to build the collage and generate the AI listing text.")

@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    user_id = message.chat.id
    
    if user_id not in user_sessions:
        user_sessions[user_id] = []
        
    # Save ONLY the text file_id to keep RAM usage at zero
    file_id = message.photo[-1].file_id
    user_sessions[user_id].append(file_id)
    
    images_loaded = len(user_sessions[user_id])
    bot.reply_to(message, f"Image {images_loaded} saved to queue. Send more or type /done.")

@bot.message_handler(commands=['done', 'generate'])
def generate_collage(message):
    user_id = message.chat.id
    
    if user_id not in user_sessions or len(user_sessions[user_id]) == 0:
        bot.reply_to(message, "You haven't sent any images yet! Send some photos first.")
        return

    status_msg = bot.reply_to(message, "Downloading images and building your seamless collage. Please wait... ⚙️")
    
    try:
        # 1. Download images into RAM only right before processing
        imgs = []
        for file_id in user_sessions[user_id]:
            file_info = bot.get_file(file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            imgs.append(Image.open(io.BytesIO(downloaded_file)).convert("RGB"))
            
        # 2. Build the collage
        final_collage = create_collage(imgs)
        
        # 3. Save to a temporary memory buffer
        bio = io.BytesIO()
        bio.name = 'Galley_La_Collage.jpg'
        final_collage.save(bio, 'JPEG', quality=90)
        bio.seek(0)
        
        # 4. Send the high-res document
        bot.edit_message_text("Uploading high-res document...", chat_id=user_id, message_id=status_msg.message_id)
        bot.send_document(
            user_id, 
            document=bio, 
            caption="Here is your high-quality, watermarked collage!",
            visible_file_name="Galley_La_Collage.jpg"
        )
        
        # 5. Generate AI Text using the compiled collage
        bot.edit_message_text("Writing Eldorado listing with Gemini AI... 🤖", chat_id=user_id, message_id=status_msg.message_id)
        
        # We pass the PIL Image object directly to Gemini Vision
        ai_description = generate_listing_description(final_collage)
        
        bot.send_message(user_id, f"**Eldorado Listing:**\n\n{ai_description}", parse_mode="Markdown")
        bot.delete_message(chat_id=user_id, message_id=status_msg.message_id)
        
    except Exception as e:
        bot.reply_to(message, f"An error occurred: {e}")
        
    finally:
        # 6. The Ultimate Memory Flush
        if user_id in user_sessions:
            user_sessions[user_id].clear()
        
        gc.collect()

# ================= LAUNCH =================
if __name__ == "__main__":
    # Start the Flask Keep-Alive server in a background thread
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
    
    print("Galley-La Bot is online and running...")
    bot.infinity_polling(skip_pending=True)
