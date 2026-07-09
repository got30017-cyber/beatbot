import json
import random
import telebot
from datetime import datetime, timedelta

from config import (
    ADMIN_ID, ROOMS, ROOM_LABELS,
    TICKET_FIRST, TICKET_CONTINUE, MAX_CAREER_BATTLES,
    NOTIFY_THROTTLE_HOURS, SLOT_MIN_PARTICIPANTS,
    FEEDBACK_CATEGORIES, RATING_POINTS, RATING_LABELS, RATING_EMOJI,
    REFERRAL_RATING_BONUS, REFERRAL_MAX_REWARDS,
    _settings, get_battle_hours, get_slot_voting_hours,
)
from storage import (
    load_users, save_users,
    load_battles, save_battles,
    _empty_queue,
    check_daily_limit, get_menu,
    _category_mode_summary,
    add_pair_rating, resolve_pair_ratings,
    get_user_intuition_stats, get_all_intuition_accuracy,
    create_beat, get_beat, update_beat_status, update_beat_file,
    record_beat_battle_result, add_predicted_for, finish_beat_career,
    list_user_beats, find_active_beat_by_user,
    add_referral_reward, mark_referral_rewarded, pop_ticket_discount,
    ensure_registration_slot, get_registration_beats, beat_in_registration,
    user_in_registration, remove_beat_from_registration, register_beat_to_slot,
    get_slot, update_slot, set_beat_priority,
    get_running_slot,
)

_bot: telebot.TeleBot = None
_scheduler = None

# Сессии голосования (видны из bot.py для сброса в admin_stop_round)
vote_context: dict = {}         # user_id -> "beat" | "free" | "room_<uid>"
vote_session: dict = {}         # user_id -> {"required": N, "battles": [...]}

_first_side_shown: dict = {}    # user_id -> "1"|"2", какой бит показан первым (текущий батл)
pending_feedback: dict = {}     # user_id -> {bid, chat_id, message_id, first_side, current_side, ratings}

# Данные нового флоу голосования
pair_shown_at: dict = {}        # user_id -> iso_timestamp показа пары (для промпта 2)
voted_at: dict = {}             # user_id -> iso_timestamp момента голоса (для промпта 2)
pending_prediction: dict = {}   # user_id -> {bid, vote_side, chat_id}


def init(bot: telebot.TeleBot, scheduler):
    global _bot, _scheduler
    _bot       = bot
    _scheduler = scheduler


# ─── Входной билет ────────────────────────────
# Билет — состояние в самом пользователе (ticket_progress/ticket_required),
# отдельной таблицы не заводим. Тонкие обёртки над load_users/save_users.

def ticket_status(user_id: str) -> dict:
    users    = load_users()
    u        = users.get(user_id, {})
    required = u.get("ticket_required", 0)
    progress = u.get("ticket_progress", 0)
    active   = required > 0
    paid     = active and progress >= required
    return {"active": active, "progress": progress, "required": required, "paid": paid}


def start_ticket(user_id: str, required: int) -> int:
    """Активирует билет с нужным числом пар. Обнуляет progress.

    Если у пользователя накоплена реферальная скидка — тратит одну "скидочную
    пару" и уменьшает required (не ниже 1). Не тратит скидку, если required
    и так уже 1 — иначе она сгорела бы без всякого эффекта (актуально для
    TICKET_CONTINUE, который и так равен 1).

    pop_ticket_discount коммитит декремент отдельной транзакцией ДО того, как
    здесь загружается users — иначе итоговый save_users() затёр бы его
    устаревшим снимком (та же гонка, что была с consume_ticket ранее).

    Возвращает 1, если скидка была применена — вызывающий код показывает
    об этом сообщение пользователю.
    """
    discount_applied = 0
    if required > 1:
        discount_applied = pop_ticket_discount(user_id)
        if discount_applied:
            required = max(1, required - discount_applied)

    users = load_users()
    if user_id not in users:
        return 0
    users[user_id]["ticket_required"] = required
    users[user_id]["ticket_progress"] = 0
    save_users(users)
    return discount_applied


def increment_ticket(user_id: str):
    """Вызывается после каждого подтверждённого прогноза.

    Инкрементит progress, если билет активен и не оплачен.
    """
    users = load_users()
    u     = users.get(user_id)
    if not u:
        return
    required = u.get("ticket_required", 0)
    progress = u.get("ticket_progress", 0)
    if required > 0 and progress < required:
        u["ticket_progress"] = progress + 1
        save_users(users)


def consume_ticket(user_id: str):
    """Сбрасывает билет после успешной отправки бита в очередь."""
    users = load_users()
    if user_id not in users:
        return
    users[user_id]["ticket_required"] = 0
    users[user_id]["ticket_progress"] = 0
    save_users(users)


# ─── Вспомогательные функции голосования ─────

def get_eligible_battles(user_id: str) -> dict:
    battles  = load_battles()
    is_admin = user_id == str(ADMIN_ID)
    return {
        bid: b for bid, b in battles.items()
        if b["status"] == "active"
        and (is_admin or user_id not in [b["player1"], b["player2"]])
    }


def has_any_votable_battle(user_id: str) -> bool:
    """True, если для этого пользователя прямо сейчас есть хотя бы один активный
    батл, доступный для голосования (не свой, ещё не оценённый).

    Bootstrap-проверка для _send_beat: та же фильтрация, что в
    get_eligible_battles/votes_needed, но без сборки полного списка — нужен
    только факт "есть хотя бы один", коротко замыкаем на первом совпадении.
    Чистая проверка текущего состояния, не флаг — пересчитывается каждый раз.
    """
    battles_data = load_battles()
    is_admin     = user_id == str(ADMIN_ID)
    voted        = load_users().get(user_id, {}).get("votes_this_round", [])
    for bid, b in battles_data.items():
        if b.get("status") != "active":
            continue
        if not is_admin and user_id in (b.get("player1"), b.get("player2")):
            continue
        if bid in voted:
            continue
        return True
    return False


def system_has_active_battles() -> bool:
    """True, если в системе есть хотя бы один активный батл — независимо от
    того, голосовал ли конкретный пользователь. Используется для бутстрапа
    билета: бесплатный проход бита даётся ТОЛЬКО когда активных батлов нет
    вообще (система реально пуста, оценивать физически нечего). Отличие от
    has_any_votable_battle: та смотрит 'есть ли для МЕНЯ неоценённый батл',
    что ошибочно срабатывает, когда юзер оценил все батлы."""
    battles_data = load_battles()
    return any(b.get("status") == "active" for b in battles_data.values())


def votes_needed(user_id: str):
    eligible = get_eligible_battles(user_id)
    users    = load_users()
    voted    = users.get(user_id, {}).get("votes_this_round", [])

    if user_id in vote_session:
        session_battles = vote_session[user_id]["battles"]
        required        = vote_session[user_id]["required"]
        not_voted     = [bid for bid in session_battles if bid in eligible and bid not in voted]
        already_voted = [bid for bid in session_battles if bid in eligible and bid in voted]
        return required, len(already_voted), not_voted

    required  = min(len(eligible), 5)
    not_voted = [bid for bid in eligible if bid not in voted]
    already   = [bid for bid in eligible if bid in voted]
    return required, len(already), not_voted


def start_vote_session(user_id: str):
    eligible  = get_eligible_battles(user_id)
    users     = load_users()
    voted     = users.get(user_id, {}).get("votes_this_round", [])
    not_voted = [bid for bid in eligible if bid not in voted]
    required  = min(len(eligible), 5)
    vote_session[user_id] = {"required": required, "battles": not_voted[:required]}
    return required, 0, not_voted[:required]


def clear_vote_session(user_id: str):
    vote_session.pop(user_id, None)


def find_user_battle(user_id: str):
    battles  = load_battles()
    for bid, b in battles.items():
        if b["status"] == "active" and user_id in [b["player1"], b["player2"]]:
            return bid, b
    finished = [
        (bid, b) for bid, b in battles.items()
        if b["status"] == "finished" and user_id in [b["player1"], b["player2"]]
    ]
    return finished[-1] if finished else (None, None)


def _feedback_summary_text(b: dict, side: str):
    summary = _category_mode_summary(b, side)
    if not summary:
        return None
    lines = ["📊 Что говорят слушатели о твоём бите:\n"]
    for cat_key, cat_label in FEEDBACK_CATEGORIES:
        rating = summary.get(cat_key)
        if rating:
            lines.append(f"{cat_label}: {RATING_EMOJI[rating]} {RATING_LABELS[rating]}")
    return "\n".join(lines)


def _feedback_comments_text(b: dict):
    """Последние анонимные комментарии слушателей к паре (без привязки к автору).

    Комментарий пишется один раз за карточку и не различается по стороне —
    поэтому собирается общий список, а не отдельно для каждого бита.
    """
    feedback = b.get("feedback") or {}
    comments = [
        entry["comment"].strip()[:200]
        for entry in feedback.values()
        if entry.get("comment", "").strip()
    ]
    if not comments:
        return None
    lines = ["💬 Комментарии слушателей:"]
    for c in comments[-3:]:
        lines.append(f"— \"{c}\"")
    return "\n".join(lines)


