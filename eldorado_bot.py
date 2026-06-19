import os
import math
import shutil
import json
import random
import io
import telebot
from telebot import types, apihelper
from PIL import Image, ImageDraw, ImageFont
from threading import Timer, Semaphore
from concurrent.futures import ThreadPoolExecutor
import gc
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
# Remember to set these on Azure, NOT in the code!
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
ADMIN_USERS = [5282482434] 
FREE_TRIAL_COLLAGES = 5
CANVAS_WIDTH = 4200  # High-resolution output width in pixels

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Global concurrency lock: only ONE collage builds at a time to cap peak RAM at ~700MB
collage_semaphore = Semaphore(1)

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
user_inactivity_timers = {}  # Tracks the 1-minute inactivity timer per user

def start_inactivity_timer(user_id, chat_id):
    """Starts a 60-second inactivity timer to auto-clear files."""
    cancel_inactivity_timer(user_id)  # Cancel any existing timer first
    
    def auto_clear():
        user_inactivity_timers.pop(user_id, None)
        user_folder = os.path.join(TEMP_DIR, user_id)
        if os.path.exists(user_folder):
            try:
                safe_delete_folder(user_folder)
                bot.send_message(
                    chat_id, 
                    "⏰ *Session expired!*\nYour uploaded photos have been automatically cleared due to 1 minute of inactivity.", 
                    parse_mode='Markdown'
                )
            except Exception as e:
                print(f"[*] Auto-clear error for {user_id}: {e}")
        user_photo_count.pop(user_id, None)

    t = Timer(60.0, auto_clear)
    user_inactivity_timers[user_id] = t
    t.start()

def cancel_inactivity_timer(user_id):
    """Cancels the inactivity timer if it exists."""
    if user_id in user_inactivity_timers:
        try:
            user_inactivity_timers[user_id].cancel()
        except:
            pass
        user_inactivity_timers.pop(user_id, None)

# Per-user quality preference (default: "document" for high-quality document, "photo" for compressed photo)
# Key: user_id (int), Value: "document" / "photo"
user_quality_settings = {}

# Per-user layout preference (default: "auto" for dynamic layout, "vertical" / "horizontal" / "grid")
# Key: user_id (int), Value: "auto" / "vertical" / "horizontal" / "grid"
user_layout_settings = {}

# Per-user collage counts and premium status
# Key: user_id (int)
user_collage_count = {}
premium_users = {}

# Per-user file size limit in MB (default: 2)
# Key: user_id (int), Value: integer 0-10
user_limit_settings = {}

# =========================================================
# DATABASE INTEGRATION (MONGODB ATLAS)
# =========================================================
MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI")

# Fallback: Parse local .env file if running locally without the environment variable set
if not MONGO_URI:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    if key.strip() in ("MONGO_URI", "MONGODB_URI"):
                        MONGO_URI = value.strip().strip("'").strip('"')
                        os.environ["MONGO_URI"] = MONGO_URI
                        break

# Initialize MongoDB client if URI is provided
db = None
users_col = None

if MONGO_URI:
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # Force a connection check to make sure Atlas is alive
        client.server_info()
        db = client["eldorado_bot"]
        users_col = db["users"]
        print("[*] Successfully connected to MongoDB Atlas!")
    except Exception as e:
        print(f"[*] Warning: Could not connect to MongoDB: {e}. Falling back to local JSON persistence.")
        db = None
        users_col = None
else:
    print("[*] No MongoDB URI specified. Falling back to local JSON persistence.")

def get_user_data(user_id):
    """Loads all settings for a user. Returns a dictionary with all user settings.
    If MongoDB is connected, loads from MongoDB. Otherwise, falls back to the in-memory dict cache.
    """
    u_id = int(user_id)
    
    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": u_id})
            if doc:
                return {
                    "watermark_enabled": doc.get("watermark_enabled", True),
                    "watermark_text": doc.get("watermark_text", "Galley-La"),
                    "watermark_color": doc.get("watermark_color", "black"),
                    "quality": doc.get("quality", "document"),
                    "layout": doc.get("layout", "auto"),
                    "collage_count": doc.get("collage_count", 0),
                    "is_premium": doc.get("is_premium", False),
                    "limit": doc.get("limit", 2)
                }
        except Exception as e:
            print(f"[*] MongoDB error in get_user_data: {e}")
            
    # Fallback to in-memory dictionaries
    return {
        "watermark_enabled": user_watermark_settings.get(u_id, True),
        "watermark_text": user_watermark_text.get(u_id, "Galley-La"),
        "watermark_color": user_watermark_colors.get(u_id, "black"),
        "quality": user_quality_settings.get(u_id, "document"),
        "layout": user_layout_settings.get(u_id, "auto"),
        "collage_count": user_collage_count.get(u_id, 0),
        "is_premium": premium_users.get(u_id, False),
        "limit": user_limit_settings.get(u_id, 2)
    }

