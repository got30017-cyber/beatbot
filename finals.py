import random
import telebot
from datetime import datetime, timedelta

from config import (
    ADMIN_ID, ROOM_LABELS,
    _settings, get_final_hours, get_final_threshold,
)
from storage import (
    load_finals, save_finals,
    load_battles, save_battles,
    load_users, save_users,
    get_menu,
)

_bot: telebot.TeleBot = None
_scheduler = None

VOTE_UNLOCK_DELAY = 20   # сек. — кнопка голосования появляется не раньше
VOTE_GRACE_PERIOD = 5    # сек. — голос засчитывается не раньше, чем через это время после разблокировки

vote_unlocked_at: dict = {}   # user_id -> iso_timestamp, когда юзеру реально пришла кнопка голосования
_final_order: dict = {}       # (user_id, fid) -> рандомизированный для этого юзера порядок битов


def init(bot: telebot.TeleBot, scheduler):
    global _bot, _scheduler
    _bot       = bot
    _scheduler = scheduler


# ─── Подсчёт кандидатов ──────────────────────

def count_final_candidates(room: str, battles: dict) -> int:
    seen = set()
    for b in battles.values():
        if (b.get("status") == "finished"
                and b.get("counted_for_final")
                and not b.get("included_in_final")
                and b.get("room") == room):
            v1, v2 = b.get("votes1", 0), b.get("votes2", 0)
            if v1 > v2:
                seen.add(b["player1"])
            elif v2 > v1:
                seen.add(b["player2"])
    return len(seen)


def _collect_final_candidates(room: str, battles: dict) -> list:
    candidates = []
    seen = set()
    for bid, b in battles.items():
        if (b.get("status") == "finished"
                and b.get("counted_for_final")
                and not b.get("included_in_final")
                and b.get("room") == room):
            v1, v2 = b.get("votes1", 0), b.get("votes2", 0)
            if v1 > v2:
                winner_id, file_id = b["player1"], b.get("beat1_file_id")
            elif v2 > v1:
                winner_id, file_id = b["player2"], b.get("beat2_file_id")
            else:
                continue
            if winner_id not in seen:
                seen.add(winner_id)
                candidates.append({
                    "user_id":      winner_id,
                    "battle_id":    bid,
                    "beat_file_id": file_id,
                })
    return candidates


# ─── Запуск / завершение финала ──────────────

def check_and_start_final(room: str):
    if not room:
        return
    finals  = load_finals()
    battles = load_battles()

    for f in finals.values():
        if f["room"] == room and f["status"] == "active":
            return

    candidates = _collect_final_candidates(room, battles)
    threshold  = get_final_threshold()
    if len(candidates) >= threshold:
        _start_final(room, candidates[:threshold])


def _start_final(room: str, candidates: list):
    battles    = load_battles()
    finals     = load_finals()
    users      = load_users()

    fid        = f"final_{len(finals) + 1}"
    start_time = datetime.now()

    finals[fid] = {
        "room":       room,
        "status":     "active",
        "beats":      candidates,
        "votes":      {c["user_id"]: 0 for c in candidates},
        "voters":     [],
        "start_time": start_time.isoformat(),
        "end_time":   None,
        "winner_id":  None,
    }

    for c in candidates:
        bid = c["battle_id"]
        if bid in battles:
            battles[bid]["included_in_final"] = fid

    save_finals(finals)
    save_battles(battles)

    _scheduler.add_job(
        finish_final,
        "date",
        run_date=start_time + timedelta(hours=get_final_hours()),
        args=[fid],
        id=f"final_{fid}",
        replace_existing=True,
    )

    room_label = ROOM_LABELS.get(room, room)
    for uid in users:
        try:
            _bot.send_message(
                uid,
                f"🏆 Финал {room_label} начался!\n\n"
                f"{len(candidates)} лучших битов ждут твоего голоса.\n"
                f"Голосование открыто {get_final_hours()} часов.\n\n"
                f"Нажми /final чтобы слушать и голосовать!",
            )
        except Exception:
            pass