def build_intuition_summary(user_id: str, since_iso: str):
    """Персональная сводка интуиции за период. None, если 0 разрешённых пар.

    Самостоятельная функция, не привязана к финалу: сейчас вызывается из
    завершения финала, при переходе на недельный цикл переедет туда.
    """
    stats = get_user_intuition_stats(user_id, since_iso)
    total = stats["total_resolved"]
    if total == 0:
        return None

    correct  = stats["correct"]
    reward_a = stats["reward_a"]
    reward_b = stats["reward_b"]
    pairs    = stats["pairs_rated"]

    # Хвост-процентиль на первую строку — только при достаточных данных.
    tail = ""
    if total >= 3:
        accuracy = get_all_intuition_accuracy(since_iso)
        others   = [c / t for uid, (c, t) in accuracy.items() if uid != user_id]
        if len(others) >= 3:
            my_acc = correct / total
            below  = sum(1 for a in others if a < my_acc)   # строго ниже
            pct    = round(below / len(others) * 100)
            tail   = f" — чувствуешь сообщество лучше, чем {pct}% участников"

    lines = [
        f"🧠 Твоя интуиция за этот цикл: угадал {correct} из {total}{tail}",
        f"🤝 Совпал с сообществом: {reward_a} раз",
        f"🦅 Белая ворона с чутьём: {reward_b} раз",
        f"❤️ Оценено пар: {pairs}",
    ]
    return "\n".join(lines)


# ─── Карьера бита ─────────────────────────────

def _process_beat_career_step(beat_id: str, result: str, predicted_delta: int):
    """После завершения батла обновляет статистику бита и решает, что показать
    автору: авто-финал карьеры при достижении лимита, либо экран решения."""
    record_beat_battle_result(beat_id, result)
    if predicted_delta:
        add_predicted_for(beat_id, predicted_delta)

    beat = get_beat(beat_id)
    if not beat:
        return
    author_id = beat["author_id"]

    if beat["battles_played"] >= MAX_CAREER_BATTLES:
        finish_beat_career(beat_id)
        try:
            _bot.send_message(
                author_id,
                f"🏁 Карьера завершена — все {MAX_CAREER_BATTLES} батла позади.\n\n"
                f"✅ {beat['wins']} побед · ❌ {beat['losses']} поражений · 🤝 {beat['draws']} ничьих\n"
                f"🧠 {beat['predicted_for']} раз сообщество ставило именно на него\n\n"
                f"Отличная работа! Теперь бит ждёт итогов недели — если он в топе, увидишь его в Бите недели.",
            )
        except Exception:
            pass
        return

    update_beat_status(beat_id, "awaiting_decision")
    try:
        _bot.send_message(
            author_id,
            _career_decision_text(beat),
            reply_markup=_career_decision_markup(beat_id),
        )
    except Exception:
        pass


def _predicted_count(predictions: dict, side: str) -> int:
    return sum(1 for pred_side in predictions.values() if pred_side == side)


def _run_beat_career_steps(b: dict, winning_side: int):
    """winning_side: 1|2|0 (ничья). Обрабатывает оба бита батла, если у них
    есть beat1_id/beat2_id (старые батлы до этой фичи их не имеют — пропускаем)."""
    predictions = b.get("predictions", {})
    beat1_id = b.get("beat1_id")
    beat2_id = b.get("beat2_id")

    if winning_side == 1:
        result1, result2 = "win", "loss"
    elif winning_side == 2:
        result1, result2 = "loss", "win"
    else:
        result1, result2 = "draw", "draw"

    if beat1_id:
        _process_beat_career_step(beat1_id, result1, _predicted_count(predictions, "1"))
    if beat2_id:
        _process_beat_career_step(beat2_id, result2, _predicted_count(predictions, "2"))


# ─── Реферальная награда ──────────────────────

def _is_users_first_battle(player_id: str, battles_data: dict, current_bid: str) -> bool:
    """True, если у player_id нет ДРУГИХ завершённых батлов, кроме текущего.

    Промпт предполагал поле users[player_id]["battles_played"], которого нет
    в схеме пользователя (battles_played есть только у битов, за их
    собственную карьеру — не подходит для "первый батл в жизни игрока",
    поскольку у второго бита счётчик карьеры снова стартует с нуля). Вместо
    нового поля просто сканируем историю батлов — на масштабе пилота дёшево.
    """
    for other_bid, other_b in battles_data.items():
        if other_bid == current_bid:
            continue
        if other_b.get("status") == "finished" and player_id in (other_b.get("player1"), other_b.get("player2")):
            return False
    return True


def _maybe_reward_referral(player_id: str, users: dict, battles_data: dict, bid: str):
    """Начисляет награду рефереру, когда приглашённый друг реально доиграл
    свой ПЕРВЫЙ батл до конца (неважно, победа/поражение/ничья) — это и есть
    защита от накрутки: заманить друга зарегистрироваться дёшево, заставить
    его пройти реальный billet→бит→батл цикл — дорого.
    """
    u = users.get(player_id)
    if not u:
        return
    referrer_id = u.get("referred_by")
    if not referrer_id or u.get("referral_rewarded"):
        return
    if not _is_users_first_battle(player_id, battles_data, bid):
        return

    if referrer_id not in users:
        mark_referral_rewarded(player_id)
        return

    if REFERRAL_MAX_REWARDS is not None and users[referrer_id].get("referral_count", 0) >= REFERRAL_MAX_REWARDS:
        mark_referral_rewarded(player_id)
        return

    add_referral_reward(referrer_id)
    mark_referral_rewarded(player_id)
    try:
        _bot.send_message(
            referrer_id,
            f"🎉 Твой друг {u.get('nickname', 'кто-то')} сыграл первый батл!\n\n"
            f"+{REFERRAL_RATING_BONUS} к рейтингу и скидка на следующий билет (-1 пара).",
        )
    except Exception:
        pass


# ─── Завершение батла ─────────────────────────

def _settle_battle(bid: str):
    """Расчётная часть завершения батла: победитель, начисление рейтинга/
    побед, карьерный шаг бита, реферальная награда. Никаких пушей о
    результате (победа/поражение/ничья автору, прогноз голосовавшим) —
    их собирает вызывающий код (finish_battle или finish_slot) из
    возвращённого словаря, чтобы не считать один батл дважды.

    Исключение: _run_beat_career_steps и _maybe_reward_referral сами шлют
    сообщения (карьерное решение автору, награда рефереру) — это отдельные,
    самодостаточные уведомления, не завязанные на способ доставки основного
    результата (одиночный пуш vs пакетный пуш слота), поэтому остаются здесь
    как в оригинале, а не переезжают к вызывающему коду.

    Возвращает None, если батл уже не active (повторный вызов/гонка).
    """
    battles = load_battles()
    users   = load_users()

    b = battles.get(bid)
    if not b or b["status"] != "active":
        return None

    b["status"]   = "finished"
    b["end_time"] = datetime.now().isoformat()

    votes1  = b.get("votes1", 0)
    votes2  = b.get("votes2", 0)
    p1, p2  = b["player1"], b["player2"]
    p1_nick = users.get(p1, {}).get("nickname", "Игрок 1")
    p2_nick = users.get(p2, {}).get("nickname", "Игрок 2")

    winner_id = loser_id = None
    winner_nick = loser_nick = None
    winner_votes = loser_votes = None
    winner_side = loser_side = None

    if votes1 > votes2:
        winning_side                 = 1
        winner_id, loser_id          = p1, p2
        winner_nick, loser_nick      = p1_nick, p2_nick
        winner_votes, loser_votes    = votes1, votes2
        winner_side, loser_side      = "1", "2"
    elif votes2 > votes1:
        winning_side                 = 2
        winner_id, loser_id          = p2, p1
        winner_nick, loser_nick      = p2_nick, p1_nick
        winner_votes, loser_votes    = votes2, votes1
        winner_side, loser_side      = "2", "1"
    else:
        winning_side = 0

    resolve_pair_ratings(bid, str(winning_side))

    predictions_snapshot = dict(b.get("predictions", {}))

    # +1 рейтинг тем, чей ПРОГНОЗ совпал с победившей стороной.
    # Награда перенесена с голоса на прогноз: награда за «правильный» голос
    # стимулирует голосовать за фаворита, а не честно; прогноз — честное угадывание.
    if winning_side != 0:
        for uid_str, pred_side in b.get("predictions", {}).items():
            if uid_str in users and pred_side == str(winning_side):
                users[uid_str]["rating"] = users[uid_str].get("rating", 0) + 1

        if winner_id in users:
            users[winner_id]["rating"] = users[winner_id].get("rating", 0) + 10
            users[winner_id]["wins"]   = users[winner_id].get("wins", 0) + 1
        b["counted_for_final"] = True

    save_battles(battles)
    save_users(users)

    new_rating = users.get(winner_id, {}).get("rating", 0) if winning_side != 0 else None

    _run_beat_career_steps(b, winning_side)

    for pid in [p1, p2]:
        _maybe_reward_referral(pid, users, battles, bid)

    # финалы упразднены — квалификация теперь по неделям, см. weeks.py

    return {
        "bid": bid,
        "winning_side": winning_side,
        "p1": p1, "p2": p2,
        "p1_nick": p1_nick, "p2_nick": p2_nick,
        "votes1": votes1, "votes2": votes2,
        "predictions": predictions_snapshot,
        "winner_id": winner_id, "loser_id": loser_id,
        "winner_nick": winner_nick, "loser_nick": loser_nick,
        "winner_votes": winner_votes, "loser_votes": loser_votes,
        "winner_side": winner_side, "loser_side": loser_side,
        "new_rating": new_rating,
        "b": b,
    }


