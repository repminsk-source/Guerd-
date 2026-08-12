"""
Aegis | Server Guardian — вся защитная логика.

Разделы:
  1. RateTracker      — скользящее окно для подсчёта действий (антинюк-лимиты)
  2. Whitelist helper  — проверка "можно ли трогать этого юзера"
  3. Punishment        — применение наказания к нарушителю
  4. AntiNuke cog       — слушает on_audit_log_entry_create, считает лимиты, наказывает
  5. AntiSpam cog       — слушает on_message, считает флуд/масс-пинги/инвайты
  6. Logging helper     — отправка embed в лог-канал + DM владельцу при угрозе
  7. Backup / Restore   — снимок структуры сервера и восстановление из него
  8. Panic mode         — экстренная блокировка сервера
  9. RaidJoin cog        — защита от массового входа ботов/аккаунтов
"""

import json
import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord.ext import commands

import config
import database as db


# ────────────────────────────────────────────────────────────────
# 1. RateTracker — считает "N действий за M секунд" на юзера/тип
# ────────────────────────────────────────────────────────────────

class RateTracker:
    """
    Хранит в памяти временные метки действий: {(guild_id, user_id, action): deque[timestamps]}
    Не persistent — это ок, лимиты живут секунды-минуты, не переживают рестарт и не должны.
    """

    def __init__(self):
        self._data: dict[tuple, deque] = defaultdict(deque)

    def hit(self, guild_id: int, user_id: int, action: str, window: int) -> int:
        """Регистрирует действие и возвращает текущее количество действий в окне."""
        key = (guild_id, user_id, action)
        now = time.monotonic()
        dq = self._data[key]
        dq.append(now)
        while dq and now - dq[0] > window:
            dq.popleft()
        return len(dq)

    def reset(self, guild_id: int, user_id: int, action: str):
        self._data.pop((guild_id, user_id, action), None)


rate_tracker = RateTracker()

# (guild_id, user_id) -> monotonic() времени входа. Используется AntiNuke,
# чтобы применять резко ужесточённый лимит к ботам, которые зашли на сервер
# совсем недавно — см. config.NEW_BOT_STRICT_WINDOW и config.NEW_BOT_STRICT_LIMIT.
recent_bot_joins: dict[tuple, float] = {}


def is_recently_joined_bot(guild_id: int, user_id: int) -> bool:
    joined_at = recent_bot_joins.get((guild_id, user_id))
    if joined_at is None:
        return False
    if time.monotonic() - joined_at > config.NEW_BOT_STRICT_WINDOW:
        recent_bot_joins.pop((guild_id, user_id), None)
        return False
    return True


# ────────────────────────────────────────────────────────────────
# 2. Whitelist helper
# ────────────────────────────────────────────────────────────────

async def is_protected(member: discord.Member) -> bool:
    """True, если этого участника нельзя наказывать: владелец, сам бот, whitelist юзер/роль."""
    guild = member.guild
    if member.id == guild.owner_id:
        return True
    if member.id == member.guild.me.id:
        return True
    if member.bot and member.id == member.guild.me.id:
        return True
    if await db.is_user_whitelisted(guild.id, member.id):
        return True
    for role in member.roles:
        if await db.is_role_whitelisted(guild.id, role.id):
            return True
    return False


# ────────────────────────────────────────────────────────────────
# 3. Punishment — применяем меру к нарушителю
# ────────────────────────────────────────────────────────────────

async def strip_dangerous_roles(member: discord.Member, reason: str) -> list[str]:
    """Снимает с юзера все роли, дающие опасные права. Возвращает имена снятых ролей."""
    to_remove = []
    for role in member.roles:
        if role.is_default():
            continue
        perms = role.permissions
        if any(getattr(perms, p, False) for p in config.DANGEROUS_PERMISSIONS):
            to_remove.append(role)

    removed_names = []
    for role in to_remove:
        try:
            await member.remove_roles(role, reason=reason)
            removed_names.append(role.name)
        except discord.NotFound:
            # Роль уже была удалена (гонка с другим действием/ботом) — пропускаем её и идём дальше
            continue
        except discord.Forbidden:
            # Не хватает прав снять именно эту роль — пропускаем, но продолжаем с остальными
            continue
    return removed_names


