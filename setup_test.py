import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
BATTLES_FILE = os.path.join(BASE_DIR, "battles.json")
QUEUE_FILE = os.path.join(BASE_DIR, "queue.json")

# 10 тестовых участников
test_users = {
    "1000000001": {"nickname": "Drako", "rating": 150, "battles_played": 10, "wins": 7, "votes_this_round": []},
    "1000000002": {"nickname": "SoundKing", "rating": 120, "battles_played": 8, "wins": 5, "votes_this_round": []},
    "1000000003": {"nickname": "BeatLord", "rating": 90, "battles_played": 6, "wins": 3, "votes_this_round": []},
    "1000000004": {"nickname": "TrapGod", "rating": 80, "battles_played": 5, "wins": 2, "votes_this_round": []},
    "1000000005": {"nickname": "LofiMaster", "rating": 70, "battles_played": 4, "wins": 2, "votes_this_round": []},
    "1000000006": {"nickname": "808King", "rating": 60, "battles_played": 4, "wins": 2, "votes_this_round": []},
    "1000000007": {"nickname": "FlipSide", "rating": 50, "battles_played": 3, "wins": 1, "votes_this_round": []},
    "1000000008": {"nickname": "SubBass", "rating": 40, "battles_played": 3, "wins": 1, "votes_this_round": []},
    "1000000009": {"nickname": "GrimBeats", "rating": 30, "battles_played": 2, "wins": 1, "votes_this_round": []},
    "1000000010": {"nickname": "CloudProd", "rating": 20, "battles_played": 2, "wins": 0, "votes_this_round": []},
}

# Твой аккаунт
your_id = "1030069328"
test_users[your_id] = {
    "nickname": "Ты",
    "rating": 0,
    "battles_played": 0,
    "wins": 0,
    "votes_this_round": []
}

# 5 активных батлов между тестовыми участниками
# Используем реальный file_id — заглушка, голосование интерфейса работает
FAKE_FILE_ID = "BQACAgIAAxkBAAIBY2V4dGVzdF9hdWRpb19maWxlX2lkAAEC"

battles = {
    "battle_1": {
        "player1": "1000000001",
        "player2": "1000000002",
        "beat1_file_id": FAKE_FILE_ID,
        "beat2_file_id": FAKE_FILE_ID,
        "votes1": 8,
        "votes2": 5,
        "status": "active"
    },
    "battle_2": {
        "player1": "1000000003",
        "player2": "1000000004",
        "beat1_file_id": FAKE_FILE_ID,
        "beat2_file_id": FAKE_FILE_ID,
        "votes1": 3,
        "votes2": 7,
        "status": "active"
    },
    "battle_3": {
        "player1": "1000000005",
        "player2": "1000000006",
        "beat1_file_id": FAKE_FILE_ID,
        "beat2_file_id": FAKE_FILE_ID,
        "votes1": 12,
        "votes2": 4,
        "status": "active"
    },
    "battle_4": {
        "player1": "1000000007",
        "player2": "1000000008",
        "beat1_file_id": FAKE_FILE_ID,
        "beat2_file_id": FAKE_FILE_ID,
        "votes1": 6,
        "votes2": 9,
        "status": "active"
    },
    "battle_5": {
        "player1": "1000000009",
        "player2": "1000000010",
        "beat1_file_id": FAKE_FILE_ID,
        "beat2_file_id": FAKE_FILE_ID,
        "votes1": 2,
        "votes2": 11,
        "status": "active"
    },
}

# SoundKing ждёт тебя в очереди как соперник
queue = {
    "1000000002": FAKE_FILE_ID
}

with open(USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(test_users, f, ensure_ascii=False, indent=4)

with open(BATTLES_FILE, "w", encoding="utf-8") as f:
    json.dump(battles, f, ensure_ascii=False, indent=4)

with open(QUEUE_FILE, "w", encoding="utf-8") as f:
    json.dump(queue, f, ensure_ascii=False, indent=4)

print("✅ Тестовые данные созданы!")
print("- 10 участников добавлено")
print("- 5 активных батлов создано")
print("- SoundKing ждёт тебя в очереди")
print("\nЧто делать:")
print("1. Запусти бота — py bot.py")
print("2. Нажми '🎵 Отправить бит' — увидишь батлы для голосования")
print("3. Проголосуй в 5 батлах")
print("4. Отправь любой аудиофайл — батл с SoundKing начнётся")
print("5. Нажми '⚔️ Мой батл' — увидишь счёт")