def set_user_data(user_id, update_dict):
    """Updates settings for a user.
    If MongoDB is connected, writes to MongoDB. Otherwise, writes to the in-memory cache and saves to local JSON.
    """
    u_id = int(user_id)
    
    if users_col is not None:
        try:
            mongo_update = {}
            for k, v in update_dict.items():
                if k == "watermark_enabled": mongo_update["watermark_enabled"] = v
                elif k == "watermark_text": mongo_update["watermark_text"] = v
                elif k == "watermark_color": mongo_update["watermark_color"] = v
                elif k == "quality": mongo_update["quality"] = v
                elif k == "layout": mongo_update["layout"] = v
                elif k == "collage_count": mongo_update["collage_count"] = v
                elif k == "is_premium": mongo_update["is_premium"] = v
                elif k == "limit": mongo_update["limit"] = v
                
            if mongo_update:
                users_col.update_one({"_id": u_id}, {"$set": mongo_update}, upsert=True)
                return True
        except Exception as e:
            print(f"[*] MongoDB error in set_user_data: {e}")
            
    # Fallback to in-memory dictionaries and save to local JSON
    for k, v in update_dict.items():
        if k == "watermark_enabled": user_watermark_settings[u_id] = v
        elif k == "watermark_text": user_watermark_text[u_id] = v
        elif k == "watermark_color": user_watermark_colors[u_id] = v
        elif k == "quality": user_quality_settings[u_id] = v
        elif k == "layout": user_layout_settings[u_id] = v
        elif k == "collage_count": user_collage_count[u_id] = v
        elif k == "is_premium": premium_users[u_id] = v
        elif k == "limit": user_limit_settings[u_id] = v
        
    save_user_settings()
    return True

def migrate_local_to_mongodb():
    """Migrates existing user settings from user_settings.json to MongoDB if MongoDB is empty."""
    if users_col is None:
        return
    try:
        if users_col.count_documents({}) > 0:
            return
            
        print("[*] MongoDB is empty. Checking for local settings to migrate...")
        if not os.path.exists(SETTINGS_FILE):
            return
            
        load_user_settings()
        
        all_user_ids = set()
        all_user_ids.update(user_watermark_settings.keys())
        all_user_ids.update(user_watermark_text.keys())
        all_user_ids.update(user_watermark_colors.keys())
        all_user_ids.update(user_quality_settings.keys())
        all_user_ids.update(user_layout_settings.keys())
        all_user_ids.update(user_collage_count.keys())
        all_user_ids.update(premium_users.keys())
        all_user_ids.update(user_limit_settings.keys())
        
        if not all_user_ids:
            return
            
        print(f"[*] Found {len(all_user_ids)} users locally. Migrating to MongoDB...")
        
        bulk_docs = []
        for u_id in all_user_ids:
            doc = {
                "_id": int(u_id),
                "watermark_enabled": user_watermark_settings.get(u_id, True),
                "watermark_text": user_watermark_text.get(u_id, "Galley-La"),
                "watermark_color": user_watermark_colors.get(u_id, "black"),
                "quality": user_quality_settings.get(u_id, "document"),
                "layout": user_layout_settings.get(u_id, "auto"),
                "collage_count": user_collage_count.get(u_id, 0),
                "is_premium": premium_users.get(u_id, False),
                "limit": user_limit_settings.get(u_id, 2)
            }
            bulk_docs.append(doc)
            
        if bulk_docs:
            users_col.insert_many(bulk_docs)
            print(f"[*] Successfully migrated {len(bulk_docs)} users to MongoDB!")
    except Exception as e:
        print(f"[*] Error during local-to-MongoDB migration: {e}")

def check_user_access(user_id):
    """Returns (allowed, reason) tuple.
    - Admins: always allowed
    - Premium users: always allowed
    - Trial users: allowed if collage_count < FREE_TRIAL_COLLAGES
    - Exhausted trial: denied with message
    """
    try:
        u_id = int(user_id)
    except (ValueError, TypeError):
        return False, "invalid_id"

    if u_id in ADMIN_USERS:
        return True, "admin"
        
    user_data = get_user_data(u_id)
    if user_data["is_premium"]:
        return True, "premium"
    
    count = user_data["collage_count"]
    if count < FREE_TRIAL_COLLAGES:
        return True, "trial"
    return False, "expired"

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
        "collage_count": {str(k): v for k, v in user_collage_count.items()},
        "premium_users": {str(k): v for k, v in premium_users.items()},
        "limit_settings": {str(k): v for k, v in user_limit_settings.items()},
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
    global user_collage_count, premium_users, user_limit_settings
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
        user_collage_count = {int(k): v for k, v in data.get("collage_count", {}).items()}
        premium_users = {int(k): v for k, v in data.get("premium_users", {}).items()}
        user_limit_settings = {int(k): v for k, v in data.get("limit_settings", {}).items()}
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
    image_files = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and f != "final_collage.jpg"]
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

    # Base width set to 4200px for high-resolution output
    canvas_width = CANVAS_WIDTH
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

        # Aggressive GC: close first-pass resized intermediates immediately
        for r in resized:
            r.close()
            
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
        # Aggressive GC: close final row images immediately after pasting
        for img in row:
            img.close()
        y += h

    # Free up source images
    for img in imgs: 
        img.close()
    gc.collect()

    # Apply the perfectly scaled diagonal watermark (if enabled)
    if watermark_enabled:
        color_info = WATERMARK_COLORS.get(watermark_color, WATERMARK_COLORS["black"])
        collage = apply_watermark(collage, watermark_text, color_rgba=color_info["rgba"])
    
    # Save with high quality
    collage.save(output_path, "JPEG", quality=95)
    return collage, output_path


