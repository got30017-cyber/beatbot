import re
import telebot
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

from config import (
    TOKEN, ADMIN_ID,
    ROOMS, ROOM_LABELS,
    DAILY_LIMIT_FREE, DAILY_LIMIT_PRO,
    BATTLE_HOURS, FINAL_HOURS,
    MESSAGE_DAILY_LIMIT, MESSAGE_COOLDOWN_MINUTES,
    FEEDBACK_CATEGORIES, RATING_POINTS, RATING_LABELS, RATING_EMOJI,
    REFERRAL_RATING_BONUS,
    _settings,
    get_battle_hours, get_final_hours, get_final_threshold,
)
from storage import (
    load_users, save_users,
    load_battles,
    load_finals,
    default_user, get_badge, get_room_wins,
    get_menu,
    load_settings, save_settings,
    init_db, migrate_from_json,
    _category_mode_summary,
    list_user_beats,
    get_current_week,
    set_referred_by,
    get_open_registration_slot, get_running_slot,
    ensure_registration_slot, register_beat_to_slot, find_active_beat_by_user,
    reset_test_data, wipe_all_data,
)
import battles
import weeks

# ─── База данных ──────────────────────────────
init_db()
migrate_from_json()

# ─── Создание экземпляров ────────────────────
bot          = telebot.TeleBot(TOKEN)
BOT_USERNAME = bot.get_me().username  # кешируем — не дёргать API на каждый /invite
scheduler    = BackgroundScheduler()
scheduler.start()

battles.init(bot, scheduler)
weeks.init(bot, scheduler)

battles.register_handlers(bot)
weeks.register_handlers(bot)

# ─── Сессии ──────────────────────────────────
msg_pending    = {}   # user_id -> target_user_id
last_message_to = {}  # (sender_uid, target_uid) -> datetime, не персистится
_pending_referrer = {}  # user_id -> referrer_id, до завершения регистрации
                        # (в момент /start строки пользователя в БД ещё нет —
                        # set_referred_by на несуществующий id был бы no-op)

_NICKNAME_RE = re.compile(r'^[\w ]+$', re.UNICODE)


# ─── Онбординг и регистрация ─────────────────

_ONBOARD = [
    (
        "🎵 Добро пожаловать в Auren!\n"
        "Место, где битмейкеры слушают музыку друг друга.\n"
        "Загружай свои биты, оценивай чужие и узнавай, как сообщество воспринимает твоё звучание."
    ),
    (
        "⚔️ Как это работает?\n\n"
        "1. Загружаешь бит — бот находит соперника\n"
        "2. Слушатели слушают оба бита и голосуют за сильнейший, а потом угадывают, что выберет большинство\n"
        "3. Победа даёт +10 к рейтингу\n\n"
        "Каждый бит может сыграть до 3 батлов — это его карьера. "
        "Сильные карьеры доходят до Бита недели — главного события каждой субботы."
    ),
    (
        "🎧 Как получить фидбек на свой бит?\n\n"
        "Оцени несколько чужих пар — и твой бит выходит в бой. "
        "Это честный обмен: ты слушаешь → тебя слушают.\n\n"
        "Дальше всё просто:\n"
        "• Побеждай в батлах → рейтинг растёт\n"
        "• Доберись до Бита недели → шанс на титул 👑\n\n"
        "Погнали — создай профиль!"
    ),
]


@bot.message_handler(commands=["start"])
def cmd_start(message):
    users   = load_users()
    user_id = str(message.from_user.id)

    # Deep link: t.me/<bot>?start=ref_<referrer_id> — Telegram передаёт это как
    # "/start ref_<id>" одной строкой. Строки пользователя в БД ещё нет (она
    # появится только после save_nickname), поэтому реферера пока просто
    # запоминаем в памяти и применяем через set_referred_by уже после
    # регистрации. Не перезаписываем при повторном /start зарегистрированного.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_") and user_id not in users:
        referrer_id = parts[1][len("ref_"):]
        if referrer_id != user_id and referrer_id in users:
            _pending_referrer[user_id] = referrer_id

    if user_id in users and users[user_id].get("role"):
        bot.send_message(
            message.chat.id,
            f"👋 С возвращением, {users[user_id]['nickname']}!",
            reply_markup=get_menu(user_id),
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Далее ▶️", callback_data="onboard_1"))
    bot.send_message(message.chat.id, _ONBOARD[0], reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("onboard_"))
def handle_onboard(call):
    step = call.data.split("_")[1]
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.answer_callback_query(call.id)

    if step == "1":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("Далее ▶️", callback_data="onboard_2"))
        bot.send_message(call.message.chat.id, _ONBOARD[1], reply_markup=markup)
    elif step == "2":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("Зарегистрироваться 🎵", callback_data="onboard_3"))
        bot.send_message(call.message.chat.id, _ONBOARD[2], reply_markup=markup)
    elif step == "3":
        bot.send_message(call.message.chat.id, "👤 Введи свой никнейм:")
        bot.register_next_step_handler(call.message, save_nickname)