def _send_author_battle_result(outcome: dict):
    """Пуш(и) автору(ам) бита о результате батла — победа/поражение/ничья,
    с процентами голосов и фидбек-сводкой. Общий хелпер для finish_battle
    (легаси, один батл за раз) и finish_slot (пачка батлов слота) — тексты
    идентичны тем, что были в finish_battle до выделения _settle_battle."""
    b            = outcome["b"]
    p1, p2       = outcome["p1"], outcome["p2"]
    p1_nick      = outcome["p1_nick"]
    p2_nick      = outcome["p2_nick"]
    votes1       = outcome["votes1"]
    votes2       = outcome["votes2"]
    winning_side = outcome["winning_side"]

    if winning_side == 0:
        for pid in [p1, p2]:
            try:
                _bot.send_message(
                    pid,
                    f"🤝 Батл завершён — ничья!\n\n"
                    f"{p1_nick} — {votes1} голосов\n"
                    f"{p2_nick} — {votes2} голосов\n\n"
                    f"Бывает и так — иногда сообщество раскалывается ровно пополам. "
                    f"Держись, следующий батл может расставить всё по местам.",
                )
            except Exception:
                pass
        return

    winner_id    = outcome["winner_id"]
    loser_id     = outcome["loser_id"]
    winner_nick  = outcome["winner_nick"]
    loser_nick   = outcome["loser_nick"]
    winner_votes = outcome["winner_votes"]
    loser_votes  = outcome["loser_votes"]
    winner_side  = outcome["winner_side"]
    loser_side   = outcome["loser_side"]
    new_rating   = outcome["new_rating"]

    total      = winner_votes + loser_votes
    winner_pct = round(winner_votes / total * 100) if total > 0 else 0
    loser_pct  = 100 - winner_pct

    winner_summary = _feedback_summary_text(b, winner_side)
    loser_summary  = _feedback_summary_text(b, loser_side)
    comments_text  = _feedback_comments_text(b)

    try:
        _bot.send_message(
            winner_id,
            f"🏆 Победа!\n\n"
            f"{winner_nick} — {winner_votes} голосов ({winner_pct}%)\n"
            f"{loser_nick} — {loser_votes} голосов ({loser_pct}%)\n\n"
            f"Сообщество на твоей стороне. ⭐️ Рейтинг: {new_rating} (+10)"
            + (f"\n\n{winner_summary}" if winner_summary else "")
            + (f"\n\n{comments_text}" if comments_text else ""),
        )
    except Exception:
        pass
    try:
        _bot.send_message(
            loser_id,
            f"⚔️ Батл завершён\n\n"
            f"{winner_nick} — {winner_votes} голосов ({winner_pct}%)\n"
            f"{loser_nick} — {loser_votes} голосов ({loser_pct}%)\n\n"
            f"На этот раз не зашло — но это только один батл. "
            f"Следующий бит может звучать иначе для тех же ушей."
            + (f"\n\n{loser_summary}" if loser_summary else "")
            + (f"\n\n{comments_text}" if comments_text else ""),
        )
    except Exception:
        pass


def finish_battle(bid: str):
    """Легаси-путь: завершение ОДНОГО батла вне слотовой модели (осиротевшие
    батлы старой очереди — см. restore_timers в bot.py). В слотовой модели
    группу батлов слота завершает finish_slot одним вызовом."""
    outcome = _settle_battle(bid)
    if outcome is None:
        return

    winning_side = outcome["winning_side"]
    p1, p2       = outcome["p1"], outcome["p2"]

    # Мгновенный пуш о результате прогноза — каждому, кто голосовал в этом батле.
    # Еженедельная агрегированная сводка интуиции (build_intuition_summary) остаётся
    # отдельной и без изменений — это бонус-статистика поверх мгновенного результата.
    for voter_id, pred_side in outcome["predictions"].items():
        # Защита избыточна (автор не может голосовать в своём батле), но не помешает.
        if voter_id in (p1, p2):
            continue
        if winning_side == 0:
            pred_text = (
                "🤔 Батл, где ты голосовал, завершился вничью — "
                "прогноз не засчитан ни в плюс, ни в минус."
            )
        elif str(pred_side) == str(winning_side):
            pred_text = (
                "✅ Твой прогноз сбылся! Сообщество выбрало именно тот бит, "
                "на который ты поставил в прогнозе. 🧠"
            )
        else:
            pred_text = (
                "❌ Не в этот раз — сообщество выбрало другой бит, чем твой прогноз. "
                "Попробуешь угадать в следующей паре?"
            )
        try:
            _bot.send_message(voter_id, pred_text)
        except Exception:
            pass

    _send_author_battle_result(outcome)


def _force_finish_battle(bid: str, winning_side: int):
    battles = load_battles()
    users   = load_users()

    b = battles.get(bid)
    if not b or b["status"] != "active":
        return

    b["status"]   = "finished"
    b["end_time"] = datetime.now().isoformat()

    p1, p2  = b["player1"], b["player2"]
    p1_nick = users.get(p1, {}).get("nickname", "Игрок 1")
    p2_nick = users.get(p2, {}).get("nickname", "Игрок 2")

    if winning_side == 1:
        winner_id, loser_id     = p1, p2
    else:
        winner_id, loser_id     = p2, p1

    if winner_id in users:
        users[winner_id]["rating"] = users[winner_id].get("rating", 0) + 10
        users[winner_id]["wins"]   = users[winner_id].get("wins", 0) + 1
    b["counted_for_final"] = True

    resolve_pair_ratings(bid, str(winning_side))

    try:
        _scheduler.remove_job(bid)
    except Exception:
        pass

    save_battles(battles)
    save_users(users)

    new_rating = users.get(winner_id, {}).get("rating", 0)
    for pid, result in [
        (winner_id, f"✅ Ты победил! (бит соперника удалён)\n⭐️ Твой рейтинг: {new_rating} (+10)"),
        (loser_id,  "❌ Твой бит был удалён администратором. Батл завершён."),
    ]:
        try:
            _bot.send_message(pid, f"⚔️ Батл завершён досрочно.\n\n{result}")
        except Exception:
            pass

    # финалы упразднены — квалификация теперь по неделям, см. weeks.py


# ─── Решение автора о карьере бита ────────────

def _maybe_resume_career(user_id: str, chat_id):
    """После оплаты билета продолжения — если у автора есть бит в
    awaiting_decision, автоматически возвращает его в набор слота."""
    status = ticket_status(user_id)
    if not status["paid"]:
        return

    beat = find_active_beat_by_user(user_id)
    if not beat or beat["status"] != "awaiting_decision":
        return

    slot_id = ensure_registration_slot()
    register_beat_to_slot(slot_id, beat["id"])
    update_beat_status(beat["id"], "queued")
    consume_ticket(user_id)

    try:
        _bot.send_message(chat_id, "✅ Билет оплачен — твой бит снова в наборе на слот.")
    except Exception:
        pass


def _handle_career_continue(call):
    beat_id = call.data[len("career_continue_"):]
    user_id = str(call.from_user.id)

    beat = get_beat(beat_id)
    if not beat or beat["status"] != "awaiting_decision" or beat["author_id"] != user_id:
        _bot.answer_callback_query(call.id, "Уже неактуально.")
        return

    _bot.answer_callback_query(call.id)
    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    users = load_users()
    discount_applied = 0
    if user_id in users and users[user_id].get("ticket_required", 0) == 0:
        discount_applied = start_ticket(user_id, TICKET_CONTINUE)

    status = ticket_status(user_id)
    discount_note = "\n\n🎁 Скидка за друга применена — тебе нужно оценить на 1 пару меньше!" if discount_applied else ""
    _bot.send_message(
        call.message.chat.id,
        f"▶️ Билет продолжения: оцени {status['required']} пар{'у' if status['required'] == 1 else ''}, "
        f"и бит вернётся в бой. Прогресс: {status['progress']}/{status['required']}.{discount_note}",
    )


def _career_decision_text(beat: dict) -> str:
    return (
        f"🎧 Как дела у твоего бита:\n"
        f"✅ {beat['wins']} побед · ❌ {beat['losses']} поражений · 🤝 {beat['draws']} ничьих\n"
        f"🧠 {beat['predicted_for']} раз сообщество ставило именно на него в прогнозах\n"
        f"🎯 Сыграно {beat['battles_played']} из {MAX_CAREER_BATTLES} батлов\n\n"
        f"Продолжаем его историю?"
    )


