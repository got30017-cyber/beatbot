import os
import sys

# ─── Пути к файлам ───────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE  = os.path.join(BASE_DIR, "beat_battle.db")

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


def get_battle_hours() -> int:
    return int(_settings.get("battle_hours", BATTLE_HOURS))


def get_final_hours() -> int:
    return int(_settings.get("final_hours", FINAL_HOURS))


def get_final_threshold() -> int:
    return int(_settings.get("final_threshold", FINAL_THRESHOLD))