def save_nickname(message):
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        bot.register_next_step_handler(message, save_nickname)
        return

    nickname = message.text.strip()

    if not (2 <= len(nickname) <= 20):
        bot.send_message(message.chat.id, "❌ Никнейм должен быть от 2 до 20 символов. Попробуй ещё раз:")
        bot.register_next_step_handler(message, save_nickname)
        return

    if not _NICKNAME_RE.fullmatch(nickname):
        bot.send_message(
            message.chat.id,
            "❌ Никнейм может содержать только буквы, цифры, пробелы и подчёркивания. Попробуй ещё раз:",
        )
        bot.register_next_step_handler(message, save_nickname)
        return

    users   = load_users()
    user_id = str(message.from_user.id)

    if any(uid != user_id and u.get("nickname", "").lower() == nickname.lower() for uid, u in users.items()):
        bot.send_message(message.chat.id, "❌ Этот никнейм уже занят. Попробуй другой:")
        bot.register_next_step_handler(message, save_nickname)
        return

    users[user_id] = default_user(nickname)
    users[user_id]["role"] = "beatmaker"   # технический дефолт — роли больше не выбираются
    save_users(users)

    referrer_id = _pending_referrer.pop(user_id, None)
    if referrer_id:
        set_referred_by(user_id, referrer_id)

    bot.send_message(
        message.chat.id,
        f"✅ Готово, {nickname}!\n\n"
        f"Ты в деле. Загружай первый бит или начни с голосования — послушай, что уже крутится в системе.\n\n"
        f"Жми на кнопки внизу 👇",
        reply_markup=get_menu(user_id),
    )


# ─── Профиль ──────────────────────────────────

_POINTS_TO_LABEL = {v: k for k, v in RATING_POINTS.items()}


def _profile_average_ratings(user_id: str, battles_data: dict):
    """Средние оценки по категориям для битов юзера — None, если оцененных битов < 3."""
    totals = {cat_key: 0 for cat_key, _ in FEEDBACK_CATEGORIES}
    counts = {cat_key: 0 for cat_key, _ in FEEDBACK_CATEGORIES}
    rated_battles = 0

    for b in battles_data.values():
        if b.get("status") != "finished":
            continue
        if b.get("player1") == user_id:
            side = "1"
        elif b.get("player2") == user_id:
            side = "2"
        else:
            continue

        feedback = b.get("feedback") or {}
        entries  = [entry[side] for entry in feedback.values() if side in entry]
        if not entries:
            continue

        rated_battles += 1
        for cat_key, _ in FEEDBACK_CATEGORIES:
            for e in entries:
                r = e.get(cat_key)
                if r:
                    totals[cat_key] += RATING_POINTS.get(r, 0)
                    counts[cat_key] += 1

    if rated_battles < 3:
        return None

    result = {}
    for cat_key, _ in FEEDBACK_CATEGORIES:
        if counts[cat_key] == 0:
            continue
        avg = round(totals[cat_key] / counts[cat_key])
        result[cat_key] = _POINTS_TO_LABEL[max(0, min(2, avg))]
    return result


def _build_own_profile_text(user_id: str, users: dict, battles_data: dict) -> str:
    u       = users[user_id]
    badge   = get_badge(u.get("wins", 0), u.get("final_wins", 0))

    today = datetime.now().date().isoformat()
    if u.get("last_battle_date") != today:
        battles_left = DAILY_LIMIT_PRO if u.get("is_pro") else DAILY_LIMIT_FREE
    else:
        limit        = DAILY_LIMIT_PRO if u.get("is_pro") else DAILY_LIMIT_FREE
        battles_left = max(0, limit - u.get("battles_today", 0))

    header = f"👤 {u['nickname']}"
    if u.get("is_pro"):
        header += " · 💎 Pro"
    lines = [
        header,
        f"{badge}\n",
        f"⭐️ Рейтинг: {u.get('rating', 0)}",
        f"✅ Побед: {u.get('wins', 0)} · 🏆 Побед недели: {u.get('final_wins', 0)}",
        f"⚔️ Батлов сегодня осталось: {battles_left}",
    ]

    ticket_status = battles.ticket_status(user_id)
    if ticket_status["active"]:
        if ticket_status["paid"]:
            lines.append("✅ Билет оплачен")
        else:
            lines.append(f"🎧 Билет: {ticket_status['progress']}/{ticket_status['required']} пар оценено")

    if u.get("referral_count", 0) > 0:
        lines.append(f"🤝 Приглашено друзей: {u.get('referral_count', 0)}")

    user_beats = list_user_beats(user_id)[:5]
    if user_beats:
        lines.append("")
        lines.append("🎧 Мои биты:")
        status_labels = {
            "queued":            "в очереди 🎯",
            "battling":          "идёт батл ⚔️",
            "awaiting_decision": "ждёт твоего решения ⏸",
        }
        for beat in user_beats:
            short_id = "#" + beat["id"].split("_")[1]
            if beat["status"] == "career_finished":
                beat_parts = []
                if beat["wins"]:
                    beat_parts.append(f"{beat['wins']} побед{'а' if beat['wins'] == 1 else ('ы' if 2 <= beat['wins'] <= 4 else '')}")
                if beat["losses"]:
                    beat_parts.append(f"{beat['losses']} поражени{'е' if beat['losses'] == 1 else 'й'}")
                if beat["draws"]:
                    beat_parts.append(f"{beat['draws']} ничь{'я' if beat['draws'] == 1 else 'их'}")
                stats_str = ", ".join(beat_parts) if beat_parts else "без побед и поражений"
                lines.append(f"• {short_id} — {stats_str}")
            else:
                lines.append(f"• {short_id} — {status_labels.get(beat['status'], beat['status'])}")

    avg_ratings = _profile_average_ratings(user_id, battles_data)
    if avg_ratings:
        parts = []
        for cat_key, cat_label in FEEDBACK_CATEGORIES:
            label = avg_ratings.get(cat_key)
            if label:
                emoji = cat_label.split()[0]
                parts.append(f"{emoji} {RATING_LABELS[label]}")
        if parts:
            lines.append(f"📊 Как тебя оценивают: {' · '.join(parts)}")

    bio = u.get("bio", "").strip()
    if bio:
        lines.append(f"\n📝 {bio}")

    return "\n".join(lines)


