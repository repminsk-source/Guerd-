"""
Aegis | Server Guardian — конфигурация и константы по умолчанию.
Значения ниже — дефолты. Реальные настройки для каждого сервера
хранятся в БД (таблица guild_config) и могут быть изменены командами.
"""

import os

# ─── Discord токен ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# ─── Общие ───────────────────────────────────────────────────────
BOT_NAME = "Aegis | Server Guardian"
EMBED_COLOR = 0x2B2D31
EMBED_COLOR_ALERT = 0xE74C3C
EMBED_COLOR_WARN = 0xF1C40F
EMBED_COLOR_OK = 0x2ECC71

DB_PATH = os.environ.get("AEGIS_DB_PATH", "aegis.db")

# ─── Опасные права (Permissions), за выдачу которых наказываем ──
DANGEROUS_PERMISSIONS = [
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "kick_members",
    "ban_members",
    "manage_messages",
    "mention_everyone",
]

# ─── Антинюк: лимиты действий (сколько действий за сколько секунд) ──
# Формат: (максимум_действий, окно_в_секундах)
ANTINUKE_LIMITS = {
    "channel_delete": (3, 10),
    "channel_create": (5, 10),
    "role_delete": (3, 10),
    "role_create": (5, 10),
    "member_ban": (5, 10),
    "member_kick": (5, 10),
    "webhook_create": (3, 10),
    "role_permission_update": (2, 10),   # выдача опасных прав роли
    "member_role_update": (5, 10),        # массовая выдача опасных ролей юзерам
}

# Наказание по умолчанию за превышение лимита антинюка
# варианты: "strip_roles", "kick", "ban"
DEFAULT_ANTINUKE_PUNISHMENT = "strip_roles"

# ─── Антиспам ────────────────────────────────────────────────────
ANTISPAM_MESSAGE_LIMIT = 5          # сообщений
ANTISPAM_MESSAGE_WINDOW = 5          # за N секунд
ANTISPAM_DUPLICATE_LIMIT = 3         # повторов одного и того же сообщения подряд
ANTISPAM_MENTION_LIMIT = 5           # упоминаний в одном сообщении
ANTISPAM_MASS_MENTION_LIMIT = 8      # суммарно упоминаний за окно ANTISPAM_MESSAGE_WINDOW
DEFAULT_SPAM_PUNISHMENT = "mute"    # "mute" | "kick" | "ban"
DEFAULT_MUTE_DURATION_MIN = 10       # минут тайм-аута за спам

INVITE_REGEX = r"(discord\.gg|discord(?:app)?\.com/invite)/\S+"
URL_REGEX = r"https?://\S+"

# ─── Защита от рейда входом ──────────────────────────────────────
RAID_JOIN_LIMIT = 10                 # входов
RAID_JOIN_WINDOW = 30                 # за N секунд -> подозрение на рейд
MIN_ACCOUNT_AGE_HOURS = 24           # аккаунты младше — авто-карантин при подозрении

# ─── Роль карантина (создаётся автоматически, если её нет) ──────
QUARANTINE_ROLE_NAME = "Aegis-Quarantine"

# ─── Прочее ──────────────────────────────────────────────────────
AUDIT_LOG_POLL_DELAY = 1.5           # сек, задержка перед запросом audit log (пока Discord его обновит)
COMMAND_PREFIX = "!"                  # префикс на случай текстовых команд (основные — slash)