# =========================================================
# 3b. PARALLEL 3-LAYOUT ENGINE & INTELLIGENT COMPRESSION
# =========================================================
def generate_layout_structure(n, variant):
    """Generates a row-structure list for a given layout variant.
    variant=1: Balanced 2-row grid
    variant=2: 3-row grid (for shuffled images)
    variant=3: 4-row grid (for shuffled images)
    """
    if n <= 1:
        return [1]

    if variant == 1:
        # Balanced 2-row split
        half = math.ceil(n / 2)
        return [half, n - half]
    elif variant == 2:
        # 3-row grid
        if n <= 2:
            return [n]
        rows_count = 3
        base = n // rows_count
        extra = n % rows_count
        layout = [base] * rows_count
        for i in range(extra):
            layout[i] += 1
        return [r for r in layout if r > 0]
    elif variant == 3:
        # 4-row grid
        if n <= 3:
            return [1] * n
        rows_count = 4
        base = n // rows_count
        extra = n % rows_count
        layout = [base] * rows_count
        for i in range(extra):
            layout[i] += 1
        return [r for r in layout if r > 0]
    return [n]


def build_collage_from_images(images, layout_rows, canvas_width=CANVAS_WIDTH):
    """Builds a seamless masonry collage from a list of PIL Image objects.
    Returns a PIL Image object (not saved to disk).
    """
    idx = 0
    rows_data = []

    for count in layout_rows:
        row_imgs = images[idx:idx + count]
        idx += count

        if not row_imgs:
            continue

        target_h = min(img.height for img in row_imgs)

        resized = []
        total_w = 0
        for img in row_imgs:
            ratio = target_h / img.height
            new_w = int(img.width * ratio)
            new_h = int(target_h)
            r = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            resized.append(r)
            total_w += new_w

        scale = canvas_width / total_w if total_w > 0 else 1
        final_row = []
        row_h = 0
        for img in resized:
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            r = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            final_row.append(r)
            row_h = new_h

        # Aggressive GC: close first-pass resized intermediates immediately
        for r in resized:
            r.close()

        rows_data.append((final_row, row_h))

    total_height = sum(h for _, h in rows_data)
    if total_height == 0:
        return None

    collage = Image.new("RGB", (canvas_width, total_height), (255, 255, 255))

    y = 0
    for row, h in rows_data:
        x = 0
        for img in row:
            collage.paste(img, (x, y))
            x += img.width
        # Aggressive GC: close final row images immediately after pasting
        for img in row:
            img.close()
        y += h

    return collage


def build_single_variant(image_folder, image_files, variant, canvas_width, wm_enabled, wm_text, wm_color):
    """Thread-safe worker that builds one complete layout variant.
    Each thread loads its own images from disk to manage memory independently.
    """
    try:
        # Load images from disk (each thread gets its own copies)
        imgs = [Image.open(os.path.join(image_folder, f)).convert("RGB") for f in image_files]

        # Shuffle for variants 2 and 3 using a thread-local RNG for safety
        if variant in (2, 3):
            rng = random.Random()
            rng.shuffle(imgs)

        layout_rows = generate_layout_structure(len(imgs), variant)
        collage = build_collage_from_images(imgs, layout_rows, canvas_width)

        # Close loaded images to free memory
        for img in imgs:
            img.close()
        del imgs
        gc.collect()

        if collage is None:
            return None

        # Apply watermark if enabled
        if wm_enabled:
            color_info = WATERMARK_COLORS.get(wm_color, WATERMARK_COLORS["black"])
            collage = apply_watermark(collage, wm_text, color_rgba=color_info["rgba"])

        return collage
    except Exception as e:
        print(f"[*] Error building variant {variant}: {e}")
        return None