def _career_decision_markup(beat_id: str):
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        telebot.types.InlineKeyboardButton("▶️ Продолжить карьеру", callback_data=f"career_continue_{beat_id}"),
        telebot.types.InlineKeyboardButton("🏁 Завершить карьеру", callback_data=f"career_finish_{beat_id}"),
    )
    return markup


def _handle_career_finish(call):
    beat_id = call.data[len("career_finish_"):]
    user_id = str(call.from_user.id)

    beat = get_beat(beat_id)
    if not beat or beat["status"] != "awaiting_decision" or beat["author_id"] != user_id:
        _bot.answer_callback_query(call.id, "Уже неактуально.")
        return

    _bot.answer_callback_query(call.id)
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        telebot.types.InlineKeyboardButton("✅ Да, завершить", callback_data=f"career_finish_confirm_{beat_id}"),
        telebot.types.InlineKeyboardButton("🔙 Назад", callback_data=f"career_finish_cancel_{beat_id}"),
    )
    text = "🏁 Завершить карьеру этого бита?\n\nЭто решение необратимое — продолжить будет нельзя."
    try:
        _bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        _bot.send_message(call.message.chat.id, text, reply_markup=markup)


def _handle_career_finish_confirm(call):
    beat_id = call.data[len("career_finish_confirm_"):]
    user_id = str(call.from_user.id)

    beat = get_beat(beat_id)
    if not beat or beat["status"] != "awaiting_decision" or beat["author_id"] != user_id:
        _bot.answer_callback_query(call.id, "Уже неактуально.")
        return

    finish_beat_career(beat_id)
    _bot.answer_callback_query(call.id)
    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _bot.send_message(call.message.chat.id, "🏁 Карьера завершена. Спасибо, что играл! Если бит в топе недели — увидишь его в Бите недели.")


def _handle_career_finish_cancel(call):
    beat_id = call.data[len("career_finish_cancel_"):]
    user_id = str(call.from_user.id)

    beat = get_beat(beat_id)
    if not beat or beat["status"] != "awaiting_decision" or beat["author_id"] != user_id:
        _bot.answer_callback_query(call.id, "Уже неактуально.")
        return

    _bot.answer_callback_query(call.id)
    text = _career_decision_text(beat)
    markup = _career_decision_markup(beat_id)
    try:
        _bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        _bot.send_message(call.message.chat.id, text, reply_markup=markup)


# ─── Показ батла для голосования ─────────────

def send_battle_for_vote(chat_id, bid, battles):
    b = battles[bid]

    order = [1, 2]
    random.shuffle(order)   # порядок прослушивания рандомный для каждого голосующего
    _first_side_shown[str(chat_id)] = str(order[0])

    for side in order:
        file_id = b.get(f"beat{side}_file_id")
        report_markup = telebot.types.InlineKeyboardMarkup()
        report_markup.add(telebot.types.InlineKeyboardButton(
            f"⚠️ Пожаловаться на бит {side}", callback_data=f"report_{bid}_{side}",
        ))

        caption = f"🎵 Бит {side}"

        if file_id:
            try:
                _bot.send_audio(chat_id, file_id, caption=caption, reply_markup=report_markup)
            except Exception:
                _bot.send_message(chat_id, caption, reply_markup=report_markup)
        else:
            _bot.send_message(chat_id, caption, reply_markup=report_markup)

    # Кнопки голоса доступны сразу — таймер-гейт удалён
    vote_markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    vote_markup.row(
        telebot.types.InlineKeyboardButton("🎵 Бит 1", callback_data=f"vote_{bid}_1"),
        telebot.types.InlineKeyboardButton("🎵 Бит 2", callback_data=f"vote_{bid}_2"),
    )
    _bot.send_message(chat_id, "❤️ Какой бит должен пройти дальше?", reply_markup=vote_markup)

    pair_shown_at[str(chat_id)] = datetime.now().isoformat()


# ─── Новый флоу: голос → прогноз → резюме ────

def _handle_vote(call):
    parts   = call.data.split("_")
    side    = parts[-1]
    bid     = "_".join(parts[1:-1])
    user_id = str(call.from_user.id)

    battles_data = load_battles()
    users        = load_users()
    b = battles_data.get(bid)

    if not b or b["status"] != "active":
        _bot.answer_callback_query(call.id, "Этот батл уже завершён.")
        return
    if user_id in [b["player1"], b["player2"]] and user_id != str(ADMIN_ID):
        _bot.answer_callback_query(call.id, "Нельзя голосовать в своём батле.")
        return
    if user_id in b.get("votes", {}):
        _bot.answer_callback_query(call.id, "Ты уже проголосовал в этом батле.")
        return
    if bid in users.get(user_id, {}).get("votes_this_round", []):
        _bot.answer_callback_query(call.id, "Ты уже оценил этот батл.")
        return

    if "votes" not in b:
        b["votes"] = {}
    b["votes"][user_id] = side
    if side == "1":
        b["votes1"] = b.get("votes1", 0) + 1
    else:
        b["votes2"] = b.get("votes2", 0) + 1
    save_battles(battles_data)

    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _bot.answer_callback_query(call.id, "✅ Голос записан!")

    voted_at[user_id] = datetime.now().isoformat()
    pending_prediction[user_id] = {
        "bid":       bid,
        "vote_side": side,
        "chat_id":   call.message.chat.id,
    }

    # Кнопки прогноза в перемешанном порядке (защита от автопилота)
    pred_sides = ["1", "2"]
    random.shuffle(pred_sides)
    pred_markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    pred_markup.row(*[
        telebot.types.InlineKeyboardButton(f"🎵 Бит {s}", callback_data=f"pred_{bid}_{s}")
        for s in pred_sides
    ])
    _bot.send_message(call.message.chat.id, "🧠 Как думаешь, что выберет большинство?", reply_markup=pred_markup)


def _handle_prediction(call):
    parts     = call.data.split("_")
    pred_side = parts[-1]
    bid       = "_".join(parts[1:-1])
    user_id   = str(call.from_user.id)

    pending = pending_prediction.get(user_id)
    if not pending or pending["bid"] != bid:
        _bot.answer_callback_query(call.id, "Эта сессия уже неактуальна.")
        return

    battles_data = load_battles()
    users        = load_users()
    b = battles_data.get(bid)
    if not b or b["status"] != "active":
        _bot.answer_callback_query(call.id, "Батл уже завершён.")
        return

    if "predictions" not in b:
        b["predictions"] = {}
    b["predictions"][user_id] = pred_side
    save_battles(battles_data)

    # Построчная запись пары в аналитику. Метки времени живут в памяти и могут
    # отсутствовать (рестарт бота) — тогда time_on_pair будет NULL, не падаем.
    add_pair_rating(
        user_id, bid,
        pending["vote_side"], pred_side,
        pair_shown_at.get(user_id), voted_at.get(user_id),
    )

    if user_id in users:
        vtr = users[user_id].get("votes_this_round", [])
        if bid not in vtr:
            vtr.append(bid)
        users[user_id]["votes_this_round"] = vtr
        save_users(users)

    increment_ticket(user_id)
    _maybe_resume_career(user_id, pending["chat_id"])

    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _bot.answer_callback_query(call.id)

    vote_side = pending["vote_side"]
    chat_id   = pending["chat_id"]
    pending_prediction.pop(user_id, None)
    pair_shown_at.pop(user_id, None)
    voted_at.pop(user_id, None)

    _send_vote_summary(chat_id, user_id, bid, vote_side, pred_side, users, b)


def _send_vote_summary(chat_id, user_id: str, bid: str, vote_side: str, pred_side: str, users: dict, b: dict):
    text = (
        f"❤️ Твой выбор: Бит {vote_side}\n"
        f"🧠 Твой прогноз: Бит {pred_side}\n\n"
        f"Узнаешь, угадал ли — как только батл завершится (~{get_slot_voting_hours()} ч). "
        f"А в конце недели — полная сводка твоей интуиции. 🤫"
    )

    status = ticket_status(user_id)
    if status["active"]:
        if status["paid"]:
            text += "\n\n✅ Билет оплачен — теперь можешь загрузить бит! Жми 🎵 Отправить бит."
        else:
            text += f"\n\n🎧 Билет: {status['progress']}/{status['required']}"

    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        telebot.types.InlineKeyboardButton("▶️ Следующая пара", callback_data=f"nextpair_{bid}"),
        telebot.types.InlineKeyboardButton("💬 Оставить развёрнутый отзыв", callback_data=f"feedback_open_{bid}"),
    )
    _bot.send_message(chat_id, text, reply_markup=markup)


