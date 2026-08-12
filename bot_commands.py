"""
Aegis | Server Guardian — slash-команды управления ботом.
"""

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import security


def admin_only():
    """Декоратор: команду может использовать только тот, у кого есть manage_guild."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild or interaction.user.id == interaction.guild.owner_id:
            return True
        await interaction.response.send_message("⛔ Нужны права **Управление сервером**.", ephemeral=True)
        return False
    return app_commands.check(predicate)


class AegisCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── группа /whitelist ──────────────────────────────────────
    whitelist_group = app_commands.Group(name="whitelist", description="Управление белым списком Aegis")

    @whitelist_group.command(name="add_user", description="Добавить пользователя в белый список")
    @admin_only()
    async def whitelist_add_user(self, interaction: discord.Interaction, user: discord.Member):
        await db.add_whitelist_user(interaction.guild.id, user.id, interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} добавлен в белый список.", ephemeral=True)

    @whitelist_group.command(name="remove_user", description="Убрать пользователя из белого списка")
    @admin_only()
    async def whitelist_remove_user(self, interaction: discord.Interaction, user: discord.Member):
        await db.remove_whitelist_user(interaction.guild.id, user.id)
        await interaction.response.send_message(f"✅ {user.mention} убран из белого списка.", ephemeral=True)

    @whitelist_group.command(name="add_role", description="Добавить роль в белый список")
    @admin_only()
    async def whitelist_add_role(self, interaction: discord.Interaction, role: discord.Role):
        await db.add_whitelist_role(interaction.guild.id, role.id, interaction.user.id)
        await interaction.response.send_message(f"✅ Роль {role.mention} добавлена в белый список.", ephemeral=True)

    @whitelist_group.command(name="remove_role", description="Убрать роль из белого списка")
    @admin_only()
    async def whitelist_remove_role(self, interaction: discord.Interaction, role: discord.Role):
        await db.remove_whitelist_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"✅ Роль {role.mention} убрана из белого списка.", ephemeral=True)

    @whitelist_group.command(name="list", description="Показать белый список сервера")
    @admin_only()
    async def whitelist_list(self, interaction: discord.Interaction):
        guild = interaction.guild
        user_ids = await db.list_whitelist_users(guild.id)
        role_ids = await db.list_whitelist_roles(guild.id)
        users_txt = "\n".join(f"• <@{uid}>" for uid in user_ids) or "—"
        roles_txt = "\n".join(f"• <@&{rid}>" for rid in role_ids) or "—"
        embed = discord.Embed(title="🛡️ Белый список", color=config.EMBED_COLOR)
        embed.add_field(name="Пользователи", value=users_txt, inline=False)
        embed.add_field(name="Роли", value=roles_txt, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── настройки ────────────────────────────────────────────────

    @app_commands.command(name="setlogchannel", description="Установить канал для логов защиты Aegis")
    @admin_only()
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.set_guild_config(interaction.guild.id, log_channel_id=channel.id)
        await interaction.response.send_message(f"✅ Канал логов установлен: {channel.mention}", ephemeral=True)

    @app_commands.command(name="config", description="Показать текущие настройки Aegis")
    @admin_only()
    async def show_config(self, interaction: discord.Interaction):
        cfg = await db.get_guild_config(interaction.guild.id)
        embed = discord.Embed(title="🛡️ Настройки Aegis", color=config.EMBED_COLOR)
        embed.add_field(name="Антинюк", value="🟢 вкл" if cfg["antinuke_enabled"] else "🔴 выкл")
        embed.add_field(name="Антиспам", value="🟢 вкл" if cfg["antispam_enabled"] else "🔴 выкл")
        embed.add_field(name="Защита от рейда входом", value="🟢 вкл" if cfg["raidjoin_enabled"] else "🔴 выкл")
        embed.add_field(name="Режим паники", value="🔴 АКТИВЕН" if cfg["panic_mode"] else "🟢 выкл")
        embed.add_field(name="Наказание (антинюк)", value=cfg["antinuke_punishment"])
        embed.add_field(name="Наказание (антиспам)", value=cfg["spam_punishment"])
        log_ch = f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "не установлен"
        embed.add_field(name="Канал логов", value=log_ch, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="toggle", description="Включить/выключить модуль защиты")
    @app_commands.choices(module=[
        app_commands.Choice(name="Антинюк", value="antinuke_enabled"),
        app_commands.Choice(name="Антиспам", value="antispam_enabled"),
        app_commands.Choice(name="Защита от входа", value="raidjoin_enabled"),
    ])
    @admin_only()
    async def toggle_module(self, interaction: discord.Interaction, module: app_commands.Choice[str]):
        cfg = await db.get_guild_config(interaction.guild.id)
        new_value = 0 if cfg[module.value] else 1
        await db.set_guild_config(interaction.guild.id, **{module.value: new_value})
        state = "включён 🟢" if new_value else "выключен 🔴"
        await interaction.response.send_message(f"✅ Модуль **{module.name}** теперь {state}", ephemeral=True)

    @app_commands.command(name="punishment", description="Настроить реакцию на нарушение")
    @app_commands.choices(
        module=[
            app_commands.Choice(name="Антинюк", value="antinuke_punishment"),
            app_commands.Choice(name="Антиспам", value="spam_punishment"),
        ],
        action=[
            app_commands.Choice(name="Снять роли", value="strip_roles"),
            app_commands.Choice(name="Кик", value="kick"),
            app_commands.Choice(name="Бан", value="ban"),
            app_commands.Choice(name="Мут (только антиспам)", value="mute"),
        ],
    )
    @admin_only()
    async def set_punishment(self, interaction: discord.Interaction, module: app_commands.Choice[str], action: app_commands.Choice[str]):
        await db.set_guild_config(interaction.guild.id, **{module.value: action.value})
        await interaction.response.send_message(f"✅ {module.name}: реакция установлена на **{action.name}**", ephemeral=True)

    # ── логи и статистика ──────────────────────────────────────

    @app_commands.command(name="incidents", description="Показать последние инциденты")
    @admin_only()
    async def show_incidents(self, interaction: discord.Interaction):
        incidents = await db.get_recent_incidents(interaction.guild.id, limit=10)
        if not incidents:
            await interaction.response.send_message("Инцидентов пока не зафиксировано. ✅", ephemeral=True)
            return
        embed = discord.Embed(title="🛡️ Последние инциденты", color=config.EMBED_COLOR_WARN)
        for inc in incidents:
            ts = f"<t:{inc['created_at']}:R>"
            embed.add_field(
                name=f"{inc['action_type']} — {ts}",
                value=f"<@{inc['user_id']}> → {inc['punishment']}\n{inc['details'][:200]}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stats", description="Статистика срабатываний защиты")
    @admin_only()
    async def show_stats(self, interaction: discord.Interaction):
        stats = await db.get_stats(interaction.guild.id)
        if not stats:
            await interaction.response.send_message("Пока нет статистики. ✅", ephemeral=True)
            return
        embed = discord.Embed(title="📊 Статистика Aegis", color=config.EMBED_COLOR)
        for metric, count in sorted(stats.items(), key=lambda x: -x[1]):
            embed.add_field(name=metric, value=str(count), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # ── бэкап / восстановление ──────────────────────────────────

    @app_commands.command(name="backup_create", description="Создать бэкап ролей и каналов сервера")
    @admin_only()
    async def backup_create(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = await security.create_backup_snapshot(interaction.guild)
        backup_id = await db.save_backup(interaction.guild.id, interaction.user.id, snapshot)
        await interaction.followup.send(
            f"✅ Бэкап создан. ID: **{backup_id}** "
            f"({len(snapshot['roles'])} ролей, {len(snapshot['channels'])} каналов).\n"
            f"Используй `/restore backup_id:{backup_id}` для восстановления.",
            ephemeral=True,
        )

    @app_commands.command(name="backup_list", description="Показать список сохранённых бэкапов")
    @admin_only()
    async def backup_list(self, interaction: discord.Interaction):
        backups = await db.list_backups(interaction.guild.id)
        if not backups:
            await interaction.response.send_message("Бэкапов пока нет. Создай через `/backup_create`.", ephemeral=True)
            return
        embed = discord.Embed(title="🛡️ Точки восстановления", color=config.EMBED_COLOR)
        for b in backups:
            embed.add_field(
                name=f"ID {b['id']}",
                value=f"Создан <t:{b['created_at']}:R> пользователем <@{b['created_by']}>",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="restore", description="Восстановить сервер из бэкапа (создаёт недостающие роли/каналы)")
    @admin_only()
    async def restore(self, interaction: discord.Interaction, backup_id: int):
        backup = await db.get_backup(backup_id, interaction.guild.id)
        if backup is None:
            await interaction.response.send_message("⛔ Бэкап с таким ID не найден на этом сервере.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await security.restore_from_snapshot(interaction.guild, backup["data"], interaction.user)
        await interaction.followup.send(f"✅ {report}", ephemeral=True)

    # ── режим паники ─────────────────────────────────────────────

    @app_commands.command(name="panic", description="Включить/выключить экстренную блокировку сервера")
    @app_commands.choices(state=[
        app_commands.Choice(name="Включить (lockdown)", value="on"),
        app_commands.Choice(name="Выключить", value="off"),
    ])
    @admin_only()
    async def panic(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=False, thinking=True)
        if state.value == "on":
            result = await security.enable_panic_mode(interaction.guild, interaction.user)
        else:
            result = await security.disable_panic_mode(interaction.guild, interaction.user)
        await interaction.followup.send(result)


    # ── справка ──────────────────────────────────────────────────

    @app_commands.command(name="help", description="Показать список команд Aegis")
    async def help_command(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title=f"🛡️ {config.BOT_NAME} — команды",
            description="Антирейд / антинюк бот. Ниже — все доступные команды.",
            color=config.EMBED_COLOR,
        )
        embed.add_field(
            name="⚙️ Настройки",
            value=(
                "`/config` — показать текущие настройки\n"
                "`/setlogchannel` — канал для логов защиты\n"
                "`/toggle` — вкл/выкл модуль (антинюк / антиспам / защита входа)\n"
                "`/punishment` — настроить реакцию на нарушение"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 Белый список",
            value=(
                "`/whitelist add_user` `/whitelist remove_user`\n"
                "`/whitelist add_role` `/whitelist remove_role`\n"
                "`/whitelist list` — показать список"
            ),
            inline=False,
        )
        embed.add_field(
            name="💾 Бэкап / восстановление",
            value=(
                "`/backup_create` — снять бэкап ролей и каналов\n"
                "`/backup_list` — список сохранённых бэкапов\n"
                "`/restore` — восстановить недостающее из бэкапа"
            ),
            inline=False,
        )
        embed.add_field(
            name="🚨 Экстренное",
            value="`/panic` — включить/выключить lockdown сервера",
            inline=False,
        )
        embed.add_field(
            name="📊 Логи и статистика",
            value=(
                "`/incidents` — последние зафиксированные инциденты\n"
                "`/stats` — статистика срабатываний защиты"
            ),
            inline=False,
        )
        embed.set_footer(text="Команды настроек и модерации доступны только с правом «Управление сервером».")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AegisCommands(bot))
