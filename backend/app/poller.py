"""
Background polling engine.

The original design pushed live updates via balldontlie webhooks (see webhooks.py
+ state.py). Webhooks need a public URL + dashboard config, which isn't available
for local / laptop runs — so without this poller a room seeds once and then freezes.

This loop polls balldontlie directly for each active game (score, inning, status,
odds, props), stores a simple `live` snapshot on the room, broadcasts the changes
to all connected clients, and settles bets when a game goes final.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from app.bdl_client import bdl_client
from app.models import WSMessage, WSMessageType
from app.room import room_manager

logger = logging.getLogger("barboards.poller")

POLL_INTERVAL = 15          # seconds between game/odds polls
PROPS_EVERY = 4             # refresh props every Nth cycle

_task: Optional[asyncio.Task] = None


def _state_of(game: Dict[str, Any]) -> str:
    s = (game.get("status") or "").upper()
    if "FINAL" in s or "COMPLET" in s or "ENDED" in s:
        return "final"
    if "PROGRESS" in s or "LIVE" in s:
        return "live"
    return "pre"


def _live_snapshot(game: Dict[str, Any]) -> Dict[str, Any]:
    away = game.get("away_team_data") or {}
    home = game.get("home_team_data") or {}
    return {
        "state": _state_of(game),
        "away": away.get("runs", 0) or 0,
        "home": home.get("runs", 0) or 0,
        "inning": game.get("period") or 1,
        "status": game.get("status"),
    }


async def _poll_game(game_id: int, cycle: int) -> None:
    rooms = room_manager.get_rooms_for_game(game_id)
    if not rooms:
        return

    try:
        game = await bdl_client.get_game(game_id)
    except Exception:
        logger.exception("poll: failed to fetch game %d", game_id)
        return
    if not game:
        return

    live = _live_snapshot(game)

    odds = None
    try:
        odds = await bdl_client.get_betting_odds(game_id)
    except Exception:
        logger.debug("poll: odds fetch failed for %d", game_id)

    props = None
    if cycle % PROPS_EVERY == 0:
        try:
            props = await bdl_client.get_player_props(game_id)
        except Exception:
            logger.debug("poll: props fetch failed for %d", game_id)

    for room in rooms:
        room.game = game
        room.live = live
        # keep the seeded GameState scores fresh for new joiners
        if room.game_state is not None:
            room.game_state.away_score = live["away"]
            room.game_state.home_score = live["home"]
            room.game_state.inning = live["inning"]
            if live["state"] == "final":
                room.game_state.game_over = True
        if odds is not None:
            room.odds = odds
        if props is not None:
            room.props = props

        await room.broadcast(WSMessage(
            type=WSMessageType.GAME_UPDATE,
            data={"game": game, "live": live},
        ))
        if odds is not None:
            await room.broadcast(WSMessage(type=WSMessageType.ODDS_UPDATE, data={"odds": odds}))
        if props is not None:
            await room.broadcast(WSMessage(type=WSMessageType.PROPS_UPDATE, data={"props": props}))

        # Settle once, when the game ends.
        if live["state"] == "final" and not room._settled:
            room.settle(live["away"], live["home"])
            room._settled = True
            logger.info("Room %s: game final %d-%d, bets settled", room.room_code, live["away"], live["home"])
            board = room.get_leaderboard()
            await room.broadcast(WSMessage(
                type=WSMessageType.LEADERBOARD_UPDATE,
                data={"leaderboard": [e.model_dump() for e in board]},
            ))


async def _loop() -> None:
    cycle = 0
    while True:
        cycle += 1
        try:
            game_ids = list(room_manager.all_active_game_ids())
            for gid in game_ids:
                await _poll_game(gid, cycle)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("poller loop error")
        await asyncio.sleep(POLL_INTERVAL)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
        logger.info("Poller started (every %ds)", POLL_INTERVAL)


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("Poller stopped")