def _handle_next_pair(call):
    user_id = str(call.from_user.id)
    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _bot.answer_callback_query(call.id)

    required, already_voted, not_voted = votes_needed(user_id)
    if not not_voted or already_voted >= required:
        ctx  = vote_context.pop(user_id, "free")
        text = (
            "✅ Отлично! Теперь можешь загрузить бит — нажми 🎵 Отправить бит"
            if ctx == "beat"
            else "✅ Проголосовал во всех доступных батлах!"
        )
        clear_vote_session(user_id)
        _bot.send_message(call.message.chat.id, text, reply_markup=get_menu(user_id))
    else:
        send_next_battle_for_vote(call.message.chat.id, user_id, not_voted)


# ─── Опциональные карточки развёрнутого отзыва ─

def _handle_feedback_open(call):
    bid     = call.data[len("feedback_open_"):]
    user_id = str(call.from_user.id)

    battles_data = load_battles()
    b = battles_data.get(bid)
    if not b or b["status"] != "active":
        _bot.answer_callback_query(call.id, "Батл уже завершён, карточку оставить нельзя.")
        return

    if user_id in b.get("feedback", {}):
        _bot.answer_callback_query(call.id, "Ты уже оставил развёрнутый отзыв.")
        return

    first_side = _first_side_shown.get(user_id, "1")
    pending_feedback[user_id] = {
        "bid":          bid,
        "chat_id":      call.message.chat.id,
        "message_id":   None,
        "first_side":   first_side,
        "current_side": first_side,
        "ratings":      {"1": {}, "2": {}},
    }
    _bot.answer_callback_query(call.id)
    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _send_feedback_card(user_id)


def _render_feedback_card(bid: str, side: str, ratings: dict):
    lines = [f"🎵 Оцени Бит {side}:"]
    rows  = []
    for cat_key, cat_label in FEEDBACK_CATEGORIES:
        if cat_key in ratings:
            chosen = ratings[cat_key]
            lines.append(f"✅ {cat_label}: {RATING_EMOJI[chosen]} {RATING_LABELS[chosen]}")
        else:
            lines.append(cat_label)
            rows.append(cat_key)

    markup = None
    if rows:
        markup = telebot.types.InlineKeyboardMarkup(row_width=3)
        for cat_key in rows:
            markup.row(*[
                telebot.types.InlineKeyboardButton(
                    RATING_LABELS[r], callback_data=f"fb_{bid}_{side}_{cat_key}_{r}",
                )
                for r in ("weak", "ok", "fire")
            ])
        markup.add(telebot.types.InlineKeyboardButton(
            "💬 Добавить комментарий (необязательно)", callback_data=f"fbcomment_{bid}",
        ))
    return "\n".join(lines), markup


def _send_feedback_card(user_id: str):
    pending = pending_feedback.get(user_id)
    if not pending:
        return
    bid  = pending["bid"]
    side = pending["current_side"]

    text, markup = _render_feedback_card(bid, side, pending["ratings"][side])
    sent = _bot.send_message(pending["chat_id"], text, reply_markup=markup)
    pending["message_id"] = sent.message_id


def _handle_feedback_rating(call):
    parts    = call.data.split("_")
    rating   = parts[-1]
    category = parts[-2]
    side     = parts[-3]
    bid      = "_".join(parts[1:-3])
    user_id  = str(call.from_user.id)

    pending = pending_feedback.get(user_id)
    if not pending or pending["bid"] != bid or pending["current_side"] != side:
        _bot.answer_callback_query(call.id, "Эта карточка уже неактуальна.")
        return
    if category not in dict(FEEDBACK_CATEGORIES) or rating not in RATING_POINTS:
        _bot.answer_callback_query(call.id, "Неизвестная категория или оценка.")
        return

    ratings_for_side = pending["ratings"][side]
    if category in ratings_for_side:
        _bot.answer_callback_query(call.id, "Эта категория уже оценена.")
        return

    ratings_for_side[category] = rating
    _bot.answer_callback_query(call.id, f"{RATING_EMOJI[rating]} {RATING_LABELS[rating]}")

    text, markup = _render_feedback_card(bid, side, ratings_for_side)
    try:
        _bot.edit_message_text(text, pending["chat_id"], pending["message_id"], reply_markup=markup)
    except Exception:
        pass

    if len(ratings_for_side) < len(FEEDBACK_CATEGORIES):
        return

    other_side = "2" if side == "1" else "1"
    if side == pending["first_side"] and not pending["ratings"][other_side]:
        pending["current_side"] = other_side
        _send_feedback_card(user_id)
        return

    _finalize_feedback_vote(user_id)


def _handle_feedback_comment_btn(call):
    bid     = call.data[len("fbcomment_"):]
    user_id = str(call.from_user.id)
    pending = pending_feedback.get(user_id)
    if not pending or pending["bid"] != bid:
        _bot.answer_callback_query(call.id, "Эта карточка уже неактуальна.")
        return
    _bot.answer_callback_query(call.id)
    _bot.send_message(call.message.chat.id, "💬 Напиши короткий комментарий для автора бита:")
    _bot.register_next_step_handler(call.message, _receive_feedback_comment, user_id, bid)


def _receive_feedback_comment(message, user_id: str, bid: str):
    pending = pending_feedback.get(user_id)
    if not pending or pending["bid"] != bid:
        return
    if not message.text:
        _bot.send_message(message.chat.id, "Комментарий не сохранён — отправь текстом.")
        return
    pending["comment"] = message.text.strip()[:300]
    _bot.send_message(message.chat.id, "✅ Комментарий сохранён, увидит автор бита после батла.")


def _finalize_feedback_vote(user_id: str):
    """Сохраняет развёрнутый отзыв после заполнения всех карточек (опциональный путь)."""
    pending = pending_feedback.pop(user_id, None)
    if not pending:
        return
    bid = pending["bid"]

    battles_data = load_battles()
    b = battles_data.get(bid)
    if not b or b["status"] != "active":
        return

    if "feedback" not in b:
        b["feedback"] = {}
    entry = {"1": pending["ratings"]["1"], "2": pending["ratings"]["2"]}
    if pending.get("comment"):
        entry["comment"] = pending["comment"]
    b["feedback"][user_id] = entry
    save_battles(battles_data)

    _bot.send_message(
        pending["chat_id"],
        "✅ Спасибо за развёрнутый отзыв! Автор бита увидит его после батла.",
    )


def send_next_battle_for_vote(chat_id, user_id, not_voted):
    if not not_voted:
        _bot.send_message(
            chat_id,
            "✅ Ты проголосовал во всех доступных батлах!",
            reply_markup=get_menu(user_id),
        )
        return
    battles_data = load_battles()
    send_battle_for_vote(chat_id, not_voted[0], battles_data)


# ─── Хэндлеры батлов ─────────────────────────

def _handle_report(call):
    parts   = call.data.split("_")
    side    = parts[-1]
    bid     = "_".join(parts[1:-1])
    user_id = str(call.from_user.id)
    users   = load_users()
    battles_data = load_battles()

    b = battles_data.get(bid)
    if not b or b["status"] != "active":
        _bot.answer_callback_query(call.id, "Этот батл уже завершён.")
        return
    if user_id in [b["player1"], b["player2"]]:
        _bot.answer_callback_query(call.id, "Нельзя жаловаться на свой батл.")
        return

    nick   = users.get(user_id, {}).get("nickname", "Неизвестный")
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("🗑 Удалить бит",  callback_data=f"admin_del_beat_{bid}_{side}"),
        telebot.types.InlineKeyboardButton("✅ Ок",           callback_data=f"admin_ok_report_{bid}_{side}"),
    )
    try:
        _bot.send_message(
            ADMIN_ID,
            f"⚠️ Жалоба на батл #{bid}, бит {side}.\nОт: @{nick}",
            reply_markup=markup,
        )
    except Exception:
        pass
    _bot.answer_callback_query(call.id, "⚠️ Жалоба отправлена администратору.")


def _beats_have_met(beat_id_a: str, beat_id_b: str) -> bool:
    battles_data = load_battles()
    pair = {beat_id_a, beat_id_b}
    return any(
        {b.get("beat1_id"), b.get("beat2_id")} == pair
        for b in battles_data.values()
        if b.get("status") in ("active", "finished")
    )


def _pick_opponent(my_beat_id: str, candidates: list):
    """candidates: [(user_id, beat_dict), ...] из очереди той же комнаты.

    Мягкое правило: избегаем соперника, с которым бит уже встречался (finished
    или active батл между этими beat_id), если есть альтернатива в очереди.
    Если альтернативы нет — допускаем повтор, это не жёсткий запрет.
    """
    if not candidates:
        return None
    fresh = [c for c in candidates if not _beats_have_met(my_beat_id, c[1]["id"])]
    return fresh[0] if fresh else candidates[0]