async def apply_punishment(member: discord.Member, punishment: str, reason: str) -> str:
    """Применяет наказание. Возвращает текстовое описание того, что было сделано."""
    guild = member.guild
    try:
        if punishment == "ban":
            await guild.ban(member, reason=reason, delete_message_seconds=0)
            return "забанен"
        elif punishment == "kick":
            removed = await strip_dangerous_roles(member, reason)
            await guild.kick(member, reason=reason)
            return f"кикнут (роли сняты: {', '.join(removed) or '—'})"
        else:  # strip_roles (по умолчанию)
            removed = await strip_dangerous_roles(member, reason)
            return f"роли сняты: {', '.join(removed) or '—'}"
    except discord.Forbidden:
        return "⚠️ не хватило прав применить наказание"


# ────────────────────────────────────────────────────────────────
# 6. Logging helper
# ────────────────────────────────────────────────────────────────

async def send_alert(guild: discord.Guild, title: str, description: str,
                       user: discord.abc.User | None = None, severe: bool = False):
    cfg = await db.get_guild_config(guild.id)
    embed = discord.Embed(
        title=f"🛡️ {title}",
        description=description,
        color=config.EMBED_COLOR_ALERT if severe else config.EMBED_COLOR_WARN,
        timestamp=discord.utils.utcnow(),
    )
    if user:
        embed.set_author(name=f"{user} ({user.id})", icon_url=getattr(user, "display_avatar", None) and user.display_avatar.url)
    embed.set_footer(text=config.BOT_NAME)

    log_channel_id = cfg.get("log_channel_id")
    if log_channel_id:
        channel = guild.get_channel(log_channel_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass

    if severe and cfg.get("dm_owner_on_threat"):
        owner = guild.owner
        if owner:
            try:
                await owner.send(embed=embed)
            except discord.Forbidden:
                pass


# ────────────────────────────────────────────────────────────────
# 4. AntiNuke cog
# ────────────────────────────────────────────────────────────────

# Соответствие discord.AuditLogAction -> ключ лимита в config.ANTINUKE_LIMITS
ACTION_MAP = {
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.channel_create: "channel_create",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.role_create: "role_create",
    discord.AuditLogAction.ban: "member_ban",
    discord.AuditLogAction.kick: "member_kick",
    discord.AuditLogAction.webhook_create: "webhook_create",
    discord.AuditLogAction.role_update: "role_permission_update",
    discord.AuditLogAction.member_role_update: "member_role_update",
}


class AntiNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        """
        Главный слушатель. Discord.py 2.4+ шлёт это событие в реальном времени,
        без ручного поллинга audit_logs(). Именно тут ловим "кто что сделал".
        """
        guild = entry.guild
        cfg = await db.get_guild_config(guild.id)
        if not cfg.get("antinuke_enabled"):
            return

        actor = entry.user
        if actor is None or actor.bot and actor.id == self.bot.user.id:
            return

        member = guild.get_member(actor.id)
        if member and await is_protected(member):
            return

        # ── 1. Проверка выдачи опасных прав роли ────────────────
        if entry.action == discord.AuditLogAction.role_update:
            await self._check_dangerous_role_grant(entry, guild, actor)

        # ── 2. Проверка выдачи опасной роли участнику ────────────
        if entry.action == discord.AuditLogAction.member_role_update:
            await self._check_dangerous_role_assign(entry, guild, actor)

        # ── 3. Общий лимит действий по типу ──────────────────────
        limit_key = ACTION_MAP.get(entry.action)
        if limit_key:
            await self._check_rate_limit(guild, actor, limit_key)

    async def _check_rate_limit(self, guild: discord.Guild, actor: discord.abc.User, limit_key: str):
        max_actions, window = config.ANTINUKE_LIMITS[limit_key]

        # Бот, зашедший недавно, не получает обычный "запас попыток" —
        # это самая частая схема рейда: только что добавленный бот сразу
        # сносит каналы/роли/банит людей, пока хозяин ещё не понял, что происходит.
        if actor.bot and is_recently_joined_bot(guild.id, actor.id):
            max_actions = config.NEW_BOT_STRICT_LIMIT
            window = config.NEW_BOT_STRICT_WINDOW

        count = rate_tracker.hit(guild.id, actor.id, limit_key, window)
        if count > max_actions:
            member = guild.get_member(actor.id)
            if member is None:
                return
            cfg = await db.get_guild_config(guild.id)
            punishment = cfg.get("antinuke_punishment", "strip_roles")
            # Для свежего бота снятие ролей недостаточно — он продолжит пытаться
            # действовать без прав. Банить сразу, вне зависимости от настройки.
            if actor.bot and is_recently_joined_bot(guild.id, actor.id):
                punishment = "ban"
            reason = f"Aegis: превышен лимит действий ({limit_key}: {count}/{window}с)"
            result = await apply_punishment(member, punishment, reason)
            rate_tracker.reset(guild.id, actor.id, limit_key)
            await db.log_incident(guild.id, actor.id, limit_key, reason, result)
            await send_alert(
                guild,
                "Обнаружена подозрительная активность",
                f"**Пользователь:** {actor.mention}\n**Действие:** `{limit_key}`\n"
                f"**Сработало:** {count} раз за {window} сек.\n**Реакция:** {result}",
                user=actor,
                severe=True,
            )

    async def _check_dangerous_role_grant(self, entry: discord.AuditLogEntry, guild: discord.Guild, actor: discord.abc.User):
        before, after = entry.before, entry.after
        before_perms = getattr(before, "permissions", None)
        after_perms = getattr(after, "permissions", None)
        if after_perms is None:
            return
        newly_dangerous = [
            p for p in config.DANGEROUS_PERMISSIONS
            if getattr(after_perms, p, False) and not (before_perms and getattr(before_perms, p, False))
        ]
        if not newly_dangerous:
            return
        role = entry.target
        member = guild.get_member(actor.id)
        if member is None:
            return
        cfg = await db.get_guild_config(guild.id)
        reason = f"Aegis: выдача опасных прав роли {getattr(role, 'name', role)} ({', '.join(newly_dangerous)})"
        # откатываем права роли обратно
        try:
            if before_perms:
                await role.edit(permissions=before_perms, reason="Aegis: откат опасных прав")
        except (discord.Forbidden, AttributeError):
            pass
        result = await apply_punishment(member, cfg.get("antinuke_punishment", "strip_roles"), reason)
        await db.log_incident(guild.id, actor.id, "role_permission_update", reason, result)
        await send_alert(
            guild, "Попытка выдать опасные права роли",
            f"**Пользователь:** {actor.mention}\n**Роль:** {getattr(role, 'mention', role)}\n"
            f"**Права:** {', '.join(newly_dangerous)}\n**Реакция:** права роли откачены, {result}",
            user=actor, severe=True,
        )

    async def _check_dangerous_role_assign(self, entry: discord.AuditLogEntry, guild: discord.Guild, actor: discord.abc.User):
        target_member = entry.target if isinstance(entry.target, discord.Member) else guild.get_member(entry.target.id)
        if target_member is None:
            return
        before_roles = set(getattr(entry.before, "roles", []) or [])
        after_roles = set(getattr(entry.after, "roles", []) or [])
        added_roles = after_roles - before_roles
        dangerous_added = [
            r for r in added_roles
            if any(getattr(r.permissions, p, False) for p in config.DANGEROUS_PERMISSIONS)
        ]
        if not dangerous_added:
            return
        member = guild.get_member(actor.id)
        if member is None:
            return
        cfg = await db.get_guild_config(guild.id)
        reason = f"Aegis: выдача опасной роли пользователю {target_member} ({', '.join(r.name for r in dangerous_added)})"
        for role in dangerous_added:
            try:
                await target_member.remove_roles(role, reason="Aegis: откат выдачи опасной роли")
            except (discord.NotFound, discord.Forbidden):
                continue
        result = await apply_punishment(member, cfg.get("antinuke_punishment", "strip_roles"), reason)
        await db.log_incident(guild.id, actor.id, "member_role_update", reason, result)
        await send_alert(
            guild, "Попытка выдать опасную роль",
            f"**Пользователь:** {actor.mention}\n**Кому выдано:** {target_member.mention}\n"
            f"**Роли:** {', '.join(r.name for r in dangerous_added)}\n**Реакция:** роли откачены, {result}",
            user=actor, severe=True,
        )


# ────────────────────────────────────────────────────────────────
# 5. AntiSpam cog
# ────────────────────────────────────────────────────────────────

class AntiSpam(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_messages: dict[tuple, deque] = defaultdict(deque)  # (guild,user) -> deque[(ts, content)]
        self._mass_mentions: dict[tuple, deque] = defaultdict(deque)  # (guild,user) -> deque[timestamps]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        guild = message.guild
        member = message.author
        if await is_protected(member):
            return
        cfg = await db.get_guild_config(guild.id)
        if not cfg.get("antispam_enabled"):
            return

        # ── частые сообщения (флуд) ───────────────────────────
        key = (guild.id, member.id)
        now = time.monotonic()
        dq = self._last_messages[key]
        dq.append((now, message.content))
        while dq and now - dq[0][0] > config.ANTISPAM_MESSAGE_WINDOW:
            dq.popleft()

        if len(dq) > config.ANTISPAM_MESSAGE_LIMIT:
            await self._punish_spam(message, "флуд сообщениями", f"{len(dq)} сообщений за {config.ANTISPAM_MESSAGE_WINDOW}с")
            self._last_messages.pop(key, None)
            return

        # ── повторяющиеся сообщения подряд ────────────────────
        if len(dq) >= config.ANTISPAM_DUPLICATE_LIMIT:
            last_n = [c for _, c in list(dq)[-config.ANTISPAM_DUPLICATE_LIMIT:]]
            if len(set(last_n)) == 1 and last_n[0].strip():
                await self._punish_spam(message, "дублирующиеся сообщения", f"{config.ANTISPAM_DUPLICATE_LIMIT}x одинаковых подряд")
                self._last_messages.pop(key, None)
                return

        # ── массовые упоминания в одном сообщении ─────────────
        mention_count = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            mention_count += 1
        if mention_count >= config.ANTISPAM_MENTION_LIMIT:
            await self._punish_spam(message, "массовые упоминания", f"{mention_count} упоминаний в одном сообщении")
            return

        # ── упоминания суммарно за окно ────────────────────────
        if mention_count > 0:
            mdq = self._mass_mentions[key]
            for _ in range(mention_count):
                mdq.append(now)
            while mdq and now - mdq[0] > config.ANTISPAM_MESSAGE_WINDOW:
                mdq.popleft()
            if len(mdq) >= config.ANTISPAM_MASS_MENTION_LIMIT:
                await self._punish_spam(message, "массовый пинг", f"{len(mdq)} упоминаний за {config.ANTISPAM_MESSAGE_WINDOW}с")
                self._mass_mentions.pop(key, None)
                return

        # ── ссылки-приглашения на другие сервера ──────────────
        if re.search(config.INVITE_REGEX, message.content, re.IGNORECASE):
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            await send_alert(
                guild, "Удалено приглашение на сторонний сервер",
                f"**Пользователь:** {member.mention}\n**Канал:** {message.channel.mention}",
                user=member,
            )

    async def _punish_spam(self, message: discord.Message, reason_type: str, detail: str):
        guild = message.guild
        member = message.author
        cfg = await db.get_guild_config(guild.id)
        punishment = cfg.get("spam_punishment", "mute")
        reason = f"Aegis: {reason_type} ({detail})"

        try:
            if punishment == "ban":
                await guild.ban(member, reason=reason, delete_message_seconds=300)
                result = "забанен"
            elif punishment == "kick":
                await guild.kick(member, reason=reason)
                result = "кикнут"
            else:
                until = discord.utils.utcnow() + timedelta(minutes=config.DEFAULT_MUTE_DURATION_MIN)
                await member.timeout(until, reason=reason)
                result = f"мут на {config.DEFAULT_MUTE_DURATION_MIN} мин."
        except discord.Forbidden:
            result = "⚠️ не хватило прав применить наказание"


# ────────────────────────────────────────────────────────────────
# 7. Backup / Restore
# ────────────────────────────────────────────────────────────────

def _serialize_overwrites(channel: discord.abc.GuildChannel) -> list[dict]:
    result = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "target_type": "role" if isinstance(target, discord.Role) else "member",
            "target_id": target.id,
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


async def create_backup_snapshot(guild: discord.Guild) -> dict:
    """Строит JSON-снимок ролей, каналов и их прав. Не бэкапит сообщения/участников."""
    roles = []
    for role in guild.roles:
        if role.is_default():
            continue
        roles.append({
            "id": role.id,
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "permissions": role.permissions.value,
            "position": role.position,
        })

    channels = []
    for channel in guild.channels:
        entry = {
            "id": channel.id,
            "name": channel.name,
            "type": str(channel.type),
            "position": channel.position,
            "category_id": channel.category_id,
            "overwrites": _serialize_overwrites(channel),
        }
        if isinstance(channel, discord.TextChannel):
            entry["topic"] = channel.topic
            entry["nsfw"] = channel.nsfw
            entry["slowmode_delay"] = channel.slowmode_delay
        channels.append(entry)

    return {
        "guild_name": guild.name,
        "guild_icon": str(guild.icon.url) if guild.icon else None,
        "roles": roles,
        "channels": channels,
    }


async def restore_from_snapshot(guild: discord.Guild, snapshot: dict, requester: discord.abc.User) -> str:
    """
    Восстанавливает роли и каналы из снимка. Не удаляет существующие лишние
    каналы/роли автоматически (это может задеть то, что появилось легитимно
    после бэкапа) — только создаёт недостающее и чинит права по имени.
    Возвращает текстовый отчёт.
    """
    created_roles, created_channels = 0, 0
    existing_role_names = {r.name: r for r in guild.roles}

    # ── роли (от низшей позиции к высшей, чтобы порядок примерно совпал) ──
    for role_data in sorted(snapshot["roles"], key=lambda r: r["position"]):
        if role_data["name"] in existing_role_names:
            continue
        try:
            await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                permissions=discord.Permissions(role_data["permissions"]),
                reason=f"Aegis: восстановление из бэкапа (запросил {requester})",
            )
            created_roles += 1
        except discord.Forbidden:
            pass

    # ── каналы (только создаём отсутствующие по имени+типу, права не трогаем) ──
    existing_channel_names = {(c.name, str(c.type)) for c in guild.channels}
    for ch_data in sorted(snapshot["channels"], key=lambda c: c["position"]):
        key = (ch_data["name"], ch_data["type"])
        if key in existing_channel_names:
            continue
        try:
            if ch_data["type"] == "category":
                await guild.create_category(ch_data["name"], reason="Aegis: восстановление из бэкапа")
            elif ch_data["type"] == "text":
                await guild.create_text_channel(
                    ch_data["name"],
                    topic=ch_data.get("topic"),
                    nsfw=ch_data.get("nsfw", False),
                    slowmode_delay=ch_data.get("slowmode_delay", 0),
                    reason="Aegis: восстановление из бэкапа",
                )
            elif ch_data["type"] == "voice":
                await guild.create_voice_channel(ch_data["name"], reason="Aegis: восстановление из бэкапа")
            else:
                continue
            created_channels += 1
        except discord.Forbidden:
            pass

    return f"Восстановлено: {created_roles} ролей, {created_channels} каналов (только отсутствовавшие)."


# ────────────────────────────────────────────────────────────────
# 8. Panic mode (Lockdown)
# ────────────────────────────────────────────────────────────────

async def enable_panic_mode(guild: discord.Guild, requester: discord.abc.User) -> str:
    """
    Закрывает все текстовые каналы на запись для @everyone и снимает опасные
    права у всех ролей, кроме владельца/бота/whitelist. Сохраняет исходное
    состояние в settings_json, чтобы можно было откатить через disable_panic_mode.
    """
    cfg = await db.get_guild_config(guild.id)
    if cfg.get("panic_mode"):
        return "Режим паники уже активен."

    saved_overwrites = {}
    locked = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            saved_overwrites[str(channel.id)] = overwrite.send_messages
            new_overwrite = overwrite
            new_overwrite.send_messages = False
            await channel.set_permissions(guild.default_role, overwrite=new_overwrite,
                                            reason=f"Aegis: panic mode ({requester})")
            locked += 1
        except discord.Forbidden:
            continue

    stripped_roles = 0
    for role in guild.roles:
        if role.is_default() or role >= guild.me.top_role:
            continue
        if await db.is_role_whitelisted(guild.id, role.id):
            continue
        if any(getattr(role.permissions, p, False) for p in config.DANGEROUS_PERMISSIONS):
            try:
                safe_perms = discord.Permissions(role.permissions.value)
                for p in config.DANGEROUS_PERMISSIONS:
                    setattr(safe_perms, p, False)
                await role.edit(permissions=safe_perms, reason="Aegis: panic mode — снятие опасных прав")
                stripped_roles += 1
            except discord.Forbidden:
                continue

    await db.set_guild_config(
        guild.id, panic_mode=1,
        settings_json=json.dumps({"panic_channel_overwrites": saved_overwrites}),
    )
    return f"🔒 Lockdown активирован: закрыто каналов — {locked}, роли обезврежены — {stripped_roles}."


async def disable_panic_mode(guild: discord.Guild, requester: discord.abc.User) -> str:
    cfg = await db.get_guild_config(guild.id)
    if not cfg.get("panic_mode"):
        return "Режим паники не активен."

    saved = json.loads(cfg.get("settings_json") or "{}").get("panic_channel_overwrites", {})
    restored = 0
    for channel_id, send_messages_value in saved.items():
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            continue
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = send_messages_value
            await channel.set_permissions(guild.default_role, overwrite=overwrite,
                                            reason=f"Aegis: снятие panic mode ({requester})")
            restored += 1
        except discord.Forbidden:
            continue

    await db.set_guild_config(guild.id, panic_mode=0, settings_json="{}")
    return (f"🔓 Lockdown снят: восстановлено каналов — {restored}. "
            f"⚠️ Права ролей, снятые во время паники, нужно вернуть вручную "
            f"(они не восстанавливаются автоматически, чтобы не повторить ошибку, из-за которой включили панику).")


# ────────────────────────────────────────────────────────────────
# 9. RaidJoin cog — защита от массового входа
# ────────────────────────────────────────────────────────────────

class RaidJoin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._join_times: dict[int, deque] = defaultdict(deque)  # guild_id -> deque[timestamps]
        self._raid_mode_until: dict[int, float] = {}              # guild_id -> monotonic() до которого действует режим

    async def _get_or_create_quarantine_role(self, guild: discord.Guild) -> discord.Role | None:
        role = discord.utils.get(guild.roles, name=config.QUARANTINE_ROLE_NAME)
        if role:
            return role
        try:
            role = await guild.create_role(
                name=config.QUARANTINE_ROLE_NAME,
                permissions=discord.Permissions.none(),
                reason="Aegis: роль карантина",
            )
            # запрещаем писать/говорить во всех каналах для роли карантина
            for channel in guild.channels:
                try:
                    await channel.set_permissions(
                        role, send_messages=False, speak=False, add_reactions=False,
                        reason="Aegis: настройка роли карантина",
                    )
                except discord.Forbidden:
                    continue
            return role
        except discord.Forbidden:
            return None

    async def _handle_bot_join(self, member: discord.Member):
        """
        Реакция на вход БОТА (не человека). Владелец сам добавляет ботов вручную,
        так что "кто пригласил" ничего не значит — вредоносный бот тоже
        приглашается владельцем, просто он не знает, что бот вредоносный.
        Поэтому здесь мы не блокируем сразу, а берём бота "под колпак":
        регистрируем время его входа в recent_bot_joins, и AntiNuke будет
        применять к нему резко ужесточённый лимит действий (см. ACTION_MAP
        и _check_rate_limit — для свежих ботов лимит "1 опасное действие =
        мгновенное наказание" вместо обычных 3-5 за 10 секунд).
        """
        if await is_protected(member):
            return
        recent_bot_joins[(member.guild.id, member.id)] = time.monotonic()
        await send_alert(
            member.guild, "🤖 Новый бот на сервере — под усиленным наблюдением",
            f"**Бот:** {member.mention} ({member.id})\n"
            f"В течение {config.NEW_BOT_STRICT_WINDOW} сек. любое опасное действие "
            f"этого бота вызовет немедленную реакцию без обычного лимита попыток.",
            user=member,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = await db.get_guild_config(guild.id)
        if not cfg.get("raidjoin_enabled"):
            return

        # ── боты берутся под усиленное наблюдение сразу при входе ──
        if member.bot:
            await self._handle_bot_join(member)
            return

        now = time.monotonic()
        dq = self._join_times[guild.id]
        dq.append(now)
        while dq and now - dq[0] > config.RAID_JOIN_WINDOW:
            dq.popleft()

        account_age = discord.utils.utcnow() - member.created_at
        is_new_account = account_age < timedelta(hours=config.MIN_ACCOUNT_AGE_HOURS)
        raid_suspected = len(dq) >= config.RAID_JOIN_LIMIT

        if raid_suspected:
            already_alerted = guild.id in self._raid_mode_until and now < self._raid_mode_until[guild.id]
            self._raid_mode_until[guild.id] = now + config.RAID_JOIN_WINDOW
            if not already_alerted:
                await send_alert(
                    guild, "⚠️ Подозрение на рейд входом",
                    f"**{len(dq)} участников** вступили за последние {config.RAID_JOIN_WINDOW} сек.\n"
                    f"Новые участники временно отправляются в карантин.",
                    severe=True,
                )

        currently_in_raid_window = guild.id in self._raid_mode_until and now < self._raid_mode_until[guild.id]

        if raid_suspected or (currently_in_raid_window and is_new_account):
            role = await self._get_or_create_quarantine_role(guild)
            if role:
                try:
                    await member.add_roles(role, reason="Aegis: карантин (подозрение на рейд / новый аккаунт)")
                    await db.log_incident(
                        guild.id, member.id, "raid_join_quarantine",
                        f"Аккаунту {account_age.days} дн., вступлений за окно: {len(dq)}",
                        "отправлен в карантин",
                    )
                except discord.Forbidden:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))
    await bot.add_cog(AntiSpam(bot))
    await bot.add_cog(RaidJoin(bot))
