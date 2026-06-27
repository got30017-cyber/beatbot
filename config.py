import os
import sys
import json

# ─── Пути к файлам ───────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
USERS_FILE    = os.path.join(BASE_DIR, "users.json")
BATTLES_FILE  = os.path.join(BASE_DIR, "battles.json")
QUEUE_FILE    = os.path.join(BASE_DIR, "queue.json")
FINALS_FILE   = os.path.join(BASE_DIR, "finals.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

# ─── Telegram credentials ────────────────────
TOKEN    = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TOKEN:
    print("❌ BOT_TOKEN не задан! Добавь переменную окружения.")
    sys.exit(1)
if not ADMIN_ID:
    print("❌ ADMIN_ID не задан! Добавь переменную окружения.")
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID)

# ─── Комнаты ─────────────────────────────────
ROOMS = ["hard", "classic", "melodic", "experimental"]
ROOM_LABELS = {
    "hard":         "🔥 Hard",
    "classic":      "🎚 Classic",
    "melodic":      "🌊 Melodic",
    "experimental": "🧪 Experimental",
}

# ─── Игровые константы ───────────────────────
BEAT_COST_FREE   = 3
BEAT_COST_PRO    = 2
DAILY_LIMIT_FREE = 1
DAILY_LIMIT_PRO  = 3
COINS_MAX        = 10
FINAL_THRESHOLD  = 3    # дефолт: побед для запуска финала
BATTLE_HOURS     = 2    # дефолт: длительность батла (ч)
FINAL_HOURS      = 3    # дефолт: длительность финала (ч)

# ─── Рантайм-настройки (загружаются при старте) ──
_settings: dict = {}


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)


def get_battle_hours() -> int:
    return int(_settings.get("battle_hours", BATTLE_HOURS))


def get_final_hours() -> int:
    return int(_settings.get("final_hours", FINAL_HOURS))


def get_final_threshold() -> int:
    return int(_settings.get("final_threshold", FINAL_THRESHOLD))