def start_slot(slot_id: str):
    """Бьёт набор слота на пары и создаёт батлы разом. Вызывается извне
    (админ-кнопка добавится в S3.5) — здесь только логика разбивки.

    Единый таймер: вместо индивидуального finish_battle на каждую пару
    вешаем ОДИН таймер на finish_slot(slot_id) — весь слот завершается разом,
    а не батлы вразнобой в разное время.

    Возвращает (True, [bid, ...]) при успехе или (False, причина) если
    слот уже не в registration или участников меньше SLOT_MIN_PARTICIPANTS.
    """
    slot = get_slot(slot_id)
    if not slot or slot["status"] != "registration":
        return False, "слот недоступен для старта"

    beats = []
    for beat_id in slot["registered_beats"]:
        b = get_beat(beat_id)
        # Защитно: бит мог быть снят с набора (пауза/отмена) уже после
        # регистрации в слот, но раньше старта — статус тогда уже не queued.
        if b and b["status"] == "queued":
            beats.append(b)

    if len(beats) < SLOT_MIN_PARTICIPANTS:
        return False, "мало участников"

    # Приоритетные (не попавшие в пару в прошлом слоте) паруются первыми —
    # гарантия, что нечётный хвост не копится бесконечно на одном бите.
    beats.sort(key=lambda b: 0 if b.get("priority_next_slot") else 1)

    users        = load_users()
    battles_data = load_battles()
    today        = datetime.now().date().isoformat()

    # MAX+1, а не COUNT+1/len+1 — устойчиво к удалениям (например, кнопкой
    # «Сбросить тестовые данные») и к нескольким парам за один вызов: len()
    # растёт только после save_battles(), а new_battle_n инкрементится
    # локально на каждую пару этого цикла, поэтому коллизий внутри одного
    # start_slot тоже не будет.
    existing_nums = [
        int(k[len("battle_"):]) for k in battles_data
        if k.startswith("battle_") and k[len("battle_"):].isdigit()
    ]
    next_battle_n = (max(existing_nums) + 1) if existing_nums else 1

    remaining           = list(beats)
    created_battle_ids  = []
    battle_participants = []   # [(bid, p1_id, p2_id), ...] — для пушей после сохранения

    while len(remaining) >= 2:
        beat = remaining.pop(0)
        candidates = [(c["author_id"], c) for c in remaining]
        opponent_id, opponent_beat = _pick_opponent(beat["id"], candidates)
        remaining.remove(opponent_beat)

        p1_id, p2_id = beat["author_id"], opponent_id
        bid          = f"battle_{next_battle_n}"
        next_battle_n += 1
        start_time   = datetime.now()

        battles_data[bid] = {
            "player1":       p1_id,
            "player2":       p2_id,
            "beat1_file_id": beat["file_id"],
            "beat2_file_id": opponent_beat["file_id"],
            "beat1_id":      beat["id"],
            "beat2_id":      opponent_beat["id"],
            "votes1":        0,
            "votes2":        0,
            "voters":        {},
            "votes":         {},
            "predictions":   {},
            "feedback":      {},
            "status":        "active",
            "room":          ROOMS[0],
            "start_time":    start_time.isoformat(),
        }
        update_beat_status(beat["id"], "battling")
        update_beat_status(opponent_beat["id"], "battling")
        # Приоритет отработал — сбрасываем, чтобы не липнул на бит навсегда.
        if beat.get("priority_next_slot"):
            set_beat_priority(beat["id"], 0)
        if opponent_beat.get("priority_next_slot"):
            set_beat_priority(opponent_beat["id"], 0)

        for pid in (p1_id, p2_id):
            if pid in users:
                if users[pid].get("last_battle_date") != today:
                    users[pid]["battles_today"]    = 0
                    users[pid]["last_battle_date"] = today
                users[pid]["battles_today"]    = users[pid].get("battles_today", 0) + 1
                users[pid]["votes_this_round"] = []

        created_battle_ids.append(bid)
        battle_participants.append((bid, p1_id, p2_id))

    leftover_beat = remaining[0] if remaining else None

    save_battles(battles_data)
    save_users(users)

    # Переводим слот в running ДО ensure_registration_slot() для нечётного
    # остатка — иначе ensure_registration_slot() увидит этот же слот ещё в
    # registration и вернёт его вместо создания нового.
    start          = datetime.now()
    voting_ends_at = start + timedelta(hours=get_slot_voting_hours())
    update_slot(
        slot_id,
        status="running",
        started_at=start.isoformat(),
        voting_ends_at=voting_ends_at.isoformat(),
        battle_ids=json.dumps(created_battle_ids, ensure_ascii=False),
    )

    if leftover_beat:
        set_beat_priority(leftover_beat["id"], 1)
        next_slot_id = ensure_registration_slot()
        register_beat_to_slot(next_slot_id, leftover_beat["id"])
        try:
            _bot.send_message(
                leftover_beat["author_id"],
                "⏳ В этом слоте не хватило пары — твой бит приоритетный в следующем, сыграет гарантированно.",
            )
        except Exception:
            pass

    _scheduler.add_job(
        finish_slot,
        "date",
        run_date=voting_ends_at,
        args=[slot_id],
        id=f"slot_{slot_id}",
        replace_existing=True,
    )

    for bid, p1_id, p2_id in battle_participants:
        p1_nick = users.get(p1_id, {}).get("nickname", "Соперник")
        p2_nick = users.get(p2_id, {}).get("nickname", "Соперник")
        try:
            _bot.send_message(
                p1_id,
                f"⚔️ Слот стартовал! Твой бит в батле с {p2_nick}.\n\n"
                f"Голосование идёт {get_slot_voting_hours()} ч — узнаешь результат, как только оно закроется.",
            )
        except Exception:
            pass
        try:
            _bot.send_message(
                p2_id,
                f"⚔️ Слот стартовал! Твой бит в батле с {p1_nick}.\n\n"
                f"Голосование идёт {get_slot_voting_hours()} ч — узнаешь результат, как только оно закроется.",
            )
        except Exception:
            pass

    # Все, чей бит был в этом наборе (спарен или ушёл приоритетным лишним),
    # уже получили персональный пуш выше — не дублируем общим "новый батл".
    all_registered_ids = {b["author_id"] for b in beats}
    _notify_new_battle(exclude_ids=all_registered_ids)

    return True, created_battle_ids


def finish_slot(slot_id: str):
    """Завершает слот целиком: считает исход каждого батла слота через
    _settle_battle, шлёт голосовавшим ОДНО сводное сообщение по всем батлам,
    где они голосовали (вместо пер-батловых пушей прогноза из finish_battle),
    авторам — обычный пуш о результате их бита, сбрасывает голосовые сессии
    и переводит слот в finished."""
    slot = get_slot(slot_id)
    if not slot or slot["status"] != "running":
        return

    outcomes = []
    for bid in slot["battle_ids"]:
        outcome = _settle_battle(bid)
        if outcome is not None:
            outcomes.append(outcome)

    # voter_id -> [строка по батлу 1, строка по батлу 2, ...] — порядок
    # батлов слота сохраняется для нумерации в сводке.
    voter_lines: dict = {}
    for outcome in outcomes:
        b            = outcome["b"]
        p1, p2       = outcome["p1"], outcome["p2"]
        p1_nick      = outcome["p1_nick"]
        p2_nick      = outcome["p2_nick"]
        votes1       = outcome["votes1"]
        votes2       = outcome["votes2"]
        winning_side = outcome["winning_side"]
        votes        = b.get("votes", {})

        for voter_id, pred_side in outcome["predictions"].items():
            if voter_id in (p1, p2):
                continue
            vote_side  = votes.get(voter_id)
            voted_nick = p1_nick if vote_side == "1" else (p2_nick if vote_side == "2" else "?")

            if winning_side == 0:
                verdict = "🤝 ничья"
            elif str(pred_side) == str(winning_side):
                verdict = "✅ угадал большинство"
            else:
                verdict = "❌ большинство выбрало другого"

            line = f"{p1_nick} ({votes1}) 🆚 {p2_nick} ({votes2}) — ты голосовал за {voted_nick}, {verdict}."
            voter_lines.setdefault(voter_id, []).append(line)

    for voter_id, lines in voter_lines.items():
        numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1))
        try:
            _bot.send_message(
                voter_id,
                f"🏁 Слот завершён! Итоги батлов, где ты голосовал:\n\n{numbered}",
            )
        except Exception:
            pass

    for outcome in outcomes:
        _send_author_battle_result(outcome)

    # Сессионные сбросы для завершённого голосования — на пилоте одно активное
    # голосование одновременно, глобальная очистка безопасна (перенесено из
    # упразднённого _admin_stop_round, см. S3.5).
    vote_session.clear()
    pending_prediction.clear()
    pair_shown_at.clear()
    voted_at.clear()

    update_slot(slot_id, status="finished", finished_at=datetime.now().isoformat())