def compress_collage(image, mb_limit):
    """Compresses a PIL Image to fit within the given MB limit using intelligent
    downscaling and binary-search JPEG quality optimization.

    Args:
        image: PIL Image object to compress.
        mb_limit: Maximum file size in MB (0 = no limit, uses 10MB ceiling).

    Returns:
        io.BytesIO buffer containing the final JPEG bytes, seeked to position 0.
    """
    if mb_limit == 0:
        target_bytes = 10 * 1024 * 1024  # 10MB ceiling for "no limit"
    else:
        target_bytes = mb_limit * 1024 * 1024

    current_image = image.copy()

    # Step A: Downscale loop — shrink dimensions until quality=20 fits the budget
    for _ in range(10):
        buf = io.BytesIO()
        current_image.save(buf, "JPEG", quality=20, optimize=True, subsampling=2)
        if buf.tell() <= target_bytes:
            buf.close()
            break
        buf.close()
        # Scale down by 90%
        new_w = int(current_image.width * 0.9)
        new_h = int(current_image.height * 0.9)
        if new_w < 100 or new_h < 100:
            break  # Safety floor: don't shrink below 100px
        old_image = current_image
        current_image = current_image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        old_image.close()  # Aggressive GC: close pre-resize image immediately

    # Step B: Binary search for maximum JPEG quality that fits the budget
    lo, hi = 20, 95
    best_buf = None

    while lo <= hi:
        mid = (lo + hi) // 2
        buf = io.BytesIO()
        current_image.save(buf, "JPEG", quality=mid, optimize=True, progressive=True, subsampling=2)

        if buf.tell() <= target_bytes:
            if best_buf is not None:
                best_buf.close()  # Close previous best before replacing
            best_buf = buf
            lo = mid + 1
        else:
            buf.close()
            hi = mid - 1

    # Safety fallback: if progressive encoding pushed it over, try without progressive
    if best_buf is None:
        best_buf = io.BytesIO()
        current_image.save(best_buf, "JPEG", quality=20, optimize=True, progressive=False, subsampling=2)

    # Final safety: re-check with progressive=False if still over budget
    if best_buf.tell() > target_bytes:
        fallback_buf = io.BytesIO()
        current_image.save(fallback_buf, "JPEG", quality=20, optimize=True, progressive=False, subsampling=2)
        if fallback_buf.tell() < best_buf.tell():
            best_buf.close()
            best_buf = fallback_buf
        else:
            fallback_buf.close()

    current_image.close()
    gc.collect()
    best_buf.seek(0)
    return best_buf


