# 🏴‍☠️ Galley-La Collage Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-pyTelegramBotAPI-2CA5E0?logo=telegram)](https://github.com/eternnoir/pyTelegramBotAPI)
[![Pillow](https://img.shields.io/badge/Pillow-Image%20Processing-yellowgreen)](https://python-pillow.org/)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas%20Ready-green?logo=mongodb)](https://www.mongodb.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Galley-La Bot** is a high-performance, cloud-resilient Telegram bot designed to build seamless, high-resolution masonry collages from screenshots and photos. Built especially for digital sellers (e.g. Eldorado, game account marketplaces like Pokémon GO, Clash of Clans, Free Fire), content creators, and power users who need professional collage composition, dynamic image ordering, customizable watermarks, and strict memory/file size limits.

---

## ✨ Features

- **🖼️ 4K Ultra HD Seamless Masonry Engine**:
  - Generates high-resolution output canvases up to `4200px` base width.
  - Zero black borders, zero gaps, and dynamic aspect-ratio-preserving row heights.
- **🔢 Custom Image Ordering & Row Splits (`/order`)**:
  - Numbered visual thumbnail contact sheet with red pill badges (`[#1]`, `[#2]`, ..., `[#N]`).
  - Set exact sequences and custom row partitions using slashes (e.g. `/order 4 1 2 3 / 5 6 7` for 4 up and 3 down).
  - Quick action buttons: `🔄 Reverse Order`, `↩️ Reset Order`, and `⚙️ Build Collage`.
  - Works dynamically for **any** number of uploaded photos (2 to 20).
- **📐 Multiple Layout Styles (`/layout`)**:
  - **Auto**: Balanced 2-row grid split.
  - **Balanced Grid**: Square-like grid composition (e.g., 2×2, 3×3, 3×2).
  - **Vertical Column**: Single vertical stack.
  - **Horizontal Row**: Single wide panorama strip.
  - **3-Variant**: Generates 3 layout variants simultaneously in parallel.
- **🔖 Rotated Diagonal Watermark Protection (`/watermark`)**:
  - Dynamically scaled $45^\circ$ rotated text watermark.
  - Mathematical square canvas calculations prevent edge-clipping.
  - 4 high-contrast translucent colors: ⚫ Black, ⚪ White, 🔴 Red, and 🟡 Yellow.
  - Custom brand/store text support.
- **⚡ Quality & Compression Engine (`/quality` & `/limit`)**:
  - Toggle between **Document** (uncompressed file delivery) and **Photo** (quick in-chat preview).
  - Binary-search JPEG compression engine targeting custom file sizes from `1MB` to `10MB` (or `0` for uncompressed).
- **🛡️ Cloud Stability & Resource Protection**:
  - Concurrency lock (`Semaphore(1)`) caps peak RAM usage to ~700MB, preventing OOM crashes on 1GB/2GB VPS dynos.
  - 60-second inactivity session timer automatically clears temporary user folders.
  - Windows-safe robust folder cleaner (`safe_delete_folder`) resolves file lock issues.
- **🗄️ Hybrid Database Architecture**:
  - Cloud persistence with **MongoDB Atlas**.
  - Automatic fallback to local `user_settings.json` if MongoDB is unavailable.
  - Auto-migration of local settings to MongoDB upon first connection.
- **👑 Tiered Access & Trial Management**:
  - Free trial system (configurable, default: 5 collages).
  - Admin and Premium unlimited access tiers.
  - Command console for granting/revoking user access.

---

## 📖 Command Reference

| Command | Description |
| :--- | :--- |
| `📸 Send Photos` | Upload screenshots directly as standard photos. |
| `📎 Send as File` | Send photos as uncompressed documents for 4K HD output. |
| `/generate` | Render and send the finished collage. |
| `/order [sequence]` | Open visual preview sheet or specify custom order & row layout (e.g. `/order 4 1 2 3 / 5 6 7`). |
| `/layout` | Select collage layout format (Auto, Grid, Vertical, Horizontal, 3-Variant). |
| `/watermark` | Configure watermark toggle, custom text, and color palette. |
| `/quality` | Switch between Document (uncompressed) or Photo (compressed) format. |
| `/limit <0-10>` | Set target collage file size limit in MB (`0` = max quality up to 10MB ceiling). |
| `/clear` | Discard current session photos and reset upload cache. |
| `/mystatus` | View current plan, collages created, and remaining trials. |
| `/help` | Display command reference and usage guide. |
| `/premium <id> [revoke]` | *(Admin only)* Grant or revoke unlimited premium access for a user. |

---

## 🔢 How Image Ordering & Row Splits Work

### 1. Visual Preview Sheet
When you upload images and type `/order`:
1. The bot generates an in-memory contact sheet where every uploaded image has a clear red badge (`#1`, `#2`, `#3`, etc.).
2. You can inspect the image IDs and decide which images go into which row.

```
+----------------+----------------+----------------+
| [#1] Image 1   | [#2] Image 2   | [#3] Image 3   |
+----------------+----------------+----------------+
| [#4] Image 4   | [#5] Image 5   | [#6] Image 6   |
+----------------+----------------+----------------+
```

### 2. Custom Row Partition (Slash Syntax)
Use `/` to divide your collage into rows:

- **7 Images (4 up, 3 down):**
  ```text
  /order 4 1 2 3 / 5 6 7
  ```
  - **Row 1 (Top)**: Images #4, #1, #2, #3
  - **Row 2 (Bottom)**: Images #5, #6, #7

- **5 Images (3 up, 2 down):**
  ```text
  /order 1 2 3 / 4 5
  ```

- **10 Images (3 rows: 3 / 4 / 3):**
  ```text
  /order 1 2 3 / 4 5 6 7 / 8 9 10
  ```

### 3. Simple Sequence Reorder
To reorder without custom row divisions:
```text
/order 3 1 2 5 4
```
*(Arranges the sequence while keeping your selected `/layout` style intact).*

---

## 🛠️ Project Structure

```text
Project-5/
├── eldorado_bot.py       # Core bot logic, layout engine, Telegram handlers
├── requirements.txt      # Python dependencies
├── Procfile              # Deployment process definition (Heroku / Railway / Render)
├── user_settings.json    # Local JSON persistence fallback
├── temp_user_data/       # Transient session cache (auto-cleaned)
│   └── <user_id>/
│       ├── order_manifest.json  # Session arrival sequence & custom row split
│       └── *.jpg                # Uploaded screenshots
└── .env                  # Environment configuration (ignored in git)
```

---

## 🚀 Installation & Local Setup

### 1. Prerequisites
- Python 3.10 or higher
- Git

### 2. Clone Repository
```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
```

### 3. Set Up Virtual Environment
```bash
# Windows (PowerShell)
python -m venv venv
.\venv\Scripts\Activate.ps1

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

### 5. Configure Environment Variables
Create a `.env` file in the project root:

```ini
# Required: Your Telegram Bot Token from @BotFather
TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"

# Optional: Comma-separated Telegram User IDs with Admin privileges
ADMIN_USERS="5282482434"

# Optional: MongoDB Atlas URI for persistent cloud storage
MONGO_URI="mongodb+srv://<username>:<password>@cluster0.example.mongodb.net/?retryWrites=true&w=majority"
MONGO_DB_NAME="collage_bot"
```

> **Note:** If `MONGO_URI` is omitted or unavailable, the bot automatically falls back to local `user_settings.json` with zero disruption.

### 6. Run the Bot
```bash
python eldorado_bot.py
```

---

## ☁️ Deployment Guide

### Heroku / Render / Railway
This repository includes a `Procfile`:
```text
web: python eldorado_bot.py
```

1. Connect your GitHub repository to your hosting platform.
2. In the platform dashboard, configure the **Environment Variables**:
   - `TELEGRAM_BOT_TOKEN` (Required)
   - `ADMIN_USERS` (Optional)
   - `MONGO_URI` (Recommended for production persistence across restarts)
3. Deploy the service. The bot will automatically sever any stale webhook connections and start long-polling.

---

## ⚙️ Core Engine Configuration

The following constants in `eldorado_bot.py` can be adjusted to tune performance:

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `CANVAS_WIDTH` | `4200` | Output width in pixels for high-resolution collages. |
| `MAX_PHOTOS_PER_SESSION` | `20` | Max images allowed per user session to prevent RAM exhaustion. |
| `FREE_TRIAL_COLLAGES` | `5` | Number of free collages granted before requiring Premium. |
| `collage_semaphore` | `Semaphore(1)` | Restricts concurrent collage renders to prevent CPU/RAM spikes. |
| Inactivity Timer | `60s` | Auto-deletes user cache after 60 seconds of inactivity. |

---

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/Akshat-Vasava/POGO_Project/issues).

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