def _send_beat(message):
    user_id = str(message.from_user.id)

    from weeks import weekly_gate_check
    passed, hint = weekly_gate_check(user_id)
    if not passed:
        _bot.send_message(message.chat.id, hint, reply_markup=get_menu(user_id))
        return

    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    u = users[user_id]

    # Проверка "уже в наборе" идёт раньше остальных гейтов — иначе юзер не
    # сможет добраться до кнопки отмены, упираясь в лимит/билет.
    if user_in_registration(user_id):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("❌ Отменить бит", callback_data="cancel_beat"),
            telebot.types.InlineKeyboardButton("🔙 Назад",        callback_data="cancel_ignore"),
        )
        _bot.send_message(message.chat.id, "⏳ Твой бит уже в наборе.\n\nХочешь отменить?", reply_markup=markup)
        return

    exhausted, _ = check_daily_limit(u)
    save_users(users)
    if exhausted:
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m в 00:00")
        _bot.send_message(
            message.chat.id,
            f"⛔️ Дневной лимит исчерпан. Обновится {tomorrow}.\n\n"
            f"{'Pro-подписка даёт 3 батла в день.' if not u.get('is_pro') else ''}",
            reply_markup=get_menu(user_id),
        )
        return

    if find_active_beat_by_user(user_id):
        _bot.send_message(
            message.chat.id,
            "⚠️ У тебя уже есть активный бит. Дождись окончания его карьеры, потом сможешь отправить новый.",
            reply_markup=get_menu(user_id),
        )
        return

    # Bootstrap: если в системе физически нет ни одного активного батла (а не
    # просто "нечего оценить лично этому юзеру" — has_any_votable_battle тут
    # не подходит, она ложно срабатывает, если юзер уже оценил все доступные
    # батлы), билет не запрашиваем вообще — не start_ticket/не consume_ticket,
    # билет остаётся неактивным, как будто его никто не начинал платить.
    # Пересчитывается при каждом входе, не флаг.
    if not system_has_active_battles():
        vote_context[f"room_{user_id}"] = ROOMS[0]
        _bot.send_message(
            message.chat.id,
            "🎧 Сейчас в системе ещё нет активных батлов для оценки — "
            "твой бит станет одним из первых!\n\n"
            "Отправь аудиофайл с битом.",
        )
        return

    # Активируем билет только если он ещё не начат — не сбрасываем прогресс
    # уже начавшего платить пользователя.
    discount_applied = 0
    if u.get("ticket_required", 0) == 0:
        discount_applied = start_ticket(user_id, TICKET_FIRST)

    status = ticket_status(user_id)
    if status["paid"]:
        vote_context[f"room_{user_id}"] = ROOMS[0]
        _bot.send_message(message.chat.id, "🎵 Отправь аудиофайл с битом:")
        return

    discount_note = "\n\n🎁 Скидка за друга применена — тебе нужно оценить на 1 пару меньше!" if discount_applied else ""
    _bot.send_message(
        message.chat.id,
        f"🎧 Чтобы твой бит вступил в батл, оцени {status['required']} чужих пар.\n\n"
        f"Прогресс: {status['progress']}/{status['required']}\n\n"
        f"Нажми 🗳 Голосовать, чтобы начать.{discount_note}",
        reply_markup=get_menu(user_id),
    )


def _edit_beat(message):
    """Замена аудиофайла уже стоящего в наборе бита — билет уже оплачен за
    участие бита, а не за конкретный файл, поэтому замена бесплатна."""
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    slot_id = user_in_registration(user_id)
    if not slot_id:
        _bot.send_message(message.chat.id, "Бит уже не в наборе — редактировать нечего.", reply_markup=get_menu(user_id))
        return

    # Комната больше не значима для набора слота (слот — плоский список
    # битов, мультикомнатность отложена) — флаг только маркирует режим
    # редактирования для _receive_beat, значение неважно, раз комната одна.
    vote_context[f"editing_{user_id}"] = ROOMS[0]
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("❌ Отменить бит вместо замены", callback_data="cancel_beat"))
    _bot.send_message(
        message.chat.id,
        "✏️ Отправь новый аудиофайл — он заменит текущий бит. Это бесплатно, билет не расходуется.",
        reply_markup=markup,
    )


def _notify_new_battle(exclude_ids: set):
    users   = load_users()
    now     = datetime.now()
    changed = False

    for uid, u in users.items():
        if uid in exclude_ids:
            continue
        if u.get("role") not in ("listener", "beatmaker"):
            continue

        last_notified = u.get("last_notified_at")
        if last_notified:
            try:
                last_dt = datetime.fromisoformat(last_notified)
            except ValueError:
                last_dt = None
            if last_dt and (now - last_dt).total_seconds() < NOTIFY_THROTTLE_HOURS * 3600:
                continue

        try:
            _bot.send_message(uid, "⚔️ Новый батл ждёт твоего голоса! Нажми 🗳 Голосовать")
        except Exception:
            continue

        u["last_notified_at"] = now.isoformat()
        changed = True

    if changed:
        save_users(users)


def _handle_room_select(call):
    room    = call.data.split("_", 1)[1]
    user_id = str(call.from_user.id)

    if room not in ROOMS:
        _bot.answer_callback_query(call.id, "Неизвестная комната")
        return

    _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    vote_context[f"room_{user_id}"] = room

    _bot.send_message(call.message.chat.id, f"✅ Комната: {ROOM_LABELS[room]}\n\nОтправь аудиофайл с битом:")
    _bot.answer_callback_query(call.id)


def _handle_cancel_beat(call):
    """Отмена — это пауза, не завершение карьеры: прогресс (battles_played)
    сохраняется, бит можно позже вернуть в набор кнопкой «Мой батл»
    (см. _handle_resume_paused)."""
    user_id = str(call.from_user.id)
    _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    if call.data == "cancel_ignore":
        _bot.answer_callback_query(call.id)
        _bot.send_message(call.message.chat.id, "Хорошо, бит остаётся в наборе.", reply_markup=get_menu(user_id))
        return

    vote_context.pop(f"editing_{user_id}", None)

    slot_id = user_in_registration(user_id)
    beat    = find_active_beat_by_user(user_id)
    if slot_id and beat:
        remove_beat_from_registration(beat["id"])
        update_beat_status(beat["id"], "paused")
        _bot.answer_callback_query(call.id, "Бит поставлен на паузу.")
        _bot.send_message(
            call.message.chat.id,
            "⏸️ Бит снят с набора и поставлен на паузу. Прогресс карьеры сохранён — "
            "сможешь вернуть его в бой в любой момент кнопкой «⚔️ Мой батл».",
            reply_markup=get_menu(user_id),
        )
    else:
        _bot.answer_callback_query(call.id, "Бит уже не в наборе.")
        _bot.send_message(call.message.chat.id, "Бит уже не в наборе.", reply_markup=get_menu(user_id))


def _handle_resume_paused(call):
    user_id = str(call.from_user.id)
    beat    = find_active_beat_by_user(user_id)

    if not beat or beat["status"] != "paused":
        _bot.answer_callback_query(call.id, "Нечего возвращать.")
        return

    slot_id = ensure_registration_slot()
    register_beat_to_slot(slot_id, beat["id"])
    update_beat_status(beat["id"], "queued")

    _bot.answer_callback_query(call.id, "Бит возвращён в набор.")
    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _bot.send_message(
        call.message.chat.id,
        f"▶️ Бит вернулся в набор с сохранённым прогрессом "
        f"({beat.get('battles_played', 0)}/{MAX_CAREER_BATTLES} батлов). Ждём старта слота.",
        reply_markup=get_menu(user_id),
    )