# =========================================================
# 4. TELEGRAM BOT HANDLERS
# =========================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Nakama"
    
    # Check access status to customize greeting
    allowed, reason = check_user_access(user_id)
    status_text = ""
    if reason == "admin":
        status_text = "⭐ **Status**: Admin Access (Unlimited)"
    elif reason == "premium":
        status_text = "👑 **Status**: Premium User (Unlimited)"
    elif reason == "trial":
        user_data = get_user_data(user_id)
        count = user_data["collage_count"]
        remaining = FREE_TRIAL_COLLAGES - count
        status_text = f"🎁 **Status**: Free Trial ({remaining}/{FREE_TRIAL_COLLAGES} collages remaining)"
    else:
        status_text = "❌ **Status**: Free Trial Used Up. Contact admin [@Ak\\_210606](https://t.me/Ak_210606) for Premium!"

    welcome_text = (
        f"🏴‍☠️ **Welcome aboard, {user_name}!**\n\n"
        "I'm the *Galley-La Bot* — your personal collage builder! 🛠️\n\n"
        "Just send me your screenshots and I'll stitch them into a clean, "
        "professional collage with watermarks, custom layouts, and more.\n\n"
        f"{status_text}\n\n"
        "Ready to set sail? 📸 Send your first photos!\n"
        "📎 _Send images as Files for maximum 4K HD quality._\n\n"
        "💡 _Type /help anytime to see all available commands._"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = (
        "📖 **Command Reference**\n\n"
        "📸 *Send photos* — Upload screenshots to the bot.\n"
        "📎 *Send as File* — Send images as documents for maximum 4K HD quality.\n"
        "⚙️ /generate — Build a collage from your uploaded photos.\n"
        "🗑️ /clear — Discard uploaded photos and start over.\n"
        "🔖 /watermark — Toggle watermark on/off, set custom text & color.\n"
        "⚡ /quality — Choose between high-quality document or fast photo output.\n"
        "📐 /layout — Pick collage layout style (Auto, Grid, Vertical, Horizontal, 3-Variant).\n"
        "📦 /limit — Set collage file size limit (0-10 MB).\n"
        "📊 /mystatus — Check your plan status & remaining free trials.\n\n"
        f"📌 _Photo limit: {MAX_PHOTOS_PER_SESSION} images per session._"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown')

@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    allowed, reason = check_user_access(message.from_user.id)
    if not allowed:
        bot.reply_to(message, "⭐ Your 5 free trial collages are used up! Contact admin [@Ak\\_210606](https://t.me/Ak_210606) to get Premium access.", parse_mode='Markdown')
        return
    
    user_id = str(message.chat.id)
    cancel_inactivity_timer(user_id)
    user_folder = os.path.join(TEMP_DIR, user_id)
    # CLOUD FIX: Absolute path path guaranteed folder creation
    os.makedirs(user_folder, exist_ok=True)

    # Enforce photo upload limit to prevent RAM exhaustion
    existing_photos = [f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and f != "final_collage.jpg"]
    if len(existing_photos) >= MAX_PHOTOS_PER_SESSION:
        bot.reply_to(message, f"⚠️ Maximum limit of {MAX_PHOTOS_PER_SESSION} photos reached! Use /generate to build your collage or /clear to start over.")
        return

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    file_path = os.path.join(user_folder, f"{message.photo[-1].file_id}.jpg")
    with open(file_path, 'wb') as new_file:
        new_file.write(downloaded_file)

    # Send "Receiving images..." only if one isn't already showing for this user
    # Reserve slot IMMEDIATELY to prevent race condition with parallel handler threads
    if user_id not in user_photo_receiving_msg:
        user_photo_receiving_msg[user_id] = None  # Claim slot before slow network call
        try:
            receiving_msg = bot.send_message(message.chat.id, "📥 _Receiving images..._", parse_mode='Markdown')
            user_photo_receiving_msg[user_id] = receiving_msg
        except Exception:
            user_photo_receiving_msg.pop(user_id, None)

    # Cancel any existing timer for this user (debounce)
    if user_id in user_photo_timers:
        user_photo_timers[user_id].cancel()

    # Start a new 2-second timer; fires only after user stops sending photos
    chat_id = message.chat.id
    def send_batch_reply():
        user_photo_timers.pop(user_id, None)
        # Count actual files in the folder for an accurate total
        total = len([f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and f != "final_collage.jpg"])
        # Delete the "Receiving images..." message
        receiving_msg = user_photo_receiving_msg.pop(user_id, None)
        if receiving_msg is not None:
            try:
                bot.delete_message(receiving_msg.chat.id, receiving_msg.message_id)
            except Exception:
                pass
        if total > 0:
            bot.send_message(chat_id, f"📸 {total} Images received! Send more, or type /generate.")
            start_inactivity_timer(user_id, chat_id)

    timer = Timer(2.0, send_batch_reply)
    user_photo_timers[user_id] = timer
    timer.start()

@bot.message_handler(content_types=['document'])
def handle_document_photos(message):
    """Accept images sent as files/documents for full 4K HD quality."""
    allowed, reason = check_user_access(message.from_user.id)
    if not allowed:
        bot.reply_to(message, "⭐ Your 5 free trial collages are used up! Contact admin [@Ak\\_210606](https://t.me/Ak_210606) to get Premium access.", parse_mode='Markdown')
        return

    doc = message.document
    if not doc or not doc.mime_type or not doc.mime_type.startswith('image/'):
        return  # Silently ignore non-image documents

    user_id = str(message.chat.id)
    cancel_inactivity_timer(user_id)
    user_folder = os.path.join(TEMP_DIR, user_id)
    os.makedirs(user_folder, exist_ok=True)

    # Enforce photo upload limit to prevent RAM exhaustion
    existing_photos = [f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and f != "final_collage.jpg"]
    if len(existing_photos) >= MAX_PHOTOS_PER_SESSION:
        bot.reply_to(message, f"⚠️ Maximum limit of {MAX_PHOTOS_PER_SESSION} photos reached! Use /generate to build your collage or /clear to start over.")
        return

    file_info = bot.get_file(doc.file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    # Determine file extension from the original filename or mime type
    original_name = doc.file_name or ""
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        ext = '.jpg'  # Default fallback

    file_path = os.path.join(user_folder, f"{doc.file_id}{ext}")
    with open(file_path, 'wb') as new_file:
        new_file.write(downloaded_file)

    # Send "Receiving images..." only if one isn't already showing for this user
    # Reserve slot IMMEDIATELY to prevent race condition with parallel handler threads
    if user_id not in user_photo_receiving_msg:
        user_photo_receiving_msg[user_id] = None  # Claim slot before slow network call
        try:
            receiving_msg = bot.send_message(message.chat.id, "📥 _Receiving HD images..._", parse_mode='Markdown')
            user_photo_receiving_msg[user_id] = receiving_msg
        except Exception:
            user_photo_receiving_msg.pop(user_id, None)

    # Cancel any existing timer for this user (debounce)
    if user_id in user_photo_timers:
        user_photo_timers[user_id].cancel()

    # Start a new 2-second timer; fires only after user stops sending
    chat_id = message.chat.id
    def send_batch_reply():
        user_photo_timers.pop(user_id, None)
        # Count actual files in the folder for an accurate total
        total = len([f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and f != "final_collage.jpg"])
        # Delete the "Receiving images..." message
        receiving_msg = user_photo_receiving_msg.pop(user_id, None)
        if receiving_msg is not None:
            try:
                bot.delete_message(receiving_msg.chat.id, receiving_msg.message_id)
            except Exception:
                pass
        if total > 0:
            bot.send_message(chat_id, f"📸 {total} HD Images received! Send more, or type /generate.")
            start_inactivity_timer(user_id, chat_id)

    timer = Timer(2.0, send_batch_reply)
    user_photo_timers[user_id] = timer
    timer.start()

@bot.message_handler(commands=['watermark'])
def toggle_watermark(message):
    """Lets the user turn watermark on or off via inline buttons."""
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    current = user_data["watermark_enabled"]
    current_text = user_data["watermark_text"]
    current_color_key = user_data["watermark_color"]
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
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    current = user_data["quality"]
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
    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)

    # Cancel any active batch timers
    if user_id in user_photo_timers:
        try:
            user_photo_timers[user_id].cancel()
        except: pass
        user_photo_timers.pop(user_id, None)
    
    cancel_inactivity_timer(user_id)
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
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    current = user_data["layout"]
    
    style_names = {
        "auto": "🤖 Auto (Default)",
        "vertical": "↕️ Single Column (Vertical)",
        "horizontal": "↔️ Single Row (Horizontal)",
        "grid": "🔲 Balanced Grid",
        "3variant": "🎲 3-Variant (3 Layouts)"
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
    markup.add(
        types.InlineKeyboardButton("🎲 3-Variant", callback_data="l_3variant")
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
    'l_auto', 'l_grid', 'l_vertical', 'l_horizontal', 'l_3variant'
])
def callback_handler(call):
    user_id = call.from_user.id
    if call.data == "wm_on":
        set_user_data(user_id, {"watermark_enabled": True})
        user_data = get_user_data(user_id)
        current_text = user_data["watermark_text"]
        current_color_key = user_data["watermark_color"]
        color_info = WATERMARK_COLORS.get(current_color_key, WATERMARK_COLORS["black"])
        current_color_name = color_info["name"]
        bot.edit_message_text(
            f"🔖 **Watermark Settings**\n\nCurrent status: ✅ ON\nCurrent text: `{current_text}`\nCurrent color: {current_color_name}\n\n_Watermark will be applied to your collages._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Watermark turned ON ✅")
    elif call.data == "wm_off":
        set_user_data(user_id, {"watermark_enabled": False})
        user_data = get_user_data(user_id)
        current_text = user_data["watermark_text"]
        current_color_key = user_data["watermark_color"]
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
        
        user_data = get_user_data(user_id)
        current_color_key = user_data["watermark_color"]
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
        user_data = get_user_data(user_id)
        current = user_data["watermark_enabled"]
        current_text = user_data["watermark_text"]
        current_color_key = user_data["watermark_color"]
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
        set_user_data(user_id, {"watermark_color": color_key})
        
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
        set_user_data(user_id, {"quality": "document"})
        bot.edit_message_text(
            "⚡ **Image Quality & Format Settings**\n\nCurrent mode: 📄 Document (High Quality)\n\n_Collages will be sent as uncompressed files to keep maximum detail._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Document mode 📄")
    elif call.data == "q_photo":
        set_user_data(user_id, {"quality": "photo"})
        bot.edit_message_text(
            "⚡ **Image Quality & Format Settings**\n\nCurrent mode: 🖼️ Photo (Compressed/Fast)\n\n_Collages will be sent as standard images for quick in-chat previews and easy sharing._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Photo mode 🖼️")
    elif call.data == "l_auto":
        set_user_data(user_id, {"layout": "auto"})
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: 🤖 Auto (Default)\n\n_The bot will dynamically pick the best layout for your screenshots._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Auto layout 🤖")
    elif call.data == "l_grid":
        set_user_data(user_id, {"layout": "grid"})
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: 🔲 Balanced Grid\n\n_Images will be distributed evenly in a grid (e.g. 2x2, 3x3)._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Balanced Grid 🔲")
    elif call.data == "l_vertical":
        set_user_data(user_id, {"layout": "vertical"})
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: ↕️ Single Column (Vertical)\n\n_Every image will be placed in a single row stacked vertically._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Vertical Column ↕️")
    elif call.data == "l_horizontal":
        set_user_data(user_id, {"layout": "horizontal"})
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: ↔️ Single Row (Horizontal)\n\n_All images will be placed next to each other in a single horizontal row._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: Horizontal Row ↔️")
    elif call.data == "l_3variant":
        set_user_data(user_id, {"layout": "3variant"})
        bot.edit_message_text(
            "📐 **Collage Layout Settings**\n\nCurrent style: 🎲 3-Variant (3 Layouts)\n\n_The bot will generate 3 different layout variants of your collage simultaneously._",
            call.message.chat.id, call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "Saved: 3-Variant 🎲")

def receive_custom_watermark_text(message):
    """Captures the user's custom watermark text from the next message."""
    user_id = message.from_user.id
    custom_text = message.text.strip() if message.text else None

    if not custom_text:
        bot.reply_to(message, "❌ Invalid input. Please use /watermark and try again.")
        return

    set_user_data(user_id, {"watermark_text": custom_text, "watermark_enabled": True})
    bot.reply_to(
        message,
        f"✅ Custom watermark set to: `{custom_text}`\n\n_Watermark has been turned ON automatically._",
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['limit'])
def set_limit(message):
    """Lets the user set their collage file size limit in MB."""
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) < 2:
        user_data = get_user_data(user_id)
        current_limit = user_data["limit"]
        limit_display = "No limit (max quality, 10MB ceiling)" if current_limit == 0 else f"{current_limit} MB"
        bot.reply_to(
            message,
            f"📦 **File Size Limit Settings**\n\n"
            f"Current limit: **{limit_display}**\n\n"
            f"Usage: `/limit <0-10>`\n"
            f"• `0` = No limit (max quality, up to 10MB)\n"
            f"• `1-10` = Maximum file size in MB\n\n"
            f"Example: `/limit 5`",
            parse_mode='Markdown'
        )
        return

    try:
        mb_value = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid input. Please enter a number between 0 and 10.")
        return

    if mb_value < 0 or mb_value > 10:
        bot.reply_to(message, "❌ Limit must be between 0 and 10 MB.")
        return

    set_user_data(user_id, {"limit": mb_value})

    if mb_value == 0:
        bot.reply_to(message, "✅ File size limit removed! Collages will be generated at maximum quality (up to 10MB ceiling).", parse_mode='Markdown')
    else:
        bot.reply_to(message, f"✅ File size limit set to **{mb_value} MB**. Collages will be intelligently compressed to fit.", parse_mode='Markdown')

@bot.message_handler(commands=['generate'])
def process_listing(message):
    user_id_int = message.from_user.id
    allowed, reason = check_user_access(user_id_int)
    if not allowed:
        bot.reply_to(message, "⭐ Your 5 free trial collages are used up! Contact admin [@Ak\\_210606](https://t.me/Ak_210606) to get Premium access.", parse_mode='Markdown')
        return

    user_id = str(message.chat.id)
    user_folder = os.path.join(TEMP_DIR, user_id)

    cancel_inactivity_timer(user_id)

    # Double check folder and content existence
    if not os.path.exists(user_folder) or not os.listdir(user_folder):
        bot.reply_to(message, "Send photos first, then /generate.")
        return

    # Count the photos in the user's folder (exclude any previously generated collage)
    image_files = [f for f in os.listdir(user_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and f != "final_collage.jpg"]
    num_images = len(image_files)

    if num_images == 0:
        bot.reply_to(message, "Send photos first, then /generate.")
        return

    # Fetch all user settings from database
    user_data = get_user_data(user_id_int)
    wm_enabled = user_data["watermark_enabled"]
    wm_text = user_data["watermark_text"]
    wm_color = user_data["watermark_color"]
    quality_pref = user_data["quality"]
    layout_pref = user_data["layout"]
    mb_limit = user_data["limit"]
    wm_label = "watermarked " if wm_enabled else ""

    is_3variant = (layout_pref == "3variant")

    if is_3variant:
        caption_text = f"⚙️ Building 3 layout variants from {num_images} images..."
    else:
        caption_text = f"⚙️ Building collage from {num_images} images..."

    loading_gif = "https://www.gifcen.com/wp-content/uploads/2022/11/one-piece-gif-1.gif"

    # Send the loading animation (or fallback to message)
    try:
        m = bot.send_animation(message.chat.id, loading_gif, caption=caption_text)
    except Exception:
        m = bot.send_message(message.chat.id, caption_text)

    # Acquire the global semaphore — only ONE collage builds at a time to cap RAM
    acquired = collage_semaphore.acquire(blocking=False)
    if not acquired:
        bot.send_message(message.chat.id, "⏳ _Another collage is being built. You've been queued, please wait..._", parse_mode='Markdown')
        collage_semaphore.acquire()  # Block until the other build finishes

    try:
        if is_3variant:
            # === 3-VARIANT PARALLEL ENGINE ===
            # Build 3 variants in parallel — each thread loads its own images from disk
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = []
                for variant in (1, 2, 3):
                    future = executor.submit(
                        build_single_variant, user_folder, image_files, variant, CANVAS_WIDTH,
                        wm_enabled, wm_text, wm_color
                    )
                    futures.append(future)

                collages = [f.result() for f in futures]

            # Compress and send each variant
            for i, collage in enumerate(collages, 1):
                if collage is None:
                    bot.send_message(message.chat.id, f"⚠️ Layout {i}/3 failed to generate.")
                    continue

                compressed_buf = compress_collage(collage, mb_limit)
                collage.close()
                del collage
                gc.collect()
                file_name = f"Collage_Layout_{i}.jpg"

                if quality_pref == "photo":
                    bot.send_photo(
                        message.chat.id,
                        compressed_buf,
                        caption=f"🖼️ Layout {i}/3 — {wm_label}collage!",
                        timeout=90
                    )
                else:
                    bot.send_document(
                        message.chat.id,
                        compressed_buf,
                        caption=f"📄 Layout {i}/3 — High-quality {wm_label}collage!",
                        visible_file_name=file_name,
                        timeout=90
                    )
                compressed_buf.close()

        else:
            # === STANDARD SINGLE COLLAGE PATH ===
            collage_path = os.path.join(user_folder, "final_collage.jpg")
            collage_result = create_collage(
                user_folder, collage_path,
                watermark_enabled=wm_enabled,
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

            collage_img, _ = collage_result

            # Intelligent compression to fit within user's MB limit
            compressed_buf = compress_collage(collage_img, mb_limit)
            collage_img.close()
            del collage_img
            gc.collect()

            if quality_pref == "photo":
                bot.send_photo(
                    message.chat.id,
                    compressed_buf,
                    caption=f"Here's your {wm_label}collage!",
                    timeout=90
                )
            else:
                bot.send_document(
                    message.chat.id,
                    compressed_buf,
                    caption=f"Here's your high-quality {wm_label}image!",
                    visible_file_name="Galley_La_Collage.jpg",
                    timeout=90
                )
            compressed_buf.close()

        # Free up loading message
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except: pass

        # Increment user's collage count
        u_id = message.from_user.id
        new_count = user_data["collage_count"] + 1
        set_user_data(u_id, {"collage_count": new_count})

        # Notify user of remaining trials if they are in trial mode
        if u_id not in ADMIN_USERS and not user_data["is_premium"]:
            remaining = max(0, FREE_TRIAL_COLLAGES - new_count)
            bot.send_message(message.chat.id, f"🎁 Trial Update: You have {remaining}/{FREE_TRIAL_COLLAGES} free trial collages remaining.")

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
    finally:
        # ALWAYS release the semaphore so the next user can build
        collage_semaphore.release()
        gc.collect()

    # CLOUD FIX: Safe Cleanup prevents random Windows permissions/ghost errors from locking folders
    try:
        if os.path.exists(user_folder):
            safe_delete_folder(user_folder)
    except Exception as e:
        # Prints to console, does not notify user
        print(f"[*] Cleanup warning for {user_id}: {e}")

    bot.send_message(message.chat.id, "✅ Session cleared.")

@bot.message_handler(commands=['mystatus'])
def my_status(message):
    """Shows user their current plan status and remaining collages."""
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Nakama"
    
    allowed, reason = check_user_access(user_id)
    user_data = get_user_data(user_id)
    
    if reason == "admin":
        msg = f"⭐ **Status for {user_name}**:\n👑 **Plan**: Admin (Unlimited Access)"
    elif reason == "premium":
        msg = f"👑 **Status for {user_name}**:\n✨ **Plan**: Premium (Unlimited Access)"
    elif reason == "trial":
        count = user_data["collage_count"]
        remaining = FREE_TRIAL_COLLAGES - count
        msg = (
            f"🎁 **Status for {user_name}**:\n"
            f"✨ **Plan**: Free Trial\n"
            f"📊 **Collages created**: {count}/{FREE_TRIAL_COLLAGES}\n"
            f"🔑 **Remaining free trials**: {remaining}"
        )
    else:
        msg = (
            f"❌ **Status for {user_name}**:\n"
            f"✨ **Plan**: Free Trial (Expired)\n"
            f"📊 **Collages created**: {user_data['collage_count']}/{FREE_TRIAL_COLLAGES}\n\n"
            f"⭐ Your trial has finished! Contact admin [@Ak\\_210606](https://t.me/Ak_210606) to get Premium access."
        )
        
    bot.send_message(message.chat.id, msg, parse_mode='Markdown')

@bot.message_handler(commands=['premium', 'approve'])
def manage_premium(message):
    """Admin-only command to grant/revoke premium access."""
    if message.from_user.id not in ADMIN_USERS:
        return  # Silently ignore non-admin commands
        
    args = message.text.split()
    cmd = args[0].lower().replace('/', '')
    
    # Check if revoke is requested
    revoke = False
    target_id_str = ""
    
    if len(args) >= 2:
        if args[1].lower() == "revoke":
            if len(args) < 3:
                bot.reply_to(message, f"❌ Please specify a user ID: `/{cmd} revoke <user_id>`", parse_mode='Markdown')
                return
            revoke = True
            target_id_str = args[2]
        else:
            target_id_str = args[1]
    else:
        bot.reply_to(
            message, 
            f"🔧 **Premium Management Console**\n\n"
            f"Usage:\n"
            f"• `/{cmd} <user_id>` — Grant premium\n"
            f"• `/{cmd} revoke <user_id>` — Revoke premium",
            parse_mode='Markdown'
        )
        return

    try:
        target_id = int(target_id_str)
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID. It must be a number.")
        return

    if revoke:
        set_user_data(target_id, {"is_premium": False})
        bot.reply_to(message, f"❌ Revoked premium status for user ID `{target_id}`.", parse_mode='Markdown')
        try:
            bot.send_message(target_id, "ℹ️ Your premium status has been revoked.")
        except Exception:
            pass
    else:
        set_user_data(target_id, {"is_premium": True})
        bot.reply_to(message, f"👑 Granted premium status to user ID `{target_id}`.", parse_mode='Markdown')
        try:
            bot.send_message(target_id, "🎉 **Congratulations!** You have been granted **Premium Unlimited** access! Enjoy building collages!")
        except Exception:
            pass

# =========================================================
# 5. EXECUTION
# =========================================================
if __name__ == "__main__":
    import time
    
    # Ensure root temp directory exists on start
    if not os.path.exists(TEMP_DIR): 
        os.makedirs(TEMP_DIR)
        
    # Load persisted user settings from previous sessions
    load_user_settings()
    migrate_local_to_mongodb()
    
    print("[*] Galley-La Bot is securely running... Press Ctrl+C to stop.")
    
    # ADVANCED FIX: Sever any ghost connections from previous deployments
    bot.remove_webhook()
    
    # Infinite loop to handle hard network crashes automatically
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=5)
        except Exception as e:
            print(f"[!] Network error occurred: {e}")
            print("[*] Reconnecting in 5 seconds...")
            time.sleep(5)
