"""
Aegis | Server Guardian — ручная модерация.

В отличие от security.py (автоматическая защита от рейдов/нюков),
этот модуль — классические команды модератора: варны, мут, кик, бан,
очистка сообщений. Варны хранятся в БД и умеют автоматически
эскалировать наказание (см. config.WARN_ESCALATION).
"""

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db


def mod_only():
    """Команду может использовать тот, у кого есть moderate_members (тайм-аут/кик/бан)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        if perms.moderate_members or perms.manage_guild or interaction.user.id == interaction.guild.owner_id:
            return True
        await interaction.response.send_message("⛔ Нужны права модератора.", ephemeral=True)
        return False
    return app_commands.check(predicate)


async def _cannot_target(interaction: discord.Interaction, target: discord.Member) -> bool:
    """Общие проверки: нельзя трогать себя, владельца, бота, или того, кто выше/равен по роли."""
    if target.id == interaction.user.id:
        await interaction.response.send_message("⛔ Нельзя применить это к самому себе.", ephemeral=True)
        return True
    if target.id == interaction.guild.owner_id:
        await interaction.response.send_message("⛔ Нельзя применить это к владельцу сервера.", ephemeral=True)
        return True
    if target.id == interaction.client.user.id:
        await interaction.response.send_message("⛔ Это я.", ephemeral=True)
        return True
    if isinstance(interaction.user, discord.Member) and target.top_role >= interaction.user.top_role \
            and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("⛔ Роль этого участника выше или равна вашей.", ephemeral=True)
        return True
    return False


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /warn ────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Выдать предупреждение участнику")
    @app_commands.describe(user="Кому выдать варн", reason="Причина")
    @mod_only()
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
        if await _cannot_target(interaction, user):
            return

        warning_id = await db.add_warning(interaction.guild.id, user.id, interaction.user.id, reason)
        warnings = await db.get_warnings(interaction.guild.id, user.id)
        count = len(warnings)

        embed = discord.Embed(
            title="⚠️ Выдано предупреждение",
            color=config.EMBED_COLOR_WARN,
            description=f"**Участник:** {user.mention}\n**Причина:** {reason}\n"
                        f"**Всего варнов:** {count}\n**ID варна:** {warning_id}",
        )
        await interaction.response.send_message(embed=embed)

        try:
            await user.send(
                f"⚠️ Вам выдано предупреждение на сервере **{interaction.guild.name}**.\n"
                f"Причина: {reason}\nВсего предупреждений: {count}"
            )
        except discord.Forbidden:
            pass

        # ── авто-эскалация ──────────────────────────────────────
        punishment = config.WARN_ESCALATION.get(count)
        if punishment:
            esc_reason = f"Aegis: авто-эскалация — {count} предупреждений"
            try:
                if punishment == "ban":
                    await interaction.guild.ban(user, reason=esc_reason)
                    result = "забанен (авто)"
                elif punishment == "kick":
                    await interaction.guild.kick(user, reason=esc_reason)
                    result = "кикнут (авто)"
                else:  # mute
                    until = discord.utils.utcnow() + timedelta(minutes=config.WARN_MUTE_DURATION_MIN)
                    await user.timeout(until, reason=esc_reason)
                    result = f"мут на {config.WARN_MUTE_DURATION_MIN} мин. (авто)"
                await interaction.followup.send(
                    f"🚨 Достигнут порог в **{count}** варнов — {user.mention}: {result}."
                )
                await db.log_incident(interaction.guild.id, user.id, "warn_escalation", esc_reason, result)
            except discord.Forbidden:
                await interaction.followup.send("⚠️ Порог варнов достигнут, но не хватило прав применить наказание.")

    @app_commands.command(name="warnings", description="Показать предупреждения участника")
    @mod_only()
    async def warnings_list(self, interaction: discord.Interaction, user: discord.Member):
        warnings = await db.get_warnings(interaction.guild.id, user.id)
        if not warnings:
            await interaction.response.send_message(f"У {user.mention} нет предупреждений. ✅", ephemeral=True)
            return
        embed = discord.Embed(title=f"⚠️ Предупреждения — {user}", color=config.EMBED_COLOR_WARN)
        for w in warnings[:15]:
            embed.add_field(
                name=f"ID {w['id']} — <t:{w['created_at']}:R>",
                value=f"От <@{w['moderator_id']}>: {w['reason']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="unwarn", description="Удалить конкретное предупреждение по ID")
    @mod_only()
    async def unwarn(self, interaction: discord.Interaction, warning_id: int):
        ok = await db.remove_warning(warning_id, interaction.guild.id)
        if ok:
            await interaction.response.send_message(f"✅ Варн ID {warning_id} удалён.", ephemeral=True)
        else:
            await interaction.response.send_message("⛔ Варн с таким ID не найден на этом сервере.", ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Очистить все предупреждения участника")
    @mod_only()
    async def clear_warnings(self, interaction: discord.Interaction, user: discord.Member):
        count = await db.clear_warnings(interaction.guild.id, user.id)
        await interaction.response.send_message(f"✅ Удалено варнов: {count} (у {user.mention}).", ephemeral=True)

    # ── /mute /unmute ────────────────────────────────────────────

    @app_commands.command(name="mute", description="Дать тайм-аут участнику")
    @app_commands.describe(minutes="На сколько минут", reason="Причина")
    @mod_only()
    async def mute(self, interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "Не указана"):
        if await _cannot_target(interaction, user):
            return
        if minutes <= 0 or minutes > 40320:  # discord максимум ~28 дней
            await interaction.response.send_message("⛔ Длительность от 1 минуты до 28 дней.", ephemeral=True)
            return
        try:
            until = discord.utils.utcnow() + timedelta(minutes=minutes)
            await user.timeout(until, reason=f"Aegis: {reason} (модератор: {interaction.user})")
        except discord.Forbidden:
            await interaction.response.send_message("⛔ Не хватает прав.", ephemeral=True)
            return
        await interaction.response.send_message(f"🔇 {user.mention} получил тайм-аут на {minutes} мин. Причина: {reason}")

    @app_commands.command(name="unmute", description="Снять тайм-аут с участника")
    @mod_only()
    async def unmute(self, interaction: discord.Interaction, user: discord.Member):
        try:
            await user.timeout(None, reason=f"Aegis: снят модератором {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("⛔ Не хватает прав.", ephemeral=True)
            return
        await interaction.response.send_message(f"🔊 Тайм-аут снят с {user.mention}.")

    # ── /kick /ban /unban ────────────────────────────────────────

    @app_commands.command(name="kick", description="Кикнуть участника")
    @mod_only()
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
        if await _cannot_target(interaction, user):
            return
        try:
            await user.kick(reason=f"Aegis: {reason} (модератор: {interaction.user})")
        except discord.Forbidden:
            await interaction.response.send_message("⛔ Не хватает прав.", ephemeral=True)
            return
        await interaction.response.send_message(f"👢 {user} кикнут. Причина: {reason}")

    @app_commands.command(name="ban", description="Забанить участника")
    @app_commands.describe(delete_days="Удалить сообщения за последние N дней (0-7)")
    @mod_only()
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана", delete_days: int = 0):
        if await _cannot_target(interaction, user):
            return
        delete_days = max(0, min(7, delete_days))
        try:
            await user.ban(reason=f"Aegis: {reason} (модератор: {interaction.user})",
                           delete_message_seconds=delete_days * 86400)
        except discord.Forbidden:
            await interaction.response.send_message("⛔ Не хватает прав.", ephemeral=True)
            return
        await interaction.response.send_message(f"🔨 {user} забанен. Причина: {reason}")

    @app_commands.command(name="unban", description="Разбанить пользователя по ID")
    @mod_only()
    async def unban(self, interaction: discord.Interaction, user_id: str):
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message("⛔ ID должен быть числом.", ephemeral=True)
            return
        try:
            user = discord.Object(id=uid)
            await interaction.guild.unban(user, reason=f"Aegis: разбанен модератором {interaction.user}")
        except discord.NotFound:
            await interaction.response.send_message("⛔ Этот пользователь не забанен.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message("⛔ Не хватает прав.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Пользователь `{uid}` разбанен.")

    # ── /purge ───────────────────────────────────────────────────

    @app_commands.command(name="purge", description="Удалить последние N сообщений в канале")
    @app_commands.describe(amount="Сколько сообщений удалить (1-100)", user="Удалить только сообщения этого пользователя")
    @mod_only()
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], user: discord.Member = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        check = (lambda m: m.author.id == user.id) if user else None
        deleted = await interaction.channel.purge(limit=amount, check=check)
        await interaction.followup.send(f"🧹 Удалено сообщений: {len(deleted)}", ephemeral=True)

    # ── /slowmode ────────────────────────────────────────────────

    @app_commands.command(name="slowmode", description="Установить медленный режим в канале")
    @app_commands.describe(seconds="Задержка в секундах (0 — выключить)")
    @mod_only()
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        try:
            await interaction.channel.edit(slowmode_delay=seconds)
        except discord.Forbidden:
            await interaction.response.send_message("⛔ Не хватает прав.", ephemeral=True)
            return
        if seconds == 0:
            await interaction.response.send_message("✅ Медленный режим выключен.")
        else:
            await interaction.response.send_message(f"🐌 Медленный режим: {seconds} сек.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