def finish_final(fid: str):
    finals = load_finals()
    users  = load_users()

    f = finals.get(fid)
    if not f or f["status"] != "active":
        return

    votes      = f.get("votes", {})
    room_label = ROOM_LABELS.get(f["room"], f["room"])

    f["status"]   = "finished"
    f["end_time"] = datetime.now().isoformat()

    if not votes or all(v == 0 for v in votes.values()):
        save_finals(finals)
        for uid in load_users():
            try:
                _bot.send_message(
                    uid,
                    f"🏆 Финал {room_label} завершён.\n\nГолосов не было — победитель не определён.",
                )
            except Exception:
                pass
        return

    winner_id    = max(votes, key=lambda uid: votes[uid])
    f["winner_id"] = winner_id

    if winner_id in users:
        users[winner_id]["final_wins"] = users[winner_id].get("final_wins", 0) + 1
        users[winner_id]["rating"]     = users[winner_id].get("rating", 0) + 100

    for beat in f["beats"]:
        uid = beat["user_id"]
        if uid in users and uid != winner_id:
            users[uid]["rating"] = users[uid].get("rating", 0) + 25

    save_finals(finals)
    save_users(users)

    winner_nick  = users.get(winner_id, {}).get("nickname", "Неизвестный")
    winner_votes = votes[winner_id]

    sorted_v  = sorted(votes.items(), key=lambda x: x[1], reverse=True)
    medals    = ["🥇", "🥈", "🥉"]
    top_lines = []
    for i, (uid, v) in enumerate(sorted_v[:5]):
        nick  = users.get(uid, {}).get("nickname", "—")
        medal = medals[i] if i < 3 else f"{i + 1}."
        top_lines.append(f"{medal} {nick} — {v} голосов")

    msg = (
        f"🏆 Финал {room_label} завершён!\n\n"
        f"👑 Победитель: {winner_nick}\n"
        f"{winner_votes} голосов · +100 очков · бейдж 👑 Легенда\n\n"
        f"Топ 5:\n" + "\n".join(top_lines)
    )
    for uid in load_users():
        try:
            _bot.send_message(uid, msg)
        except Exception:
            pass


# ─── Команда /final ───────────────────────────

def _cmd_final(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    finals  = load_finals()
    battles = load_battles()
    now     = datetime.now()

    active_finals  = {fid: f for fid, f in finals.items() if f["status"] == "active"}
    lines          = ["🏆 Финальный турнир\n"]
    markup_buttons = []

    for room, label in ROOM_LABELS.items():
        room_item = next(
            ((fid, f) for fid, f in active_finals.items() if f["room"] == room),
            None,
        )
        if room_item:
            fid, f     = room_item
            start      = datetime.fromisoformat(f["start_time"])
            hours_left = max(0, int((start + timedelta(hours=get_final_hours()) - now).total_seconds() // 3600))
            already    = user_id in f.get("voters", [])
            status_str = "✅ проголосовал" if already else f"⏳ {hours_left}ч"
            lines.append(f"{label} — 🔥 ФИНАЛ! {status_str}")
            markup_buttons.append((f"{'✅' if already else '🗳'} {label}", f"final_view_{fid}"))
        else:
            count = count_final_candidates(room, battles)
            lines.append(f"{label} — {count}/{get_final_threshold()} к финалу")

    markup = None
    if markup_buttons:
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        for btn_text, cdata in markup_buttons:
            markup.add(telebot.types.InlineKeyboardButton(btn_text, callback_data=cdata))

    _bot.send_message(message.chat.id, "\n".join(lines), reply_markup=markup)


def _handle_final_view(call):
    fid     = call.data[len("final_view_"):]
    user_id = str(call.from_user.id)
    finals  = load_finals()
    users   = load_users()

    f = finals.get(fid)
    if not f or f["status"] != "active":
        _bot.answer_callback_query(call.id, "Этот финал уже завершён.")
        return

    if user_id in f.get("voters", []):
        votes      = f.get("votes", {})
        sorted_v   = sorted(votes.items(), key=lambda x: x[1], reverse=True)
        medals     = ["🥇", "🥈", "🥉"]
        room_label = ROOM_LABELS.get(f["room"], "")
        text       = f"🏆 Финал {room_label} — текущий счёт:\n\n"
        for i, (uid, v) in enumerate(sorted_v[:5]):
            nick  = users.get(uid, {}).get("nickname", "—")
            medal = medals[i] if i < 3 else f"{i + 1}."
            text += f"{medal} {nick} — {v} голосов\n"
        _bot.answer_callback_query(call.id, "Ты уже проголосовал!")
        _bot.send_message(call.message.chat.id, text)
        return

    _bot.answer_callback_query(call.id)
    room_label = ROOM_LABELS.get(f["room"], "")
    _bot.send_message(
        call.message.chat.id,
        f"🏆 Финал {room_label}\n\n"
        f"Послушай все {len(f['beats'])} битов и проголосуй за лучший.\n"
        f"Один голос — одна попытка! Имена скрыты. 👇",
    )

    order_key = (user_id, fid)
    if order_key not in _final_order:
        shuffled = list(f["beats"])
        random.shuffle(shuffled)   # порядок прослушивания рандомный для каждого голосующего
        _final_order[order_key] = shuffled

    _send_final_beat(call.message.chat.id, user_id, fid, 0)


def _send_final_beat(chat_id: int, user_id: str, fid: str, index: int):
    finals = load_finals()
    f      = finals.get(fid)
    if not f:
        return

    beats = _final_order.get((user_id, fid), f["beats"])
    total = len(beats)

    if index >= total:
        _bot.send_message(chat_id, "🎵 Ты прослушал все биты. Выбери лучший и проголосуй!")
        return

    beat_info = beats[index]
    owner_uid = beat_info["user_id"]
    file_id   = beat_info.get("beat_file_id")
    fid_num   = fid.split("_")[1]

    if owner_uid == user_id:
        _bot.send_message(chat_id, f"🎵 Бит {index + 1} из {total} — это твой бит (пропускаем)")
        _send_final_beat(chat_id, user_id, fid, index + 1)
        return

    markup = None
    if index + 1 < total:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(
            f"▶️ Далее ({index + 2}/{total})",
            callback_data=f"fnext_{fid_num}_{index + 1}",
        ))

    room_label = ROOM_LABELS.get(f.get("room", ""), "")
    caption    = (
        f"🎵 Бит {index + 1} из {total}\n🏆 Финал {room_label}\n"
        f"Голосовать за этот бит можно будет через {VOTE_UNLOCK_DELAY} секунд."
    )

    sent = False
    if file_id:
        try:
            _bot.send_audio(chat_id, file_id, caption=caption, reply_markup=markup)
            sent = True
        except Exception:
            pass
    if not sent:
        _bot.send_message(chat_id, caption, reply_markup=markup)

    _scheduler.add_job(
        _unlock_final_vote,
        "date",
        run_date=datetime.now() + timedelta(seconds=VOTE_UNLOCK_DELAY),
        args=[chat_id, fid, owner_uid],
        id=f"funlock_{fid}_{owner_uid}_{chat_id}",
        replace_existing=True,
    )


def _unlock_final_vote(chat_id, fid: str, owner_uid: str):
    finals = load_finals()
    f      = finals.get(fid)
    if not f or f["status"] != "active":
        return

    user_id = str(chat_id)
    if user_id in f.get("voters", []):
        return

    fid_num = fid.split("_")[1]
    markup  = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "🔥 Голосовать за этот бит", callback_data=f"fvote_{fid_num}_{owner_uid}",
    ))
    try:
        _bot.send_message(chat_id, "✅ Теперь можешь голосовать за этот трек 👇", reply_markup=markup)
    except Exception:
        return

    vote_unlocked_at[user_id] = datetime.now().isoformat()