def _receive_beat(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    if vote_context.get(f"editing_{user_id}"):
        if not user_in_registration(user_id):
            vote_context.pop(f"editing_{user_id}", None)
            _bot.send_message(message.chat.id, "⏳ Бит уже не в наборе — редактирование отменено.", reply_markup=get_menu(user_id))
            return
        if message.audio:
            file_id = message.audio.file_id
        elif message.voice:
            file_id = message.voice.file_id
        elif message.document:
            file_id = message.document.file_id
        else:
            _bot.send_message(message.chat.id, "Пришли аудиофайл, чтобы заменить бит.")
            return
        active_beat = find_active_beat_by_user(user_id)
        if active_beat:
            update_beat_file(active_beat["id"], file_id)
        vote_context.pop(f"editing_{user_id}", None)
        _bot.send_message(message.chat.id, "✅ Бит обновлён в наборе.", reply_markup=get_menu(user_id))
        return

    if find_active_beat_by_user(user_id):
        _bot.send_message(
            message.chat.id,
            "⚠️ У тебя уже есть активный бит в системе. Дождись окончания его карьеры.",
            reply_markup=get_menu(user_id),
        )
        return

    room_key = f"room_{user_id}"
    room     = vote_context.get(room_key)
    if not room:
        _bot.send_message(
            message.chat.id,
            "Сначала выбери жанровую комнату — нажми 🎵 Отправить бит",
            reply_markup=get_menu(user_id),
        )
        return

    if not system_has_active_battles():
        # Бутстрап: в системе физически нет ни одного активного батла —
        # билет не требуется для этого входа, независимо от того, начинал ли
        # пользователь его когда-то платить. has_any_votable_battle тут не
        # подходит: она ложно сработала бы, если юзер уже оценил все
        # доступные батлы (а не когда их нет вообще). Ничего не "потребляем" —
        # просто идём дальше по обычному флоу приёма бита, как если бы билет
        # был не нужен вовсе.
        pass
    else:
        status = ticket_status(user_id)
        if not status["paid"]:
            vote_context.pop(room_key, None)
            _bot.send_message(
                message.chat.id,
                f"⚠️ Сначала оплати билет: оцени {status['required']} пар.\n"
                f"Прогресс: {status['progress']}/{status['required']}.",
                reply_markup=get_menu(user_id),
            )
            return

    u = users[user_id]
    exhausted, _ = check_daily_limit(u)
    if exhausted:
        vote_context.pop(room_key, None)
        _bot.send_message(message.chat.id, "⛔️ Условия изменились — дневной лимит исчерпан.", reply_markup=get_menu(user_id))
        return

    vote_context.pop(room_key, None)

    if message.audio:
        file_id = message.audio.file_id
    elif message.voice:
        file_id = message.voice.file_id
    else:
        file_id = message.document.file_id

    if user_in_registration(user_id):
        _bot.send_message(message.chat.id, "⏳ Твой бит уже в наборе!", reply_markup=get_menu(user_id))
        return

    # Слот игнорирует комнаты (мультикомнатность отложена — набор слота это
    # плоский список beat_id без привязки к room). Выбор комнаты выше остаётся
    # только UI-гейтом «сначала выбери, потом отправляй», дальше `room` не
    # передаётся ни в create_beat, ни в слот. Матчинг на пары и создание
    # батла переезжают в S3 (старт слота) — _pick_opponent/_beats_have_met
    # здесь больше не вызываются, только определены на будущее.
    beat_id = create_beat(user_id, file_id)
    slot_id = ensure_registration_slot()
    register_beat_to_slot(slot_id, beat_id)

    u["votes_this_round"] = []
    save_users(users)
    consume_ticket(user_id)

    _bot.send_message(
        message.chat.id,
        "✅ Бит принят в набор!\n\n"
        "Как только слот наберётся и стартует — начнётся голосование. "
        "Ты получишь уведомление. ⏳",
        reply_markup=get_menu(user_id),
    )


def _vote_menu(message):
    user_id = str(message.from_user.id)

    from weeks import weekly_gate_check
    passed, hint = weekly_gate_check(user_id)
    if not passed:
        _bot.send_message(message.chat.id, hint, reply_markup=get_menu(user_id))
        return

    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    vote_context[user_id] = "free"
    _, _, not_voted = start_vote_session(user_id)

    if not not_voted:
        _bot.send_message(
            message.chat.id,
            "✅ Ты уже оценил все активные батлы!\n\n"
            "Загляни чуть позже — как только появятся новые пары, сможешь снова голосовать.",
            reply_markup=get_menu(user_id),
        )
        return

    status      = ticket_status(user_id)
    ticket_line = ""
    if status["active"]:
        if status["paid"]:
            ticket_line = "✅ Билет оплачен — можешь загружать бит.\n\n"
        else:
            ticket_line = f"🎧 Билет: {status['progress']}/{status['required']} пар оценено.\n\n"

    pairs_count = len(not_voted)
    pairs_word  = "пара" if pairs_count == 1 else ("пары" if 2 <= pairs_count <= 4 else "пар")
    _bot.send_message(
        message.chat.id,
        f"{ticket_line}Впереди {pairs_count} {pairs_word}. "
        f"Слушай оба бита в каждой, выбирай сильнейший — а потом угадай, что выберет большинство.",
    )
    send_next_battle_for_vote(message.chat.id, user_id, not_voted)


def _my_battle(message):
    user_id = str(message.from_user.id)

    from weeks import weekly_gate_check
    passed, hint = weekly_gate_check(user_id)
    if not passed:
        _bot.send_message(message.chat.id, hint, reply_markup=get_menu(user_id))
        return

    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    if user_in_registration(user_id):
        _bot.send_message(message.chat.id, "⏳ Твой бит в наборе — ждём старта слота.", reply_markup=get_menu(user_id))
        return

    paused_beat = find_active_beat_by_user(user_id)
    if paused_beat and paused_beat["status"] == "paused":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("▶️ Вернуть бит в набор", callback_data="resume_paused"))
        _bot.send_message(
            message.chat.id,
            f"⏸️ Твой бит на паузе.\n"
            f"Прогресс: {paused_beat.get('battles_played', 0)}/{MAX_CAREER_BATTLES}",
            reply_markup=markup,
        )
        return

    bid, b = find_user_battle(user_id)
    if not b:
        _bot.send_message(
            message.chat.id,
            "У тебя нет активного батла.\nЗагрузи бит — нажми 🎵 Отправить бит",
            reply_markup=get_menu(user_id),
        )
        return

    p1_id, p2_id   = b["player1"], b["player2"]
    p1_nick        = users.get(p1_id, {}).get("nickname", "Игрок 1")
    p2_nick        = users.get(p2_id, {}).get("nickname", "Игрок 2")
    votes1, votes2 = b.get("votes1", 0), b.get("votes2", 0)
    total          = votes1 + votes2

    if user_id == p1_id:
        my_nick, my_v, opp_nick, opp_v = p1_nick, votes1, p2_nick, votes2
    else:
        my_nick, my_v, opp_nick, opp_v = p2_nick, votes2, p1_nick, votes1

    my_pct  = round(my_v / total * 100) if total > 0 else 0
    opp_pct = 100 - my_pct

    status_label = "✅ Завершён" if b["status"] == "finished" else "⏳ Идёт"

    time_line = ""
    if b.get("status") == "active":
        # Время завершения принадлежит слоту (voting_ends_at), а не батлу —
        # единый таймер вешается на весь слот в start_slot. Fallback на
        # старую батл-таймерную логику только для осиротевших легаси-батлов
        # без своего running-слота (созданных до слотовой модели).
        slot = get_running_slot()
        if slot and slot.get("voting_ends_at") and bid in slot.get("battle_ids", []):
            end_time = datetime.fromisoformat(slot["voting_ends_at"])
        else:
            start_time = datetime.fromisoformat(b["start_time"])
            end_time   = start_time + timedelta(hours=get_battle_hours())
        remaining = end_time - datetime.now()
        if remaining.total_seconds() > 0:
            hours_left   = int(remaining.total_seconds() // 3600)
            minutes_left = int((remaining.total_seconds() % 3600) // 60)
            if hours_left > 0:
                time_str = f"{hours_left} ч {minutes_left} мин"
            else:
                time_str = f"{minutes_left} мин"
            time_line = f"\n⏳ До завершения: {time_str}"
        else:
            time_line = "\n⏳ Батл вот-вот завершится"

    _bot.send_message(
        message.chat.id,
        f"⚔️ Твой батл — {status_label}\n\n"
        f"🎵 {my_nick} (ты) — {my_v} голосов ({my_pct}%)\n"
        f"🎵 {opp_nick} — {opp_v} голосов ({opp_pct}%)\n\n"
        f"📊 Всего голосов: {total}"
        f"{time_line}",
        reply_markup=get_menu(user_id),
    )


# ─── Регистрация хэндлеров ───────────────────

def register_handlers(bot: telebot.TeleBot):
    bot.message_handler(func=lambda m: m.text == "🎵 Отправить бит")(_send_beat)
    bot.message_handler(func=lambda m: m.text == "✏️ Редактировать бит")(_edit_beat)
    bot.message_handler(func=lambda m: m.text == "⚔️ Мой батл")(_my_battle)
    bot.message_handler(func=lambda m: m.text == "🗳 Голосовать")(_vote_menu)
    bot.message_handler(content_types=["audio", "voice", "document"])(_receive_beat)
    bot.callback_query_handler(func=lambda c: c.data.startswith("room_"))(_handle_room_select)
    bot.callback_query_handler(func=lambda c: c.data in ["cancel_beat", "cancel_ignore"])(_handle_cancel_beat)
    bot.callback_query_handler(func=lambda c: c.data == "resume_paused")(_handle_resume_paused)
    bot.callback_query_handler(func=lambda c: c.data.startswith("vote_"))(_handle_vote)
    bot.callback_query_handler(func=lambda c: c.data.startswith("pred_"))(_handle_prediction)
    bot.callback_query_handler(func=lambda c: c.data.startswith("nextpair_"))(_handle_next_pair)
    bot.callback_query_handler(func=lambda c: c.data.startswith("feedback_open_"))(_handle_feedback_open)
    bot.callback_query_handler(func=lambda c: c.data.startswith("fbcomment_"))(_handle_feedback_comment_btn)
    bot.callback_query_handler(func=lambda c: c.data.startswith("fb_"))(_handle_feedback_rating)
    bot.callback_query_handler(func=lambda c: c.data.startswith("report_"))(_handle_report)
    # Более специфичные career_finish_confirm_/cancel_ регистрируются раньше
    # общего career_finish_, иначе он перехватит их (общий префикс).
    bot.callback_query_handler(func=lambda c: c.data.startswith("career_continue_"))(_handle_career_continue)
    bot.callback_query_handler(func=lambda c: c.data.startswith("career_finish_confirm_"))(_handle_career_finish_confirm)
    bot.callback_query_handler(func=lambda c: c.data.startswith("career_finish_cancel_"))(_handle_career_finish_cancel)
    bot.callback_query_handler(func=lambda c: c.data.startswith("career_finish_"))(_handle_career_finish)
