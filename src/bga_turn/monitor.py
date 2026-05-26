from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import discord
from discord.ext import tasks

from .bga_client import BgaClient, BgaClientError, BgaNotPublicError, BgaTableUnavailableError
from .database import Database
from .i18n import tr
from .models import LinkedUser, NOTIFY_FINAL, NOTIFY_RECAP, NOTIFY_TURN, WatchSubscription
from .utils import build_table_url, format_game_name

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveTurnMessage:
    message: discord.Message
    waiting_ids: list[str]


class TurnDetailsView(discord.ui.View):
    def __init__(
        self,
        *,
        table_id: str,
        table_url: str | None,
        game_label: str,
        all_players: dict[str, str],
    ) -> None:
        super().__init__(timeout=21600)  # 6 hours — message is deleted when turn changes anyway
        self.table_id = table_id
        self.table_url = table_url
        self.game_label = game_label
        self.all_players = dict(all_players)

    @discord.ui.button(label="Show players", style=discord.ButtonStyle.secondary)
    async def show_players(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        lines = [f"**{self.game_label}** (Table {self.table_id})"]
        if self.table_url:
            lines.append(self.table_url)
        if self.all_players:
            lines.append("")
            lines.append("**Players in this game:**")
            for name in sorted(self.all_players.values()):
                lines.append(f"- {name}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class BgaMonitor:
    def __init__(
        self,
        bot: discord.Client,
        database: Database,
        bga_client: BgaClient,
        poll_seconds: int,
    ) -> None:
        self.bot = bot
        self.database = database
        self.bga_client = bga_client
        self._table_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_turn_messages: dict[int, ActiveTurnMessage] = {}
        self._last_player_name_refresh_at: dict[str, float] = {}
        self._last_moved_ids: dict[int, list[str]] = {}
        self.sync_tables.change_interval(seconds=max(5, poll_seconds))

    def start(self) -> None:
        if not self.sync_tables.is_running():
            self.sync_tables.start()

    def stop(self) -> None:
        if self.sync_tables.is_running():
            self.sync_tables.cancel()
        for task in self._table_tasks.values():
            task.cancel()
        self._table_tasks.clear()
        self._active_turn_messages.clear()
        self._last_player_name_refresh_at.clear()
        self._last_moved_ids.clear()

    @tasks.loop(seconds=30)
    async def sync_tables(self) -> None:
        await self._sync_tables_once()

    async def refresh_now(self) -> None:
        await self._sync_tables_once()

    async def _sync_tables_once(self) -> None:
        subscriptions = self.database.list_watch_subscriptions()
        active_table_ids = {subscription.table_id for subscription in subscriptions}
        active_subscription_ids = {subscription.subscription_id for subscription in subscriptions}

        for subscription_id in list(self._active_turn_messages):
            if subscription_id not in active_subscription_ids:
                active_message = self._active_turn_messages.pop(subscription_id, None)
                if active_message is not None:
                    try:
                        await active_message.message.delete()
                        LOGGER.info(tr("orphan_turn_message_deleted", subscription_id=subscription_id))
                    except discord.NotFound:
                        pass
                    except discord.DiscordException as exc:
                        LOGGER.error(tr("orphan_turn_message_delete_failed", subscription_id=subscription_id, error=exc))

        for table_id in list(self._table_tasks):
            if table_id not in active_table_ids:
                task = self._table_tasks.pop(table_id)
                task.cancel()
                self._last_player_name_refresh_at.pop(table_id, None)
                LOGGER.info(tr("worker_stopped", table_id=table_id))

        for table_id in sorted(active_table_ids):
            task = self._table_tasks.get(table_id)
            if task is None or task.done():
                self._table_tasks[table_id] = asyncio.create_task(self._run_table_worker(table_id))
                LOGGER.info(tr("worker_started", table_id=table_id))

    @sync_tables.before_loop
    async def before_sync_tables(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_table_worker(self, table_id: str) -> None:
        backoff_seconds = 5
        did_cleanup = False
        while True:
            try:
                subscriptions = self._subscriptions_for_table(table_id)
                if not subscriptions:
                    return

                reference = subscriptions[0]
                if not reference.table_url or not reference.base_url:
                    LOGGER.warning(tr("legacy_watch_without_url", table_id=table_id))
                    return

                table_info = self.bga_client.build_public_table_info(
                    table_id=reference.table_id,
                    table_url=reference.table_url,
                    base_url=reference.base_url,
                    gameserver=reference.gameserver or "",
                    game_name=reference.game_name or "unknown",
                )
                if not did_cleanup:
                    await self._cleanup_stale_table_messages(subscriptions, table_id)
                    did_cleanup = True
                finished_publicly = False
                if self.bga_client.enable_tableinfos_fallback:
                    try:
                        finished_publicly = await asyncio.to_thread(
                            self.bga_client.fetch_public_table_finished_status,
                            table_info,
                        )
                    except BgaClientError as exc:
                        LOGGER.warning(
                            tr(
                                "startup_tableinfos_check_failed",
                                table_id=table_id,
                                error=exc,
                            )
                        )
                if finished_publicly:
                    await self._finalize_finished_table(subscriptions, table_id)
                    return
                current_waiting_ids = self._select_previous_waiting_ids(subscriptions)
                known_player_names = self._merge_player_names(subscriptions)

                async for state in self.bga_client.watch_table(
                    table_info,
                    current_waiting_ids=current_waiting_ids,
                    known_player_names=known_player_names,
                ):
                    current_waiting_ids = state.waiting_ids or current_waiting_ids
                    known_player_names.update(state.player_names)
                    await self._apply_table_state(table_id, reference.game_name or "unknown", state)

                backoff_seconds = 5
            except asyncio.CancelledError:
                raise
            except BgaTableUnavailableError as exc:
                LOGGER.warning(tr("table_unavailable_autounwatch", table_id=table_id, error=exc))
                subscriptions = self._subscriptions_for_table(table_id)
                if subscriptions:
                    await self._finalize_finished_table(subscriptions, table_id)
                else:
                    self._table_tasks.pop(table_id, None)
                return
            except BgaNotPublicError as exc:
                LOGGER.warning(tr("table_not_public", table_id=table_id, error=exc))
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)
            except BgaClientError as exc:
                LOGGER.error(tr("websocket_error", table_id=table_id, error=exc))
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)
            except Exception:
                LOGGER.exception(tr("unexpected_worker_error", table_id=table_id))
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)

    async def _apply_table_state(self, table_id: str, fallback_game_name: str, state) -> None:
        subscriptions = self._subscriptions_for_table(table_id)
        if not subscriptions:
            return

        merged_player_names = self._merge_player_names(subscriptions)
        merged_player_names.update(state.player_names)
        waiting_ids = state.waiting_ids if state.waiting_ids is not None else self._select_previous_waiting_ids(subscriptions)
        merged_player_names = await self._refresh_missing_player_names(
            subscriptions=subscriptions,
            table_id=table_id,
            fallback_game_name=fallback_game_name,
            waiting_ids=waiting_ids,
            player_names=merged_player_names,
        )
        await asyncio.to_thread(self.database.enrich_linked_users_from_players, merged_player_names)

        if state.is_game_finished:
            LOGGER.info(tr("table_finished_public", table_id=table_id))
            await self._finalize_finished_table(subscriptions, table_id)
            return

        table_packet_id = state.highest_packet_id or max(item.last_packet_id for item in subscriptions)
        LOGGER.info(
            tr(
                "table_state",
                table_id=table_id,
                packet_id=table_packet_id,
                waiting_ids=state.waiting_ids,
                source=state.source,
                details=state.details,
            )
        )

        for subscription in subscriptions:
            previous_waiting_ids = subscription.last_waiting_ids
            waiting_ids = state.waiting_ids if state.waiting_ids is not None else previous_waiting_ids
            current_player_names = dict(subscription.player_names)
            current_player_names.update(state.player_names)
            game_name = subscription.game_name or fallback_game_name

            if not subscription.is_initialized:
                self.database.update_watch_state(
                    subscription_id=subscription.subscription_id,
                    last_packet_id=table_packet_id,
                    waiting_ids=waiting_ids,
                    player_names=current_player_names,
                    is_initialized=True,
                    game_name=game_name,
                )

                if waiting_ids:
                    message = await self._publish_turn_snapshot(
                        subscription=subscription,
                        table_id=table_id,
                        waiting_ids=waiting_ids,
                        player_names=current_player_names,
                        game_label=format_game_name(game_name),
                    )
                    if message is not None:
                        self._active_turn_messages[subscription.subscription_id] = ActiveTurnMessage(
                            message=message,
                            waiting_ids=list(waiting_ids),
                        )
                continue

            active_message = self._active_turn_messages.get(subscription.subscription_id)
            if active_message is None and waiting_ids:
                message = await self._publish_turn_snapshot(
                    subscription=subscription,
                    table_id=table_id,
                    waiting_ids=waiting_ids,
                    player_names=current_player_names,
                    game_label=format_game_name(game_name),
                )
                if message is not None:
                    self._active_turn_messages[subscription.subscription_id] = ActiveTurnMessage(
                        message=message,
                        waiting_ids=list(waiting_ids),
                    )
            elif waiting_ids != previous_waiting_ids:
                await self._handle_waiting_ids_transition(
                    subscription=subscription,
                    table_id=table_id,
                    previous_waiting_ids=previous_waiting_ids,
                    waiting_ids=waiting_ids,
                    player_names=current_player_names,
                    game_label=format_game_name(game_name),
                )

            self.database.update_watch_state(
                subscription_id=subscription.subscription_id,
                last_packet_id=table_packet_id,
                waiting_ids=waiting_ids,
                player_names=current_player_names,
                is_initialized=True,
                game_name=game_name,
            )

    async def _finalize_finished_table(
        self,
        subscriptions: list[WatchSubscription],
        table_id: str,
    ) -> None:
        announced: set[str] = set()
        for subscription in subscriptions:
            active_message = self._active_turn_messages.get(subscription.subscription_id)
            if active_message is not None:
                deleted = await self._delete_turn_message(
                    subscription=subscription,
                    active_message=active_message,
                    table_id=table_id,
                )
                if deleted:
                    self._active_turn_messages.pop(subscription.subscription_id, None)

            if subscription.channel_id not in announced and (subscription.notification_flags & NOTIFY_FINAL):
                await self._post_game_over_announcement(subscription, table_id)
                announced.add(subscription.channel_id)

            self.database.remove_watch_subscription(
                table_id=subscription.table_id,
                guild_id=subscription.guild_id,
                channel_id=subscription.channel_id,
            )

        self._table_tasks.pop(table_id, None)
        LOGGER.info(tr("table_finished_cleanup", table_id=table_id))

    async def _post_game_over_announcement(
        self,
        subscription: WatchSubscription,
        table_id: str,
    ) -> None:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None:
            return

        game_label = format_game_name(subscription.game_name)
        player_names = subscription.player_names

        result: dict = {}
        if subscription.table_url and subscription.base_url:
            table_info = self.bga_client.build_public_table_info(
                table_id=subscription.table_id,
                table_url=subscription.table_url,
                base_url=subscription.base_url,
                gameserver=subscription.gameserver or "",
                game_name=subscription.game_name or game_label,
            )
            result = await asyncio.to_thread(self.bga_client.fetch_public_table_result, table_info)

        lines = [f"🎉 Game over! **{game_label}** (Table {table_id}) has ended!"]

        scores = self._extract_scores(result, player_names)
        if scores:
            lines.append("")
            lines.append("🏆 **Final scores:**")
            medals = ["🥇", "🥈", "🥉"]
            for i, (name, score) in enumerate(scores):
                medal = medals[i] if i < len(medals) else "▪"
                lines.append(f"{medal} **{name}** - {score}")

        if not scores and player_names:
            names = ", ".join(sorted(player_names.values()))
            lines.append(f"Well played, {names}!")

        lines.append("")
        lines.append("🦄 ⭐ 🎉")

        try:
            await channel.send("\n".join(lines))
        except discord.DiscordException as exc:
            LOGGER.error("Failed to post game-over announcement for table %s: %s", table_id, exc)

    @staticmethod
    def _extract_scores(
        result: dict,
        player_names: dict[str, str],
    ) -> list[tuple[str, str]]:
        """Tries common BGA result shapes. Returns list of (name, score_str) sorted by rank."""
        if not result:
            return []

        # Shape 1: result["player"] = {"player_id": {"score": x, "rank": y, "name": z}}
        player_block = result.get("player")
        if isinstance(player_block, dict):
            entries = []
            for pid, info in player_block.items():
                if not isinstance(info, dict):
                    continue
                name = str(info.get("name") or info.get("player_name") or player_names.get(str(pid), str(pid)))
                score = info.get("score") or info.get("points") or info.get("result")
                rank = info.get("rank") or info.get("position") or 999
                try:
                    rank = int(rank)
                except (TypeError, ValueError):
                    rank = 999
                entries.append((rank, name, str(score) if score is not None else None))
            entries.sort(key=lambda x: x[0])
            return [(name, score) for _, name, score in entries if score is not None]

        # Shape 2: result["ranking"] = [{"name": x, "score": y}, ...]
        ranking = result.get("ranking")
        if isinstance(ranking, list):
            entries = []
            for item in ranking:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("player_name") or "")
                score = item.get("score") or item.get("points")
                if name:
                    entries.append((name, str(score) if score is not None else None))
            return [(name, score) for name, score in entries if score is not None]

        return []

    async def _handle_waiting_ids_transition(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        previous_waiting_ids: list[str],
        waiting_ids: list[str],
        player_names: dict[str, str],
        game_label: str,
    ) -> None:
        active_message = self._active_turn_messages.get(subscription.subscription_id)
        previous_set = set(previous_waiting_ids)
        waiting_set = set(waiting_ids)
        is_same_turn_progress = bool(previous_waiting_ids) and waiting_set.issubset(previous_set)
        just_moved_ids = [pid for pid in previous_waiting_ids if pid not in waiting_set]
        if just_moved_ids:
            self._last_moved_ids[subscription.subscription_id] = just_moved_ids

        if active_message is not None and not waiting_ids:
            deleted = await self._delete_turn_message(
                subscription=subscription,
                active_message=active_message,
                table_id=table_id,
            )
            if deleted:
                self._active_turn_messages.pop(subscription.subscription_id, None)
            return

        if active_message is not None and waiting_ids and is_same_turn_progress:
            edited = await self._edit_turn_message(
                subscription=subscription,
                active_message=active_message,
                table_id=table_id,
                waiting_ids=waiting_ids,
                player_names=player_names,
                game_label=game_label,
                just_moved_ids=just_moved_ids,
            )
            if edited:
                active_message.waiting_ids = list(waiting_ids)
                return

        if active_message is not None and waiting_ids and not is_same_turn_progress:
            deleted = await self._delete_turn_message(
                subscription=subscription,
                active_message=active_message,
                table_id=table_id,
            )
            if deleted:
                self._active_turn_messages.pop(subscription.subscription_id, None)

        if not waiting_ids:
            return

        message = await self._publish_turn_snapshot(
            subscription=subscription,
            table_id=table_id,
            waiting_ids=waiting_ids,
            player_names=player_names,
            game_label=game_label,
            just_moved_ids=just_moved_ids,
        )
        if message is not None:
            self._active_turn_messages[subscription.subscription_id] = ActiveTurnMessage(
                message=message,
                waiting_ids=list(waiting_ids),
            )

    async def _publish_turn_snapshot(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
        game_label: str,
        just_moved_ids: list[str] | None = None,
    ) -> discord.Message | None:
        if not (subscription.notification_flags & NOTIFY_TURN):
            return None

        channel = await self._resolve_channel(subscription, table_id)
        if channel is None:
            return None

        content = await self._build_turn_message_content(
            waiting_ids=waiting_ids,
            player_names=player_names,
            table_id=table_id,
            subscription=subscription,
            game_label=game_label,
            just_moved_ids=just_moved_ids,
        )
        view = TurnDetailsView(
            table_id=table_id,
            table_url=subscription.table_url,
            game_label=game_label,
            all_players=player_names,
        )

        try:
            message = await channel.send(content, view=view)
            LOGGER.info(tr("notification_sent", table_id=table_id, waiting_ids=waiting_ids))
            return message
        except discord.DiscordException as exc:
            LOGGER.error(
                tr(
                    "notification_send_failed",
                    table_id=table_id,
                    channel_id=subscription.channel_id,
                    error=exc,
                )
            )
            return None

    async def _edit_turn_message(
        self,
        *,
        subscription: WatchSubscription,
        active_message: ActiveTurnMessage,
        table_id: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
        game_label: str,
        just_moved_ids: list[str] | None = None,
    ) -> bool:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None:
            return False

        message = active_message.message

        content = await self._build_turn_message_content(
            waiting_ids=waiting_ids,
            player_names=player_names,
            table_id=table_id,
            subscription=subscription,
            game_label=game_label,
            just_moved_ids=just_moved_ids,
        )
        view = TurnDetailsView(
            table_id=table_id,
            table_url=subscription.table_url,
            game_label=game_label,
            all_players=player_names,
        )
        try:
            await message.edit(content=content, view=view)
            LOGGER.info(tr("turn_message_updated", table_id=table_id, waiting_ids=waiting_ids))
            return True
        except discord.NotFound:
            LOGGER.info(tr("turn_message_missing_update", table_id=table_id))
            return False
        except discord.DiscordException as exc:
            LOGGER.error(tr("turn_message_update_failed", table_id=table_id, error=exc))
            return False

    async def _delete_turn_message(
        self,
        *,
        subscription: WatchSubscription,
        active_message: ActiveTurnMessage,
        table_id: str,
    ) -> bool:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None:
            return False

        message = active_message.message
        try:
            await message.delete()
            LOGGER.info(tr("turn_message_deleted", table_id=table_id))
            return True
        except discord.NotFound:
            LOGGER.info(tr("turn_message_missing_delete", table_id=table_id))
            return True
        except discord.DiscordException as exc:
            LOGGER.error(tr("turn_message_delete_failed", table_id=table_id, error=exc))
            return False

    async def _cleanup_stale_table_messages(
        self,
        subscriptions: list[WatchSubscription],
        table_id: str,
    ) -> None:
        """On startup, recover the most recent bot turn message per subscription into
        _active_turn_messages so the bot doesn't repost an unchanged turn. Any duplicate
        messages for the same table in the same channel are deleted."""
        if self.bot.user is None:
            return

        seen_channels: set[str] = set()
        deleted_count = 0
        table_markers = {
            f"{tr('label_table')} : {table_id}",
            f"{tr('label_table')}: {table_id}",
            f"Table : {table_id}",
            f"Table: {table_id}",
            f"Table {table_id}:",
        }

        for subscription in subscriptions:
            if subscription.channel_id in seen_channels:
                continue
            seen_channels.add(subscription.channel_id)

            channel = await self._resolve_channel(subscription, table_id)
            if channel is None or not hasattr(channel, "history"):
                continue

            try:
                recovered: discord.Message | None = None
                async for message in channel.history(limit=100):
                    if message.author.id != self.bot.user.id:
                        continue
                    if not any(marker in message.content for marker in table_markers):
                        continue
                    if recovered is None:
                        recovered = message
                    else:
                        try:
                            await message.delete()
                            deleted_count += 1
                        except discord.NotFound:
                            pass
                        except discord.DiscordException as exc:
                            LOGGER.warning(
                                tr(
                                    "stale_message_delete_failed",
                                    table_id=table_id,
                                    channel_id=subscription.channel_id,
                                    error=exc,
                                )
                            )

                if recovered is not None and subscription.subscription_id not in self._active_turn_messages:
                    self._active_turn_messages[subscription.subscription_id] = ActiveTurnMessage(
                        message=recovered,
                        waiting_ids=list(subscription.last_waiting_ids),
                    )
                    LOGGER.info("Startup: recovered existing turn message for subscription %s (table %s)", subscription.subscription_id, table_id)

            except discord.DiscordException as exc:
                LOGGER.warning(
                    tr(
                        "channel_history_cleanup_failed",
                        channel_id=subscription.channel_id,
                        table_id=table_id,
                        error=exc,
                    )
                )

        if deleted_count:
            LOGGER.info(tr("startup_cleanup", deleted_count=deleted_count, table_id=table_id))

    async def _build_turn_message_content(
        self,
        *,
        waiting_ids: list[str],
        player_names: dict[str, str],
        table_id: str,
        subscription: WatchSubscription,
        game_label: str,
        just_moved_ids: list[str] | None = None,
    ) -> str:
        observed_waiting_players = {
            player_id: player_names.get(player_id, "")
            for player_id in waiting_ids
        }
        linked_users = await asyncio.to_thread(
            self.database.get_linked_users_for_players,
            observed_waiting_players,
        )
        linked_users_by_bga_id = {user.bga_player_id: user for user in linked_users if user.bga_player_id}
        linked_users_by_name = {
            user.bga_player_name.casefold(): user
            for user in linked_users
            if user.bga_player_name
        }
        mentions = [
            self._format_player_mention(pid, player_names, linked_users_by_bga_id, linked_users_by_name)
            for pid in waiting_ids
        ]
        if len(mentions) == 1:
            callout = f"⏳ {mentions[0]}, you're up"
        else:
            callout = f"⏳ {', '.join(mentions)} - you're all up"

        table_url = subscription.table_url or build_table_url(table_id)
        lines = [f"{callout} in **{game_label}**!"]

        include_recap = (subscription.notification_flags & NOTIFY_RECAP) and just_moved_ids
        if include_recap:
            moved_names = ", ".join(player_names.get(pid, pid) for pid in just_moved_ids)
            lines.append(f"✅ {moved_names} just completed their turn.")

        lines.append(f"Table {table_id}: {table_url}")
        return "\n".join(lines)

    async def _refresh_missing_player_names(
        self,
        *,
        subscriptions: list[WatchSubscription],
        table_id: str,
        fallback_game_name: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
    ) -> dict[str, str]:
        missing_player_ids = [player_id for player_id in waiting_ids if not player_names.get(player_id, "").strip()]
        if not missing_player_ids:
            return player_names

        last_refresh_at = self._last_player_name_refresh_at.get(table_id, 0.0)
        now = time.monotonic()
        if now - last_refresh_at < 60:
            return player_names

        reference = subscriptions[0]
        if not reference.table_url or not reference.base_url:
            return player_names

        self._last_player_name_refresh_at[table_id] = now
        table_info = self.bga_client.build_public_table_info(
            table_id=reference.table_id,
            table_url=reference.table_url,
            base_url=reference.base_url,
            gameserver=reference.gameserver or "",
            game_name=reference.game_name or fallback_game_name,
        )

        try:
            refreshed_names = await asyncio.to_thread(self.bga_client.fetch_public_player_names, table_info)
        except BgaClientError as exc:
            LOGGER.debug(tr("player_name_refresh_failed", table_id=table_id, error=exc))
            return player_names

        if not refreshed_names:
            return player_names

        merged_names = dict(player_names)
        merged_names.update(refreshed_names)
        resolved_missing_ids = [
            player_id
            for player_id in missing_player_ids
            if merged_names.get(player_id, "").strip()
        ]
        if resolved_missing_ids:
            LOGGER.info(
                tr(
                    "player_name_refresh_success",
                    table_id=table_id,
                    count=len(resolved_missing_ids),
                )
            )
        return merged_names

    async def _resolve_channel(self, subscription: WatchSubscription, table_id: str) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(int(subscription.channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(subscription.channel_id))
            except discord.DiscordException as exc:
                LOGGER.error(
                    tr(
                        "channel_fetch_failed",
                        channel_id=subscription.channel_id,
                        table_id=table_id,
                        error=exc,
                    )
                )
                return None

        if not isinstance(channel, discord.abc.Messageable):
            LOGGER.error(
                tr(
                    "channel_not_messageable",
                    channel_id=subscription.channel_id,
                    table_id=table_id,
                )
            )
            return None
        return channel

    def _subscriptions_for_table(self, table_id: str) -> list[WatchSubscription]:
        return [item for item in self.database.list_watch_subscriptions() if item.table_id == table_id]

    @staticmethod
    def _format_player_reference(player_id: str, player_names: dict[str, str]) -> str:
        player_name = player_names.get(player_id)
        if player_name and player_name != player_id:
            return f"{player_name} ({player_id})"
        return player_id

    @classmethod
    def _format_waiting_player(
        cls,
        player_id: str,
        player_names: dict[str, str],
        linked_users_by_bga_id: dict[str, LinkedUser],
        linked_users_by_name: dict[str, LinkedUser],
    ) -> str:
        linked_user = linked_users_by_bga_id.get(player_id)
        if linked_user is None:
            player_name = player_names.get(player_id, "").strip()
            if player_name:
                linked_user = linked_users_by_name.get(player_name.casefold())
        if linked_user is None:
            return cls._format_player_reference(player_id, player_names)
        player_label = linked_user.bga_player_name or player_names.get(player_id, "").strip() or player_id
        player_id_label = linked_user.bga_player_id or player_id
        return f"<@{linked_user.discord_user_id}> {player_label} ({player_id_label})"

    @staticmethod
    def _select_previous_waiting_ids(subscriptions: list[WatchSubscription]) -> list[str]:
        initialized_subscriptions = [item for item in subscriptions if item.is_initialized]
        if not initialized_subscriptions:
            return []
        initialized_subscriptions.sort(key=lambda item: item.last_packet_id, reverse=True)
        return initialized_subscriptions[0].last_waiting_ids

    @staticmethod
    def _merge_player_names(subscriptions: list[WatchSubscription]) -> dict[str, str]:
        merged: dict[str, str] = {}
        for subscription in subscriptions:
            merged.update(subscription.player_names)
        return merged