@bot.message_handler(commands=["profile"])
@bot.message_handler(func=lambda m: m.text == "📊 Мой профиль")
def my_profile(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    battles_data = load_battles()
    markup       = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✏️ Изменить bio", callback_data="bio_edit"))
    bot.send_message(
        message.chat.id,
        _build_own_profile_text(user_id, users, battles_data),
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "bio_edit")
def handle_bio_edit(call):
    user_id = str(call.from_user.id)
    users   = load_users()
    if user_id not in users:
        bot.answer_callback_query(call.id, "Сначала зарегистрируйся!")
        return
    bot.answer_callback_query(call.id)
    current = users[user_id].get("bio", "").strip()
    hint    = f"Текущее: \"{current}\"\n\n" if current else ""
    bot.send_message(call.message.chat.id, f"✏️ {hint}Введи новый bio (до 150 символов):")
    bot.register_next_step_handler(call.message, _save_bio)


def _save_bio(message):
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    user_id = str(message.from_user.id)
    text    = message.text.strip()
    if len(text) > 150:
        bot.send_message(message.chat.id, f"❌ Слишком длинный текст ({len(text)} символов). Максимум 150.")
        return
    users = load_users()
    if user_id not in users:
        return
    users[user_id]["bio"] = text
    save_users(users)
    bot.send_message(message.chat.id, "✅ Bio обновлён!", reply_markup=get_menu(user_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith("profile_"))
def handle_view_profile(call):
    target_uid = call.data.split("_", 1)[1]
    viewer_uid = str(call.from_user.id)
    users      = load_users()

    u = users.get(target_uid)
    if not u:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return

    badge = get_badge(u.get("wins", 0), u.get("final_wins", 0))

    lines = [f"👤 {u['nickname']}", badge]
    if u.get("is_pro"):
        lines.append("💎 Pro")
    lines += ["", f"⭐️ Рейтинг: {u.get('rating', 0)}", f"✅ Побед: {u.get('wins', 0)}"]
    bio = u.get("bio", "").strip()
    if bio:
        lines.append(f"\n📝 {bio}")

    markup = None
    if viewer_uid != target_uid:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("✉️ Написать", callback_data=f"write_{target_uid}"))

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=markup)


# ─── Рейтинг ──────────────────────────────────

def _build_global_rating_text() -> str:
    """Общий рейтинг (топ-10 по u['rating']) — единственный вид рейтинга,
    который сейчас показывает кнопка «🏆 Рейтинг». Комнатная развилка
    (rating_room_<room>) — легаси мультикомнатной модели: сейчас комната
    одна, выбирать нечего, поэтому кнопка ведёт сюда напрямую. Комнатный
    рейтинг (handle_rating_room, room in ROOMS) остаётся в коде нетронутым
    на будущее — вернётся при возврате мультикомнатности."""
    users  = load_users()
    medals = ["🥇", "🥈", "🥉"]

    entries = [(uid, u, u.get("rating", 0), "очков") for uid, u in users.items()]
    entries.sort(key=lambda x: x[2], reverse=True)

    text = "🏆 Общий рейтинг\n\n"
    for i, (uid, u, sc, suf) in enumerate(entries[:10]):
        medal       = medals[i] if i < 3 else f"{i + 1}."
        badge_emoji = get_badge(u.get("wins", 0), u.get("final_wins", 0)).split()[0]
        text       += f"{medal} {u['nickname']} {badge_emoji} — {sc} {suf}\n"

    if not entries:
        text += "Пока нет данных."
    return text


@bot.message_handler(commands=["rating"])
@bot.message_handler(func=lambda m: m.text == "🏆 Рейтинг")
def show_rating(message):
    bot.send_message(message.chat.id, _build_global_rating_text())