def _handle_final_next(call):
    parts   = call.data.split("_")
    fid_num = parts[1]
    index   = int(parts[2])
    fid     = f"final_{fid_num}"
    user_id = str(call.from_user.id)

    finals = load_finals()
    f      = finals.get(fid)
    if not f or f["status"] != "active":
        _bot.answer_callback_query(call.id, "Этот финал уже завершён.")
        return
    if user_id in f.get("voters", []):
        _bot.answer_callback_query(call.id, "Ты уже проголосовал!")
        return

    _bot.answer_callback_query(call.id)
    _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    _send_final_beat(call.message.chat.id, user_id, fid, index)


def _handle_final_vote(call):
    parts     = call.data.split("_")
    fid_num   = parts[1]
    owner_uid = parts[2]
    fid       = f"final_{fid_num}"
    user_id   = str(call.from_user.id)

    finals = load_finals()
    users  = load_users()

    f = finals.get(fid)
    if not f or f["status"] != "active":
        _bot.answer_callback_query(call.id, "Этот финал уже завершён.")
        return
    if user_id in f.get("voters", []):
        _bot.answer_callback_query(call.id, "Ты уже проголосовал в этом финале!")
        return
    if owner_uid == user_id:
        _bot.answer_callback_query(call.id, "Нельзя голосовать за свой бит!")
        return

    beat_users = [b["user_id"] for b in f["beats"]]
    if owner_uid not in beat_users:
        _bot.answer_callback_query(call.id, "Участник не найден в финале.")
        return

    unlocked_at_str = vote_unlocked_at.get(user_id)
    if unlocked_at_str:
        unlocked_at = datetime.fromisoformat(unlocked_at_str)
        if (datetime.now() - unlocked_at).total_seconds() < VOTE_GRACE_PERIOD:
            _bot.answer_callback_query(call.id, "Подожди немного — дай биту доиграть 🎧")
            return
    # если unlocked_at отсутствует (например, бот перезапустился) — не блокируем голос

    if "votes" not in f:
        f["votes"] = {}
    f["votes"][owner_uid] = f["votes"].get(owner_uid, 0) + 1

    if "voters" not in f:
        f["voters"] = []
    f["voters"].append(user_id)

    save_finals(finals)
    vote_unlocked_at.pop(user_id, None)

    _bot.answer_callback_query(call.id, "✅ Голос засчитан!")
    _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    owner_nick = users.get(owner_uid, {}).get("nickname", "Неизвестный")
    _bot.send_message(
        call.message.chat.id,
        f"✅ Ты проголосовал за: {owner_nick}\n\n"
        f"Результаты узнаешь когда финал завершится.",
        reply_markup=get_menu(user_id),
    )


# ─── Регистрация хэндлеров ───────────────────

def register_handlers(bot: telebot.TeleBot):
    bot.message_handler(commands=["final"])(_cmd_final)
    bot.callback_query_handler(func=lambda c: c.data.startswith("final_view_"))(_handle_final_view)
    bot.callback_query_handler(func=lambda c: c.data.startswith("fnext_"))(_handle_final_next)
    bot.callback_query_handler(func=lambda c: c.data.startswith("fvote_"))(_handle_final_vote)
