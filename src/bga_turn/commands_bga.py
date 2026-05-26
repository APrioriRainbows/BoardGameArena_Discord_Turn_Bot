from __future__ import annotations

import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .bga_client import BgaClient, BgaClientError, BgaNotPublicError
from .database import Database
from .i18n import tr
from .models import NOTIFY_FINAL, NOTIFY_RECAP, NOTIFY_TURN
from .monitor import BgaMonitor, TurnDetailsView
from .utils import build_table_url, format_game_name, parse_public_table_url, parse_table_id


class PlayerLinkSelect(discord.ui.UserSelect):
    """One searchable Discord-member picker mapped to a single BGA player."""

    def __init__(self, *, bga_player_id: str, bga_player_name: str, database: Database, row: int) -> None:
        super().__init__(
            placeholder=f"{bga_player_name} (BGA) - pick their Discord account",
            min_values=0,
            max_values=1,
            row=row,
        )
        self.bga_player_id = bga_player_id
        self.bga_player_name = bga_player_name
        self.database = database

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            await interaction.response.defer()
            return
        discord_user = self.values[0]
        await asyncio.to_thread(
            self.database.upsert_linked_user,
            str(discord_user.id),
            self.bga_player_id,
            self.bga_player_name,
        )
        await interaction.response.send_message(
            f"Linked **{self.bga_player_name}** to {discord_user.mention}.",
            ephemeral=True,
        )