@bot.callback_query_handler(func=lambda call: call.data.startswith("rating_room_"))
def handle_rating_room(call):
    room = call.data[len("rating_room_"):]

    if room == "global":
        text = _build_global_rating_text()
    elif room in ROOMS:
        battles_data = load_battles()
        users        = load_users()
        medals       = ["🥇", "🥈", "🥉"]

        entries = []
        for uid, u in users.items():
            rw = get_room_wins(uid, battles_data)
            entries.append((uid, u, rw[room], "побед"))
        entries.sort(key=lambda x: x[2], reverse=True)
        entries = [(uid, u, sc, suf) for uid, u, sc, suf in entries if sc > 0]

        text = f"🏆 Топ {ROOM_LABELS[room]}\n\n"
        for i, (uid, u, sc, suf) in enumerate(entries[:10]):
            medal       = medals[i] if i < 3 else f"{i + 1}."
            badge_emoji = get_badge(u.get("wins", 0), u.get("final_wins", 0)).split()[0]
            text       += f"{medal} {u['nickname']} {badge_emoji} — {sc} {suf}\n"

        if not entries:
            text += "Пока нет данных для этой комнаты."
    else:
        bot.answer_callback_query(call.id, "Неизвестная комната")
        return

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
    except Exception:
        bot.send_message(call.message.chat.id, text)
    bot.answer_callback_query(call.id)


# ─── Помощь ───────────────────────────────────

@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(
        message.chat.id,
        "ℹ️ Как работает Auren\n\n"
        "🎵 Отправляешь бит — бот ищет соперника и стартует батл. "
        "Бит может сыграть до 3 батлов подряд — это его карьера.\n"
        "🗳 Слушатели оценивают оба бита анонимно, выбирают сильнейший "
        "и угадывают, что выберет большинство.\n"
        "🏆 Каждую субботу — Бит недели: лучшие завершённые карьеры выходят "
        "на 48-часовое голосование всего сообщества.\n"
        "🎧 Чтобы отправить свой бит в батл, сначала оцени несколько чужих пар — это входной билет.\n"
        "⭐️ Рейтинг растёт за победы в батлах и за победу в Бите недели.\n\n"
        "Команды: /start, /profile, /rating, /week, /invite, /help\n\n"
        "Что-то сломалось? Нажми ⚠️ под битом, чтобы пожаловаться, "
        "либо напиши мне напрямую.",
    )


# ─── Реферальная система ──────────────────────

@bot.message_handler(commands=["invite"])
@bot.message_handler(func=lambda m: m.text == "🤝 Пригласить друга")
def cmd_invite(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    link  = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    count = users[user_id].get("referral_count", 0)
    bot.send_message(
        message.chat.id,
        f"🤝 Приглашай друзей!\n\n"
        f"Твоя ссылка:\n{link}\n\n"
        f"За каждого друга, который сыграет свой первый батл, "
        f"ты получаешь +{REFERRAL_RATING_BONUS} к рейтингу и скидку на билет.\n\n"
        f"Приглашено друзей: {count}",
    )


# Старая витрина "🎯 Звёзды" (show_winners) удалена — кнопка меню переиспользована
# под "🏆 Бит недели" (см. weeks.register_handlers), сама витрина больше не нужна.


# ─── Переписка ────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def handle_write(call):
    target_uid = call.data.split("_", 1)[1]
    sender_uid = str(call.from_user.id)
    users      = load_users()

    if target_uid not in users:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return
    if sender_uid == target_uid:
        bot.answer_callback_query(call.id, "Нельзя написать самому себе.")
        return

    target_nick             = users[target_uid]["nickname"]
    msg_pending[sender_uid] = target_uid

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"✉️ Пишешь {target_nick}.\n\nВведи сообщение:")
    bot.register_next_step_handler(call.message, handle_outgoing_message)


def handle_outgoing_message(message):
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return

    sender_uid = str(message.from_user.id)
    target_uid = msg_pending.pop(sender_uid, None)

    if not target_uid:
        bot.send_message(message.chat.id, "Сессия устарела — попробуй снова.")
        return

    text = message.text.strip()
    if not text:
        bot.send_message(message.chat.id, "Пустое сообщение не отправлено.")
        return

    users  = load_users()
    sender = users.get(sender_uid)
    if not sender:
        bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    today = datetime.now().date().isoformat()
    if sender.get("last_message_date") != today:
        sender["messages_sent_today"] = 0
        sender["last_message_date"]   = today

    if sender.get("messages_sent_today", 0) >= MESSAGE_DAILY_LIMIT:
        save_users(users)
        bot.send_message(
            message.chat.id,
            f"⛔️ Лимит сообщений на сегодня исчерпан ({MESSAGE_DAILY_LIMIT}/день). Попробуй завтра.",
        )
        return

    pair_key  = (sender_uid, target_uid)
    last_sent = last_message_to.get(pair_key)
    if last_sent and (datetime.now() - last_sent).total_seconds() < MESSAGE_COOLDOWN_MINUTES * 60:
        bot.send_message(message.chat.id, "⏳ Подожди немного перед следующим сообщением этому человеку.")
        return

    sender_nick = sender.get("nickname", "Неизвестный")

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("↩️ Ответить", callback_data=f"reply_{sender_uid}"),
        telebot.types.InlineKeyboardButton("👤 Профиль",  callback_data=f"profile_{sender_uid}"),
    )
    try:
        bot.send_message(
            target_uid,
            f"📬 Новое сообщение!\n\nОт: {sender_nick}\n\"{text}\"",
            reply_markup=markup,
        )
        sender["messages_sent_today"] = sender.get("messages_sent_today", 0) + 1
        save_users(users)
        last_message_to[pair_key] = datetime.now()
        bot.send_message(message.chat.id, "✅ Сообщение отправлено!", reply_markup=get_menu(sender_uid))
    except Exception:
        bot.send_message(message.chat.id, "⚠️ Не удалось доставить сообщение.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("reply_"))
def handle_reply(call):
    target_uid = call.data.split("_", 1)[1]
    sender_uid = str(call.from_user.id)
    users      = load_users()

    if target_uid not in users:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return

    target_nick             = users[target_uid].get("nickname", "Неизвестный")
    msg_pending[sender_uid] = target_uid

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"↩️ Отвечаешь {target_nick}.\n\nВведи ответ:")
    bot.register_next_step_handler(call.message, handle_outgoing_message)


# ─── Админ-панель ─────────────────────────────

@bot.message_handler(commands=["admin"])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "⛔️ Нет доступа.")
        return

    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("▶️ Запустить слот",         callback_data="admin_start_slot"),
        telebot.types.InlineKeyboardButton("🛑 Завершить слот сейчас",  callback_data="admin_finish_slot"),
        telebot.types.InlineKeyboardButton("🏆 Остановить финал",       callback_data="admin_stop_final"),
        telebot.types.InlineKeyboardButton("👤 Тест-пользователи",      callback_data="admin_test_users"),
        telebot.types.InlineKeyboardButton("🧹 Сбросить тестовые данные", callback_data="admin_reset_test"),
        telebot.types.InlineKeyboardButton("📊 Статистика",             callback_data="admin_stats"),
        telebot.types.InlineKeyboardButton("⏱ Время батлов",            callback_data="admin_set_time"),
        telebot.types.InlineKeyboardButton("🏆 Порог финала",           callback_data="admin_set_threshold"),
        telebot.types.InlineKeyboardButton("🧪 Закрыть неделю",         callback_data="admin_force_close_week"),
        telebot.types.InlineKeyboardButton("🧪 Финиш Бита недели",      callback_data="admin_force_finish_week"),
    )
    bot.send_message(message.chat.id, "👑 Админ-панель\n\nВыбери действие:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def handle_admin_actions(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет доступа.")
        return

    if call.data == "admin_start_slot":
        bot.answer_callback_query(call.id)
        slot = get_open_registration_slot()
        if not slot:
            bot.send_message(
                call.message.chat.id,
                "Нет открытого набора. Слот создаётся автоматически, когда первый бит попадает в набор.",
            )
            return
        try:
            ok, result = battles.start_slot(slot["id"])
            if ok:
                bot.send_message(call.message.chat.id, f"✅ Слот запущен. Создано батлов: {len(result)}")
            else:
                bot.send_message(
                    call.message.chat.id,
                    f"⚠️ Недостаточно битов для старта (нужно ≥ 2). "
                    f"Сейчас в наборе: {len(slot['registered_beats'])}.",
                )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"⚠️ Ошибка при запуске слота:\n{type(e).__name__}: {e}")

    elif call.data == "admin_finish_slot":
        bot.answer_callback_query(call.id)
        slot = get_running_slot()
        if not slot:
            bot.send_message(call.message.chat.id, "Нет активного слота для завершения.")
            return
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("✅ Да, завершить", callback_data="admin_confirm_finish_slot"),
            telebot.types.InlineKeyboardButton("❌ Отмена",        callback_data="admin_cancel"),
        )
        bot.edit_message_text(
            "⚠️ Завершить активный слот досрочно?\n\nПодтверждаешь?",
            call.message.chat.id, call.message.message_id, reply_markup=markup,
        )

    elif call.data == "admin_confirm_finish_slot":
        slot = get_running_slot()
        if not slot:
            bot.edit_message_text("Нет активного слота для завершения.", call.message.chat.id, call.message.message_id)
            return
        bot.edit_message_text("⏳ Завершаю слот...", call.message.chat.id, call.message.message_id)
        # id таймера — тот же, что вешает battles.start_slot (см. S3).
        try:
            scheduler.remove_job(f"slot_{slot['id']}")
        except Exception:
            pass
        try:
            battles.finish_slot(slot["id"])
            bot.send_message(call.message.chat.id, "🛑 Слот завершён досрочно.")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"⚠️ Ошибка при завершении слота:\n{type(e).__name__}: {e}")

    elif call.data == "admin_stop_final":
        finals_data = load_finals()
        active      = {fid: f for fid, f in finals_data.items() if f["status"] == "active"}
        if not active:
            bot.answer_callback_query(call.id, "Нет активных финалов.")
            return
        markup = telebot.types.InlineKeyboardMarkup()
        for fid, f in active.items():
            label = ROOM_LABELS.get(f["room"], f["room"])
            markup.add(telebot.types.InlineKeyboardButton(
                f"🏆 Остановить {label}", callback_data=f"admin_confirm_final_{fid}"
            ))
        markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel"))
        bot.edit_message_text(
            "Выбери финал для остановки:",
            call.message.chat.id, call.message.message_id, reply_markup=markup,
        )

    elif call.data.startswith("admin_confirm_final_"):
        fid = call.data[len("admin_confirm_final_"):]
        bot.edit_message_text("⏳ Останавливаю финал...", call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, _admin_stop_final(fid))

    elif call.data == "admin_set_time":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"⏱ Настройка времени\n\n"
            f"Текущие значения:\n  Батл: {get_battle_hours()} ч\n  Финал: {get_final_hours()} ч\n\n"
            f"Введи длительность батла в часах (сейчас: {get_battle_hours()}):",
        )
        bot.register_next_step_handler(call.message, _admin_time_step1)

    elif call.data == "admin_test_users":
        bot.answer_callback_query(call.id)
        try:
            bot.send_message(call.message.chat.id, _create_test_users())
        except Exception as e:
            bot.send_message(call.message.chat.id, f"⚠️ Ошибка при создании тест-юзеров:\n{type(e).__name__}: {e}")

    elif call.data == "admin_reset_test":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("✅ Да, очистить тест", callback_data="admin_confirm_reset_test"),
            telebot.types.InlineKeyboardButton("❌ Отмена",            callback_data="admin_cancel"),
        )
        bot.edit_message_text(
            "⚠️ Удалить тестовых пользователей (test_*/testq_*) и всё, что с ними связано "
            "(биты, батлы, пустые registration-слоты)?\n\nРеальных пользователей не тронет. Подтверждаешь?",
            call.message.chat.id, call.message.message_id, reply_markup=markup,
        )

    elif call.data == "admin_confirm_reset_test":
        bot.edit_message_text("⏳ Чищу тестовые данные...", call.message.chat.id, call.message.message_id)
        try:
            result = reset_test_data()
            bot.send_message(
                call.message.chat.id,
                f"🧹 Удалено: юзеров {result['users']}, битов {result['beats']}, "
                f"батлов {result['battles']}, слотов {result['slots']}.",
            )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"⚠️ Ошибка при сбросе тестовых данных:\n{type(e).__name__}: {e}")

    elif call.data == "admin_stats":
        bot.answer_callback_query(call.id)
        users = load_users()
        total = len(users)
        with_active_beat = sum(1 for uid in users if find_active_beat_by_user(uid))

        reg_slot        = get_open_registration_slot()
        in_registration = len(reg_slot["registered_beats"]) if reg_slot else 0

        running_slot    = get_running_slot()
        running_battles = len(running_slot["battle_ids"]) if running_slot else 0

        voted_now = sum(1 for u in users.values() if u.get("votes_this_round"))

        bot.send_message(
            call.message.chat.id,
            f"📊 Статистика\n\n"
            f"👥 Всего пользователей: {total}\n"
            f"🎧 С активным битом: {with_active_beat}\n"
            f"🎯 В наборе на слот: {in_registration}\n"
            f"⚔️ В текущем слоте (батлов): {running_battles}\n"
            f"🗳 Проголосовало в этом цикле: {voted_now}",
        )

    elif call.data.startswith("admin_del_beat_"):
        rest        = call.data[len("admin_del_beat_"):]
        side        = int(rest.rsplit("_", 1)[1])
        bid         = rest.rsplit("_", 1)[0]
        winner_side = 2 if side == 1 else 1
        bot.edit_message_text(
            f"🗑 Бит {side} батла #{bid} удалён. Победа присвоена биту {winner_side}.",
            call.message.chat.id, call.message.message_id,
        )
        battles._force_finish_battle(bid, winner_side)

    elif call.data.startswith("admin_ok_report_"):
        rest = call.data[len("admin_ok_report_"):]
        bid  = rest.rsplit("_", 1)[0]
        side = rest.rsplit("_", 1)[1]
        bot.edit_message_text(
            f"✅ Жалоба на батл #{bid} бит {side} отклонена.",
            call.message.chat.id, call.message.message_id,
        )

    elif call.data == "admin_set_threshold":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"🏆 Порог финала\n\nТекущее значение: {get_final_threshold()} побед\n\n"
            f"Введи новое значение (целое число ≥ 1):",
        )
        bot.register_next_step_handler(call.message, _admin_threshold_step)

    elif call.data == "admin_force_close_week":
        bot.answer_callback_query(call.id)
        week = get_current_week()
        if not week or week["status"] != "running":
            bot.send_message(call.message.chat.id, "Нет недели в статусе running для закрытия.")
        else:
            weeks.close_week(week["id"])
            bot.send_message(call.message.chat.id, "✅ close_week вызван.")

    elif call.data == "admin_force_finish_week":
        bot.answer_callback_query(call.id)
        week = get_current_week()
        if not week or week["status"] != "voting":
            bot.send_message(call.message.chat.id, "Нет недели в статусе voting для финиша.")
        else:
            weeks.finish_week_voting(week["id"])
            bot.send_message(call.message.chat.id, "✅ finish_week_voting вызван.")

    elif call.data == "admin_cancel":
        bot.edit_message_text("Отменено.", call.message.chat.id, call.message.message_id)