class LinkAllView(discord.ui.View):
    PER_PAGE = 4

    def __init__(
        self,
        *,
        bga_players: list[tuple[str, str]],
        database: Database,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=300)
        self.all_players = bga_players
        self.database = database
        self.page = page

        start = page * self.PER_PAGE
        for row_index, (bga_id, bga_name) in enumerate(bga_players[start : start + self.PER_PAGE]):
            self.add_item(PlayerLinkSelect(
                bga_player_id=bga_id,
                bga_player_name=bga_name,
                database=database,
                row=row_index,
            ))

        total_pages = (len(bga_players) + self.PER_PAGE - 1) // self.PER_PAGE
        if total_pages > 1:
            if page > 0:
                btn = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, row=4)
                btn.callback = self._prev_page
                self.add_item(btn)
            if page < total_pages - 1:
                btn = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, row=4)
                btn.callback = self._next_page
                self.add_item(btn)

    @staticmethod
    def page_header(page: int, total: int) -> str:
        per_page = LinkAllView.PER_PAGE
        start = page * per_page + 1
        end = min(start + per_page - 1, total)
        suffix = f" (showing {start}-{end} of {total})" if total > per_page else ""
        return f"**Link BGA players to Discord members{suffix}:**\nSelect a Discord account for each BGA player."

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        new_view = LinkAllView(bga_players=self.all_players, database=self.database, page=self.page - 1)
        await interaction.response.edit_message(
            content=self.page_header(self.page - 1, len(self.all_players)),
            view=new_view,
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        new_view = LinkAllView(bga_players=self.all_players, database=self.database, page=self.page + 1)
        await interaction.response.edit_message(
            content=self.page_header(self.page + 1, len(self.all_players)),
            view=new_view,
        )


class GameSettingsView(discord.ui.View):
    """Channel picker + notification toggles + save button. Used for both new and existing watches."""

    def __init__(
        self,
        *,
        table_id: str,
        table_url: str,
        base_url: str,
        gameserver: str,
        game_name: str,
        guild_id: str,
        default_channel_id: str,
        database: Database,
        monitor: BgaMonitor,
        bga_client: BgaClient,
        existing_subscription_id: int | None = None,
        existing_flags: int = 7,
    ) -> None:
        from .models import NOTIFY_ALL
        super().__init__(timeout=300)
        self.table_id = table_id
        self.table_url = table_url
        self.base_url = base_url
        self.gameserver = gameserver
        self.game_name = game_name
        self.guild_id = guild_id
        self.database = database
        self.monitor = monitor
        self.bga_client = bga_client
        self.existing_subscription_id = existing_subscription_id
        self._channel_id = default_channel_id
        self._notification_flags = existing_flags

        channel_select = discord.ui.ChannelSelect(
            placeholder="Post notifications in... (defaults to current channel)",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            min_values=0,
            max_values=1,
            row=0,
        )

        async def on_channel(interaction: discord.Interaction) -> None:
            if channel_select.values:
                self._channel_id = str(channel_select.values[0].id)
            await interaction.response.defer()

        channel_select.callback = on_channel
        self.add_item(channel_select)

        notif_select = discord.ui.Select(
            placeholder="Notification types (all on by default)",
            min_values=0,
            max_values=3,
            row=1,
            options=[
                discord.SelectOption(
                    label="Turn notifications",
                    value=str(NOTIFY_TURN),
                    description="⏳ Ping when it's your turn",
                    default=bool(existing_flags & NOTIFY_TURN),
                ),
                discord.SelectOption(
                    label="Turn recap",
                    value=str(NOTIFY_RECAP),
                    description="✅ Show who just completed their turn",
                    default=bool(existing_flags & NOTIFY_RECAP),
                ),
                discord.SelectOption(
                    label="Final results",
                    value=str(NOTIFY_FINAL),
                    description="🎉 Post scores when the game ends",
                    default=bool(existing_flags & NOTIFY_FINAL),
                ),
            ],
        )

        async def on_notif(interaction: discord.Interaction) -> None:
            self._notification_flags = sum(int(v) for v in notif_select.values)
            await interaction.response.defer()

        notif_select.callback = on_notif
        self.add_item(notif_select)

        save_label = "Save settings" if existing_subscription_id else "Start watching"
        save_btn = discord.ui.Button(
            label=save_label,
            style=discord.ButtonStyle.primary,
            row=2,
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

    async def _on_save(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        table_info = self.bga_client.build_public_table_info(
            table_id=self.table_id,
            table_url=self.table_url,
            base_url=self.base_url,
            gameserver=self.gameserver,
            game_name=self.game_name,
        )

        try:
            state = await self.bga_client.probe_public_table(table_info, known_player_names={})
        except BgaNotPublicError as exc:
            await interaction.followup.send(
                f"Table is not publicly accessible: {exc}", ephemeral=True
            )
            return
        except BgaClientError as exc:
            await interaction.followup.send(
                f"Could not verify table: {exc}", ephemeral=True
            )
            return

        if self.existing_subscription_id:
            await asyncio.to_thread(
                self.database.update_subscription_channel,
                self.existing_subscription_id,
                self._channel_id,
            )
            await asyncio.to_thread(
                self.database.set_notification_flags,
                self.existing_subscription_id,
                self._notification_flags,
            )
        else:
            subscription = await asyncio.to_thread(
                self.database.upsert_watch_subscription,
                table_id=self.table_id,
                table_url=self.table_url,
                base_url=self.base_url,
                gameserver=self.gameserver,
                guild_id=self.guild_id,
                channel_id=self._channel_id,
                created_by_discord_user_id=str(interaction.user.id),
                game_name=self.game_name,
            )
            persisted_names = dict(subscription.player_names)
            persisted_names.update(state.player_names)
            await asyncio.to_thread(
                self.database.update_watch_state,
                subscription_id=subscription.subscription_id,
                last_packet_id=subscription.last_packet_id,
                waiting_ids=subscription.last_waiting_ids,
                player_names=persisted_names,
                is_initialized=subscription.is_initialized,
                game_name=self.game_name,
            )
            await asyncio.to_thread(self.database.enrich_linked_users_from_players, persisted_names)
            await asyncio.to_thread(
                self.database.set_notification_flags,
                subscription.subscription_id,
                self._notification_flags,
            )

        await self.monitor.refresh_now()
        channel_mention = f"<#{self._channel_id}>"
        game_label = format_game_name(self.game_name)
        action = "Settings updated" if self.existing_subscription_id else "Now watching"
        await interaction.followup.send(
            f"{action} **{game_label}**! Notifications will post in {channel_mention}.",
            ephemeral=True,
        )


class MainConfigView(discord.ui.View):
    """Top-level configure screen: lists watched games and offers to edit or add one."""

    def __init__(
        self,
        *,
        subscriptions: list,
        guild_id: str,
        default_channel_id: str,
        database: Database,
        monitor: BgaMonitor,
        bga_client: BgaClient,
    ) -> None:
        super().__init__(timeout=300)
        self.subscriptions = subscriptions
        self.guild_id = guild_id
        self.default_channel_id = default_channel_id
        self.database = database
        self.monitor = monitor
        self.bga_client = bga_client
        self._selected_sub_id: int = subscriptions[0].subscription_id

        btn_row = 0
        if len(subscriptions) > 1:
            options = [
                discord.SelectOption(
                    label=f"{format_game_name(s.game_name)} (Table {s.table_id})",
                    value=str(s.subscription_id),
                    description=f"Notifications in channel {s.channel_id}",
                )
                for s in subscriptions
            ]
            game_select = discord.ui.Select(
                placeholder="Select a game to edit",
                min_values=1,
                max_values=1,
                row=0,
                options=options,
            )

            async def on_game_select(interaction: discord.Interaction) -> None:
                self._selected_sub_id = int(game_select.values[0])
                await interaction.response.defer()

            game_select.callback = on_game_select
            self.add_item(game_select)
            btn_row = 1

        edit_btn = discord.ui.Button(
            label="Edit settings",
            style=discord.ButtonStyle.secondary,
            row=btn_row,
        )

        async def on_edit(interaction: discord.Interaction) -> None:
            sub = next((s for s in self.subscriptions if s.subscription_id == self._selected_sub_id), None)
            if sub is None:
                await interaction.response.send_message("Game not found.", ephemeral=True)
                return
            view = GameSettingsView(
                table_id=sub.table_id,
                table_url=sub.table_url or "",
                base_url=sub.base_url or "",
                gameserver=sub.gameserver or "",
                game_name=sub.game_name or "",
                guild_id=self.guild_id,
                default_channel_id=sub.channel_id,
                database=self.database,
                monitor=self.monitor,
                bga_client=self.bga_client,
                existing_subscription_id=sub.subscription_id,
                existing_flags=sub.notification_flags,
            )
            game_label = format_game_name(sub.game_name)
            await interaction.response.send_message(
                f"Edit settings for **{game_label}** (Table {sub.table_id}):",
                view=view,
                ephemeral=True,
            )

        edit_btn.callback = on_edit
        self.add_item(edit_btn)

        add_btn = discord.ui.Button(
            label="Add new game",
            style=discord.ButtonStyle.primary,
            row=btn_row,
        )

        async def on_add(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                AddGameModal(
                    guild_id=self.guild_id,
                    default_channel_id=self.default_channel_id,
                    database=self.database,
                    monitor=self.monitor,
                    bga_client=self.bga_client,
                )
            )

        add_btn.callback = on_add
        self.add_item(add_btn)


class AddGameModal(discord.ui.Modal, title="Watch a new game"):
    url = discord.ui.TextInput(
        label="BGA Table URL",
        placeholder="https://boardgamearena.com/6/scythe?table=854745799",
        style=discord.TextStyle.short,
    )

    def __init__(
        self,
        *,
        guild_id: str,
        default_channel_id: str,
        database: Database,
        monitor: BgaMonitor,
        bga_client: BgaClient,
    ) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.default_channel_id = default_channel_id
        self.database = database
        self.monitor = monitor
        self.bga_client = bga_client

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            table_id, table_url, base_url, gameserver, game_name = parse_public_table_url(
                self.url.value.strip()
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        game_label = format_game_name(game_name)
        view = GameSettingsView(
            table_id=table_id,
            table_url=table_url,
            base_url=base_url,
            gameserver=gameserver,
            game_name=game_name,
            guild_id=self.guild_id,
            default_channel_id=self.default_channel_id,
            database=self.database,
            monitor=self.monitor,
            bga_client=self.bga_client,
        )
        await interaction.response.send_message(
            f"Set up **{game_label}** (Table {table_id}):\nPick a channel and choose which notifications to enable, then click **Start watching**.",
            view=view,
            ephemeral=True,
        )


class BgaCommands(commands.Cog):
    bga = app_commands.Group(name="bga", description=tr("command_group_description"))

    def __init__(self, database: Database, bga_client: BgaClient, monitor: BgaMonitor) -> None:
        self.database = database
        self.bga_client = bga_client
        self.monitor = monitor

    @staticmethod
    def _has_manage_permissions(interaction: discord.Interaction) -> bool:
        permissions = interaction.permissions
        return permissions.manage_guild or permissions.administrator

    @staticmethod
    def _truncate_text(value: str, max_length: int) -> str:
        if max_length <= 0:
            return ""
        if len(value) <= max_length:
            return value
        if max_length == 1:
            return "…"
        return value[: max_length - 1].rstrip(", ") + "…"

    @classmethod
    def _format_bounded_list(cls, items: list[str], empty_text: str, max_length: int) -> str:
        if max_length <= 0:
            return ""
        if not items:
            return cls._truncate_text(empty_text, max_length)

        included: list[str] = []
        total_count = len(items)
        for index, item in enumerate(items):
            candidate_items = included + [item]
            candidate = ", ".join(candidate_items)
            remaining_count = total_count - index - 1
            if remaining_count > 0:
                suffix = tr("watch_detected_more", count=remaining_count)
                candidate = f"{candidate}, {suffix}"
            if len(candidate) <= max_length:
                included.append(item)
                continue
            if not included:
                return cls._truncate_text(item, max_length)
            break

        remaining_count = total_count - len(included)
        if remaining_count <= 0:
            return ", ".join(included)

        suffix = tr("watch_detected_more", count=remaining_count)
        while included:
            candidate = f'{", ".join(included)}, {suffix}'
            if len(candidate) <= max_length:
                return candidate
            included.pop()

        return cls._truncate_text(suffix, max_length)

    @classmethod
    def _split_message_lines(cls, header: str, lines: list[str], max_length: int = 2000) -> list[str]:
        current_chunk = cls._truncate_text(header, max_length)
        chunks: list[str] = []

        for line in lines:
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) <= max_length:
                current_chunk = candidate
                continue

            if current_chunk:
                chunks.append(current_chunk)

            if len(line) <= max_length:
                current_chunk = line
            else:
                chunks.append(cls._truncate_text(line, max_length))
                current_chunk = ""

        if current_chunk:
            chunks.append(current_chunk)
        return chunks or [cls._truncate_text(header, max_length)]

    @staticmethod
    async def _send_ephemeral_chunks(
        interaction: discord.Interaction,
        chunks: list[str],
    ) -> None:
        if not chunks:
            chunks = [""]

        first_chunk, *remaining_chunks = chunks
        if interaction.response.is_done():
            await interaction.followup.send(first_chunk, ephemeral=True)
        else:
            await interaction.response.send_message(first_chunk, ephemeral=True)

        for chunk in remaining_chunks:
            await interaction.followup.send(chunk, ephemeral=True)

    @bga.command(name="link-member", description=tr("command_link_member_description"))
    @app_commands.describe(
        member=tr("command_link_member_member"),
        bga_player_name=tr("command_link_member_name"),
        bga_player_id=tr("command_link_member_id"),
    )
    async def link_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        bga_player_name: str | None = None,
        bga_player_id: str | None = None,
    ) -> None:
        if not self._has_manage_permissions(interaction):
            await interaction.response.send_message(
                tr("error_manage_server_required_link"),
                ephemeral=True,
            )
            return

        candidate_id = (bga_player_id or "").strip()
        candidate_name = (bga_player_name or "").strip()
        if not candidate_id and not candidate_name:
            await interaction.response.send_message(
                tr("error_need_bga_name_or_id"),
                ephemeral=True,
            )
            return
        if candidate_id and not candidate_id.isdigit():
            await interaction.response.send_message(
                tr("error_invalid_bga_player_id"),
                ephemeral=True,
            )
            return

        self.database.upsert_linked_user(
            discord_user_id=str(member.id),
            bga_player_id=candidate_id,
            bga_player_name=candidate_name,
        )
        linked_user = self.database.get_linked_user(str(member.id))
        if linked_user is None:
            raise RuntimeError("Failed to load the linked BGA user after saving it.")
        name_display = linked_user.bga_player_name or tr("link_missing_value_placeholder")
        id_display = linked_user.bga_player_id or tr("link_missing_value_placeholder")
        await interaction.response.send_message(
            tr(
                "link_saved",
                member_mention=member.mention,
                bga_name=name_display,
                bga_id=id_display,
            ),
            ephemeral=True,
        )

    @bga.command(name="unlink-member", description=tr("command_unlink_member_description"))
    @app_commands.describe(member=tr("command_unlink_member_member"))
    async def unlink_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        if not self._has_manage_permissions(interaction):
            await interaction.response.send_message(
                tr("error_manage_server_required_unlink"),
                ephemeral=True,
            )
            return

        removed = self.database.remove_linked_user(str(member.id))
        if not removed:
            await interaction.response.send_message(
                tr("unlink_not_found", member_mention=member.mention),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            tr("unlink_saved", member_mention=member.mention),
            ephemeral=True,
        )

    @bga.command(name="linked", description=tr("command_linked_description"))
    async def linked(self, interaction: discord.Interaction) -> None:
        linked_users = self.database.list_linked_users()
        if not linked_users:
            await interaction.response.send_message(
                tr("linked_none"),
                ephemeral=True,
            )
            return

        lines = [
            tr(
                "linked_line",
                discord_user_id=item.discord_user_id,
                bga_player_name=item.bga_player_name or tr("value_unknown"),
                bga_player_id=item.bga_player_id or tr("value_unknown"),
            )
            for item in linked_users
        ]
        await self._send_ephemeral_chunks(
            interaction,
            self._split_message_lines(tr("linked_header"), lines),
        )

    # /bga watch is intentionally removed; use /bga configure to add and configure games.

    @bga.command(name="unwatch", description=tr("command_unwatch_description"))
    @app_commands.describe(table_or_url=tr("command_unwatch_target"))
    async def unwatch(self, interaction: discord.Interaction, table_or_url: str) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                tr("error_command_server_channel_only"),
                ephemeral=True,
            )
            return

        try:
            table_id = parse_table_id(table_or_url)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        removed = self.database.remove_watch_subscription(
            table_id=table_id,
            guild_id=str(interaction.guild_id),
            channel_id=str(interaction.channel_id),
        )
        if not removed:
            await interaction.response.send_message(
                tr("unwatch_not_found", table_id=table_id),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            tr("unwatch_removed", table_id=table_id),
            ephemeral=True,
        )
        await self.monitor.refresh_now()

    @bga.command(name="unwatch-all", description=tr("command_unwatch_all_description"))
    async def unwatch_all(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        if not self._has_manage_permissions(interaction):
            await interaction.response.send_message(
                tr("error_manage_server_required_unwatch_all"),
                ephemeral=True,
            )
            return

        removed_count = self.database.remove_all_watch_subscriptions_for_guild(str(interaction.guild_id))
        if removed_count == 0:
            await interaction.response.send_message(
                tr("unwatch_all_none"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            tr("unwatch_all_removed", removed_count=removed_count),
            ephemeral=True,
        )
        await self.monitor.refresh_now()

    @bga.command(name="watchlist", description=tr("command_watchlist_description"))
    async def watchlist(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        subscriptions = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        if not subscriptions:
            await interaction.response.send_message(
                tr("watchlist_none"),
                ephemeral=True,
            )
            return

        lines = []
        for subscription in subscriptions:
            public_url = subscription.table_url or build_table_url(subscription.table_id)
            lines.append(
                tr(
                    "watchlist_line",
                    table_id=subscription.table_id,
                    game_name=format_game_name(subscription.game_name),
                    channel_label=tr("label_channel"),
                    channel_id=subscription.channel_id,
                    state_label=tr("label_state"),
                    state=(
                        tr("watch_state_initialized")
                        if subscription.is_initialized
                        else tr("watch_state_waiting_first_event")
                    ),
                    url_label=tr("label_url"),
                    table_url=public_url,
                )
            )

        await self._send_ephemeral_chunks(
            interaction,
            self._split_message_lines(tr("watchlist_header"), lines),
        )

    @bga.command(name="status", description=tr("command_status_description"))
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        subscriptions = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        if not subscriptions:
            await interaction.response.send_message(
                tr("status_none"),
                ephemeral=True,
            )
            return

        lines = []
        for subscription in subscriptions:
            if not subscription.is_initialized:
                state = tr("status_unknown")
            elif subscription.last_waiting_ids:
                linked_users = self.database.get_linked_users_by_bga_ids(subscription.last_waiting_ids)
                if linked_users:
                    mentions = ", ".join(f"<@{item.discord_user_id}>" for item in linked_users)
                    state = tr("status_waiting_for", mentions=mentions)
                else:
                    state = tr("status_waiting_no_link")
            else:
                state = tr("status_no_waiting")

            lines.append(
                tr(
                    "status_line",
                    table_id=subscription.table_id,
                    game_name=format_game_name(subscription.game_name),
                    channel_label=tr("label_channel"),
                    channel_id=subscription.channel_id,
                    waiting_ids_label=tr("label_waiting_ids"),
                    waiting_ids=", ".join(subscription.last_waiting_ids) or tr("value_none"),
                    state_label=tr("label_state"),
                    state=state,
                )
            )

        await self._send_ephemeral_chunks(
            interaction,
            self._split_message_lines(tr("status_header"), lines),
        )

    @bga.command(name="turn", description="Post whose turn it currently is for a watched game in this channel")
    async def turn(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                tr("error_command_server_channel_only"),
                ephemeral=True,
            )
            return

        all_subs = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        channel_subs = [s for s in all_subs if s.channel_id == str(interaction.channel_id)]

        if not channel_subs:
            await interaction.response.send_message(
                "No game is being watched in this channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        for subscription in channel_subs:
            game_label = format_game_name(subscription.game_name)
            waiting_ids = subscription.last_waiting_ids
            player_names = subscription.player_names

            if not subscription.is_initialized or not waiting_ids:
                await interaction.followup.send(
                    f"**{game_label}** (Table {subscription.table_id}): no active turn detected yet.",
                    ephemeral=True,
                )
                continue

            linked_users = await asyncio.to_thread(
                self.database.get_linked_users_for_players,
                {pid: player_names.get(pid, "") for pid in waiting_ids},
            )
            linked_by_bga_id = {u.bga_player_id: u for u in linked_users if u.bga_player_id}
            linked_by_name = {
                u.bga_player_name.casefold(): u for u in linked_users if u.bga_player_name
            }
            waiting_descriptions = ", ".join(
                self.monitor._format_waiting_player(pid, player_names, linked_by_bga_id, linked_by_name)
                for pid in waiting_ids
            )
            content = tr(
                "turn_message_content",
                game_label=tr("label_game"),
                game_name=game_label,
                table_label=tr("label_table"),
                table_id=subscription.table_id,
                players_label=tr("label_players_still_waiting"),
                players=waiting_descriptions or tr("value_none"),
                url_label=tr("label_url"),
                table_url=subscription.table_url or build_table_url(subscription.table_id),
            )
            view = TurnDetailsView(
                table_id=subscription.table_id,
                table_url=subscription.table_url,
                game_label=game_label,
                all_players=player_names,
            )
            await interaction.followup.send(content, view=view)

    @bga.command(name="link-all", description="Link all BGA players from watched games to Discord members at once")
    async def link_all(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        subs = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        all_player_names: dict[str, str] = {}
        for sub in subs:
            all_player_names.update(sub.player_names)

        if not all_player_names:
            await interaction.response.send_message(
                "No BGA players found yet. Watch a game first with `/bga watch`.",
                ephemeral=True,
            )
            return

        linked = await asyncio.to_thread(
            self.database.get_linked_users_by_bga_ids,
            list(all_player_names.keys()),
        )
        linked_bga_ids = {u.bga_player_id for u in linked if u.bga_player_id}
        unlinked = sorted(
            [(pid, name) for pid, name in all_player_names.items() if pid not in linked_bga_ids],
            key=lambda x: x[1].casefold(),
        )

        if not unlinked:
            await interaction.response.send_message(
                "All BGA players are already linked to Discord members.",
                ephemeral=True,
            )
            return

        view = LinkAllView(bga_players=unlinked, database=self.database)
        await interaction.response.send_message(
            LinkAllView.page_header(0, len(unlinked)),
            view=view,
            ephemeral=True,
        )

    @bga.command(name="logs", description="Show recent raw BGA events (for debugging move data)")
    async def logs(self, interaction: discord.Interaction) -> None:
        log_path = Path.cwd() / "bga-bot.log"
        if not log_path.exists():
            await interaction.response.send_message(
                "No log file found. Make sure the bot was started with output redirected to `bga-bot.log`.",
                ephemeral=True,
            )
            return

        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        bga_event_lines = [l for l in lines if "BGA_EVENT" in l]
        recent = bga_event_lines[-30:] if len(bga_event_lines) > 30 else bga_event_lines

        if not recent:
            await interaction.response.send_message(
                "No BGA_EVENT lines found in log yet. Watch a game and take a turn.",
                ephemeral=True,
            )
            return

        chunk = "\n".join(recent)
        if len(chunk) > 1900:
            chunk = chunk[-1900:]
        await interaction.response.send_message(f"```\n{chunk}\n```", ephemeral=True)

    @bga.command(name="info", description="Show what this bot does and all available commands")
    async def info(self, interaction: discord.Interaction) -> None:
        lines = [
            "## 🎲 BGA Turn Bot",
            "Watches your Board Game Arena tables and posts turn notifications in Discord.",
            "",
            "**How it works:**",
            "- Run `/bga configure` to add a game: paste the BGA table URL, pick the channel/thread, choose which notifications to enable",
            "- The bot posts ⏳ when it's your turn, ✅ when someone completes theirs",
            "- Link your BGA username to your Discord account so the bot can @mention you",
            "- When the game ends, the bot posts final scores and removes itself",
            "",
            "**Setup:**",
            "`/bga configure` - Add a new game to watch, or edit settings (channel, notifications) for an existing one",
            "",
            "**Watching:**",
            "`/bga unwatch <table_id>` - Stop watching a table",
            "`/bga unwatch-all` - Stop watching all tables in this server",
            "`/bga watchlist` - List all tables currently being watched",
            "`/bga turn` - Manually post whose turn it is right now",
            "`/bga status` - Show turn status for all watched tables (only you see this)",
            "",
            "**Linking players:**",
            "`/bga link-member <discord_user> <bga_name>` - Link one BGA player to a Discord member",
            "`/bga link-all` - Bulk-link all BGA players from watched games using dropdowns",
            "`/bga unlink-member <discord_user>` - Remove a link",
            "`/bga linked` - List all current Discord/BGA links",
            "",
            "**Other:**",
            "`/bga info` - Show this message",
            "`/bga logs` - Dump recent raw BGA events (for debugging)",
            "",
            "**Note:** Tables must be set to public (spectator mode on) in BGA for the bot to watch them.",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @bga.command(name="configure", description="Add a new game to watch or edit settings for an existing one")
    async def configure(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                tr("error_command_server_channel_only"), ephemeral=True
            )
            return

        all_subs = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))

        if not all_subs:
            await interaction.response.send_modal(
                AddGameModal(
                    guild_id=str(interaction.guild_id),
                    default_channel_id=str(interaction.channel_id),
                    database=self.database,
                    monitor=self.monitor,
                    bga_client=self.bga_client,
                )
            )
            return

        view = MainConfigView(
            subscriptions=all_subs,
            guild_id=str(interaction.guild_id),
            default_channel_id=str(interaction.channel_id),
            database=self.database,
            monitor=self.monitor,
            bga_client=self.bga_client,
        )
        game_list = ", ".join(
            f"**{format_game_name(s.game_name)}** (Table {s.table_id})" for s in all_subs
        )
        await interaction.response.send_message(
            f"Watching: {game_list}\nEdit settings for a game or add a new one:",
            view=view,
            ephemeral=True,
        )