# ВРЕМЕННО: команда полного сноса БД для чистого старта на пилоте. Не кнопка
# (в отличие от остального админ-инструментария) — намеренно, чтобы её нельзя
# было случайно нажать в панели. Убрать перед публичным запуском — держать в
# проде команду полного сноса БД опасно.
@bot.message_handler(commands=["wipe_all_confirm"])
def cmd_wipe_all(message):
    if message.from_user.id != ADMIN_ID:
        return
    # Снять все слот/батл/недельные таймеры перед сносом, чтобы планировщик
    # не дёргал finish_slot/finish_battle по уже удалённым id.
    scheduler.remove_all_jobs()
    wipe_all_data()
    bot.send_message(
        message.chat.id,
        "💥 Вся БД очищена. Все пользователи, биты, батлы, слоты, недели удалены.\n\n"
        "Перезапусти бота (или он сам создаст новую неделю при следующем действии), "
        "затем заново пройди онбординг через /start.",
    )


# ─── Шаговые хэндлеры админа ─────────────────

def _admin_time_step1(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    try:
        hours = int(message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи целое положительное число.")
        return
    _settings["battle_hours"] = hours
    bot.send_message(
        message.chat.id,
        f"✅ Батл: {hours} ч\n\n"
        f"Введи длительность финала в часах (сейчас: {get_final_hours()}):",
    )
    bot.register_next_step_handler(message, _admin_time_step2)


def _admin_time_step2(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    try:
        hours = int(message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи целое положительное число.")
        return
    _settings["final_hours"] = hours
    save_settings(_settings)
    bot.send_message(
        message.chat.id,
        f"✅ Обновлено. Батл: {get_battle_hours()} ч · Финал: {get_final_hours()} ч\n\n"
        f"Новое время применяется к следующим батлам.",
    )


def _admin_threshold_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    try:
        val = int(message.text.strip())
        if val < 1:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи целое число ≥ 1.")
        return
    _settings["final_threshold"] = val
    save_settings(_settings)
    bot.send_message(message.chat.id, f"✅ Порог финала обновлён: {val} побед.")


# ─── Функции остановки финала ────────────────
# _admin_stop_round() (старая очередь/индивидуальные батлы) упразднена вместе
# с очередью — её роль в слотовой модели играет admin_finish_slot ->
# battles.finish_slot() (S4).

def _admin_stop_final(fid: str) -> str:
    from storage import load_finals, save_finals
    finals_data = load_finals()
    users       = load_users()

    f = finals_data.get(fid)
    if not f or f["status"] != "active":
        return "Финал не найден или уже завершён."

    votes      = f.get("votes", {})
    room_label = ROOM_LABELS.get(f["room"], f["room"])

    f["status"]   = "finished"
    f["end_time"] = datetime.now().isoformat()

    winner_nick = None
    if votes and any(v > 0 for v in votes.values()):
        winner_id    = max(votes, key=lambda uid: votes[uid])
        f["winner_id"] = winner_id

        if winner_id in users:
            users[winner_id]["final_wins"] = users[winner_id].get("final_wins", 0) + 1
            users[winner_id]["rating"]     = users[winner_id].get("rating", 0) + 100

        for beat in f["beats"]:
            uid = beat["user_id"]
            if uid in users and uid != winner_id:
                users[uid]["rating"] = users[uid].get("rating", 0) + 25

        winner_nick = users.get(winner_id, {}).get("nickname", "Неизвестный")

    try:
        scheduler.remove_job(f"final_{fid}")
    except Exception:
        pass

    save_finals(finals_data)
    save_users(users)

    msg = (
        f"🛑 Финал {room_label} остановлен администратором.\n\n"
        + (f"👑 Победитель: {winner_nick} (+100 очков · бейдж 👑 Легенда)" if winner_nick
           else "Голосов не было — победитель не определён.")
    )
    for uid in load_users():
        try:
            bot.send_message(uid, msg)
        except Exception:
            pass

    return f"✅ Финал {room_label} остановлен."


def _create_test_users() -> str:
    """Кладёт тестовые биты в набор registration-слота вместо создания
    батлов напрямую — слотовую модель нельзя обойти стороной, весь цикл
    (набор → старт → голосование → finish_slot) должен проходить через
    настоящий battles.start_slot(), который админ запускает вручную
    кнопкой ▶️ Запустить слот."""
    users = load_users()

    created_users = []
    for i in range(1, 7):
        uid = f"test_{i}"
        if uid not in users:
            u          = default_user(f"TestBeat{i}")
            u["role"]  = "beatmaker"
            users[uid] = u
            created_users.append(f"TestBeat{i}")

    registered_beats = 0
    for i in range(1, 7):
        uid = f"test_{i}"
        if find_active_beat_by_user(uid):
            continue
        beat_id = battles.create_beat(uid, f"TEST_FAKE_FILE_{i}")
        slot_id = ensure_registration_slot()
        register_beat_to_slot(slot_id, beat_id)
        registered_beats += 1

    # Квалификационные биты для Бита недели — отдельные тест-юзеры
    # (testq_1..testq_4), НЕ регистрируются в слот и никогда не участвуют в
    # реальном слотовом флоу: доводятся до career_finished напрямую, минуя
    # обычное завершение батла, чтобы квалификация наполнялась сразу.
    finished_test_beats = 0
    for i, result in zip(range(1, 5), ["win", "loss", "win", "draw"]):
        uid = f"testq_{i}"
        if uid not in users:
            u          = default_user(f"TestQualify{i}")
            u["role"]  = "beatmaker"
            users[uid] = u
        if list_user_beats(uid):
            continue
        beat_id = battles.create_beat(uid, f"TEST_QUALIFY_FILE_{i}")
        battles.record_beat_battle_result(beat_id, result)
        battles.finish_beat_career(beat_id)
        finished_test_beats += 1

    save_users(users)

    slot            = get_open_registration_slot()
    in_registration = len(slot["registered_beats"]) if slot else 0

    lines = []
    if created_users:
        lines.append(f"✅ Тест-пользователей создано: {len(created_users)}")
    else:
        lines.append("ℹ️ Тест-пользователи уже существовали.")
    if registered_beats:
        lines.append(f"✅ Тестовых битов добавлено в набор: {registered_beats}")
    else:
        lines.append("ℹ️ Тестовые биты уже были в наборе (или уже в игре).")
    lines.append(f"🎯 Сейчас в наборе на слот: {in_registration}")
    if finished_test_beats:
        lines.append(f"✅ Тестовых карьер завершено (квалификация для Бита недели): {finished_test_beats}")
    lines.append("\nТеперь нажми ▶️ Запустить слот в админке, чтобы разбить набор на пары.")
    return "\n".join(lines)


# ─── Восстановление таймеров ──────────────────

def restore_timers():
    battles_data = load_battles()
    now          = datetime.now()

    # Легаси: в слотовой модели активные батлы завершаются пачкой через
    # finish_slot (единый таймер на слот, восстанавливается ниже), а не
    # индивидуально. Этот цикл остаётся только на случай "осиротевших"
    # батлов старой (досслотовой) модели, у которых нет своего слота —
    # на чистой БД пилота таких не будет, но на БД разработки они могут
    # ещё висеть. Не убирать — иначе такие батлы никогда не завершатся.
    for bid, b in battles_data.items():
        if b["status"] == "active" and "start_time" in b:
            start    = datetime.fromisoformat(b["start_time"])
            end_time = start + timedelta(hours=get_battle_hours())
            if end_time > now:
                scheduler.add_job(
                    battles.finish_battle,
                    "date",
                    run_date=end_time,
                    args=[bid],
                    id=bid,
                    replace_existing=True,
                )
            else:
                battles.finish_battle(bid)

    current_week = get_current_week()
    if current_week:
        if current_week["status"] == "running":
            closes_at = datetime.fromisoformat(current_week["closes_at"])
            if closes_at > now:
                scheduler.add_job(
                    weeks.close_week, "date",
                    run_date=closes_at,
                    args=[current_week["id"]],
                    id=f"week_close_{current_week['id']}",
                    replace_existing=True,
                )
            else:
                weeks.close_week(current_week["id"])
        elif current_week["status"] == "voting":
            voting_ends = datetime.fromisoformat(current_week["voting_ends_at"])
            if voting_ends > now:
                scheduler.add_job(
                    weeks.finish_week_voting, "date",
                    run_date=voting_ends,
                    args=[current_week["id"]],
                    id=f"week_voting_{current_week['id']}",
                    replace_existing=True,
                )
            else:
                weeks.finish_week_voting(current_week["id"])
    else:
        weeks.ensure_current_week()

    running_slot = get_running_slot()
    if running_slot and running_slot.get("voting_ends_at"):
        voting_ends = datetime.fromisoformat(running_slot["voting_ends_at"])
        if voting_ends > now:
            scheduler.add_job(
                battles.finish_slot, "date",
                run_date=voting_ends,
                args=[running_slot["id"]],
                id=f"slot_{running_slot['id']}",
                replace_existing=True,
            )
        else:
            battles.finish_slot(running_slot["id"])


# ─── Запуск ───────────────────────────────────
_settings.update(load_settings())

# Одноразовое обновление длительности батлов/финалов под маленькую аудиторию.
# Срабатывает один раз — дальше не перетирает значения, заданные через /admin.
if not _settings.get("_short_durations_applied"):
    _settings["battle_hours"]              = BATTLE_HOURS
    _settings["final_hours"]               = FINAL_HOURS
    _settings["_short_durations_applied"]  = True
    save_settings(_settings)

restore_timers()
print("Бот запущен!")
bot.infinity_polling()
