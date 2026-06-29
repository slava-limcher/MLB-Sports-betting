"""
Room manager.

Each "room" represents one venue watching one game. It holds:
- Connected WebSocket clients (TV displays + mobile phones)
- All placed bets and derived popularity metrics
- The leaderboard
- Cached API data (game state, odds, props)

Rooms are identified by a short venue code (e.g. "DENVBAR01")
embedded in the QR URL: https://yourapp.com/room/DENVBAR01
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from app.models import (
    BetMarket,
    BetSide,
    LeaderboardEntry,
    PlacedBet,
    PopularityMetric,
    TeamAffiliation,
    UserSession,
    WSMessage,
    WSMessageType,
)
from app.state import GameState, LineupContext

logger = logging.getLogger("barboards.room")

START_BALANCE = 1000.0

def american_profit(amount: float, odds: int) -> float:
    """Profit (excluding stake) on a winning American-odds bet."""
    if not odds:
        return 0.0
    return amount * odds / 100.0 if odds > 0 else amount * 100.0 / abs(odds)


class GameRoom:
    def __init__(self, room_code: str, game_id: int) -> None:
        self.room_code = room_code
        self.game_id = game_id
        self.game_state: Optional[GameState] = None
        self.lineup_context: Optional[LineupContext] = None
        self.current_pa: Optional[Dict[str, Any]] = None
        self.created_at = datetime.utcnow()
        self.seeded: bool = False

        # ── Connected clients ───────────────────
        self._connections: Dict[str, WebSocket] = {}

        # ── User sessions ───────────────────────
        self.users: Dict[str, UserSession] = {}

        # ── Bets ────────────────────────────────
        self.bets: List[PlacedBet] = []

        # ── Cached API data ─────────────────────
        self.game: Dict[str, Any] = {}            # BDL game metadata blob
        self.odds: Dict[str, Any] = {}
        self.props: List[Dict[str, Any]] = []
        self.stats: List[Dict[str, Any]] = []

        # ── Odds history for sparkline ──────────
        self.odds_history: List[Dict[str, Any]] = []

        # -- Win Expectancy history --------------
        self.we_history: List[Dict[str, Any]] = []

        # -- Simple live snapshot (set by the poller) + settlement guard --
        self.live: Dict[str, Any] = {}
        self._settled: bool = False

    # ── Connection management ───────────────────

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def connect(self, alias: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[alias] = ws
        logger.info("Room %s: %s connected (%d total)", self.room_code, alias, self.client_count)

        await self._send(alias, WSMessage(
            type=WSMessageType.ROOM_STATE,
            data=self._build_full_state(alias),
        ))

    async def disconnect(self, alias: str) -> None:
        self._connections.pop(alias, None)
        logger.info("Room %s: %s disconnected (%d remain)", self.room_code, alias, self.client_count)

    async def broadcast(self, msg: WSMessage, exclude: Optional[str] = None) -> None:
        dead: List[str] = []
        try:
            payload = msg.model_dump(mode="json")
        except Exception as e:
            logger.error("Failed to serialize message: %s", e)
            return
        for alias, ws in self._connections.items():
            if alias == exclude:
                continue
            try:
                await ws.send_json(payload)
            except Exception as e:
                logger.error("Send failed for %s: %s", alias, e)
                dead.append(alias)
        for alias in dead:
            self._connections.pop(alias, None)

    async def _send(self, alias: str, msg: WSMessage) -> None:
        ws = self._connections.get(alias)
        if ws:
            try:
                await ws.send_json(msg.model_dump(mode="json"))
            except Exception:
                self._connections.pop(alias, None)

    async def send_to_user(self, alias: str, msg: WSMessage) -> None:
        await self._send(alias, msg)

    # ── User actions ────────────────────────────

    def set_user_team(self, alias: str, team: TeamAffiliation) -> None:
        if alias not in self.users:
            self.users[alias] = UserSession(alias=alias, team=team)
        else:
            self.users[alias].team = team

    def set_favorite_player(self, alias: str, player_id: int, player_name: str) -> None:
        if alias in self.users:
            self.users[alias].favorite_player_id = player_id
            self.users[alias].favorite_player_name = player_name

    def get_users_tracking_player(self, player_id: int) -> List[str]:
        return [
            alias for alias, session in self.users.items()
            if session.favorite_player_id == player_id
        ]

    # ── Betting ─────────────────────────────────

    def place_bet(
        self,
        alias: str,
        market: BetMarket,
        side: BetSide,
        amount: float,
        odds: int,
        description: str = "",
        player_id: Optional[int] = None,
        line: Optional[float] = None,
    ) -> PlacedBet:
        user = self.users.get(alias)
        if user is None:
            user = UserSession(alias=alias)
            self.users[alias] = user
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if amount > user.balance:
            raise ValueError("Insufficient balance")
        user.balance -= amount   # stake is held until settlement
        bet = PlacedBet(
            id=str(uuid.uuid4())[:8],
            user_alias=alias,
            market=market,
            side=side,
            amount=amount,
            odds=odds,
            description=description,
            player_id=player_id,
            line=line,
        )
        self.bets.append(bet)
        return bet

    def settle(self, away_score: int, home_score: int) -> bool:
        """Settle all open bets against a final score. Returns True if any settled.
        Moneyline / run line / total grade automatically; player props (and any
        line-less spread/total) are refunded (push)."""
        total = away_score + home_score
        changed = False
        for b in self.bets:
            if b.settled:
                continue
            won: Optional[bool] = None  # None => push / refund
            if b.market == BetMarket.MONEYLINE:
                if away_score != home_score:
                    won = (b.side == BetSide.AWAY) == (away_score > home_score)
            elif b.market == BetMarket.SPREAD and b.line is not None:
                adj = (away_score + b.line - home_score) if b.side == BetSide.AWAY \
                    else (home_score + b.line - away_score)
                won = True if adj > 0 else (False if adj < 0 else None)
            elif b.market == BetMarket.OVER_UNDER and b.line is not None:
                if total != b.line:
                    won = (b.side == BetSide.OVER) == (total > b.line)
            b.settled = True
            b.won = won
            changed = True
            user = self.users.get(b.user_alias)
            if user is None:
                continue
            if won is True:
                user.balance += b.amount + american_profit(b.amount, b.odds)
                user.wins += 1
                user.streak = max(0, user.streak) + 1
            elif won is False:
                user.losses += 1
                user.streak = min(0, user.streak) - 1
            else:
                user.balance += b.amount  # push / void refund
        return changed

    def get_popularity(self) -> Dict[str, PopularityMetric]:
        metrics: Dict[str, PopularityMetric] = {}

        spread_bets = [b for b in self.bets if b.market == BetMarket.SPREAD and not b.settled]
        if spread_bets:
            m = PopularityMetric(market="spread", left_label="away", right_label="home")
            for b in spread_bets:
                if b.side == BetSide.AWAY:
                    m.left_money += b.amount
                    m.left_count += 1
                else:
                    m.right_money += b.amount
                    m.right_count += 1
            metrics["spread"] = m

        ml_bets = [b for b in self.bets if b.market == BetMarket.MONEYLINE and not b.settled]
        if ml_bets:
            m = PopularityMetric(market="moneyline", left_label="away", right_label="home")
            for b in ml_bets:
                if b.side == BetSide.AWAY:
                    m.left_money += b.amount
                    m.left_count += 1
                else:
                    m.right_money += b.amount
                    m.right_count += 1
            metrics["moneyline"] = m

        ou_bets = [b for b in self.bets if b.market == BetMarket.OVER_UNDER and not b.settled]
        if ou_bets:
            m = PopularityMetric(market="over_under", left_label="over", right_label="under")
            for b in ou_bets:
                if b.side == BetSide.OVER:
                    m.left_money += b.amount
                    m.left_count += 1
                else:
                    m.right_money += b.amount
                    m.right_count += 1
            metrics["over_under"] = m

        return metrics

    # ── Leaderboard ─────────────────────────────

    def get_leaderboard(self, top_n: int = 10) -> List[LeaderboardEntry]:
        board: List[LeaderboardEntry] = []
        for alias, u in self.users.items():
            open_n = sum(1 for b in self.bets if b.user_alias == alias and not b.settled)
            board.append(LeaderboardEntry(
                alias=alias,
                balance=round(u.balance, 2),
                net=round(u.balance - START_BALANCE, 2),
                wins=u.wins, losses=u.losses, streak=u.streak, open=open_n,
            ))
        board.sort(key=lambda e: e.balance, reverse=True)
        if board:
            board[0].badge = "crown"
            for e in board:
                if e.streak >= 3 and e.badge != "crown":
                    e.badge = "fire"
        return board[:top_n]

    # ── Odds history ────────────────────────────

    def record_odds_snapshot(self) -> None:
        if not self.odds:
            return
        # Inning comes from the live GameState now (was self.game_state.get("period"))
        inning = self.game_state.inning if self.game_state else None
        self.odds_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "inning": inning,
            "odds": self.odds,
        })

    # ── State serialization ─────────────────────

    def _build_full_state(self, for_alias: Optional[str] = None) -> Dict[str, Any]:
        popularity = self.get_popularity()

        game_state_dict = self.game_state.to_dict() if self.game_state else None
        lineup_dict = None
        if self.lineup_context:
            lineup_dict = {
                "home": self.lineup_context.home_lineup,
                "away": self.lineup_context.away_lineup,
            }

        return {
            "room_code": self.room_code,
            "game_id": self.game_id,
            "client_count": self.client_count,
            "game": self.game,                # BDL game metadata blob
            "live": self.live,                # simple poller-driven snapshot (score/inning/state)
            "game_state": game_state_dict,    # the live GameState (synced, bases, outs, etc.)
            "lineup": lineup_dict,            # lineups for upcoming-batter computation
            "odds": self.odds,
            "odds_history": self.odds_history[-30:],
            "props": self.props,
            "we_history": self.we_history[-100:],
            "popularity": {
                k: {
                    "left_pct": v.left_pct,
                    "right_pct": v.right_pct,
                    "left_label": v.left_label,
                    "right_label": v.right_label,
                    "left_count": v.left_count,
                    "right_count": v.right_count,
                }
                for k, v in popularity.items()
            },
            "leaderboard": [e.model_dump() for e in self.get_leaderboard()],
            "user": self.users.get(for_alias, {}) if for_alias else None,
            "current_pa": self.current_pa,
        }

    def upcoming_batter_ids(self, count: int = 3) -> list:
        """Compute the next N batters for the team currently at bat."""
        if not self.game_state or not self.lineup_context:
            return []
        if self.game_state.half == "top":
            lineup = self.lineup_context.away_lineup
            idx = self.game_state.away_lineup_idx
        else:
            lineup = self.lineup_context.home_lineup
            idx = self.game_state.home_lineup_idx
        if not lineup:
            return []
        return [lineup[(idx + i) % len(lineup)]["id"] for i in range(count)]


class RoomManager:
    """Global registry of active rooms."""

    def __init__(self) -> None:
        self._rooms: Dict[str, GameRoom] = {}

    def create_room(self, room_code: str, game_id: int) -> GameRoom:
        room = GameRoom(room_code=room_code, game_id=game_id)
        self._rooms[room_code] = room
        logger.info("Created room %s for game %d", room_code, game_id)
        return room

    def get_room(self, room_code: str) -> Optional[GameRoom]:
        return self._rooms.get(room_code)

    def get_or_create(self, room_code: str, game_id: int) -> GameRoom:
        if room_code not in self._rooms:
            return self.create_room(room_code, game_id)
        return self._rooms[room_code]

    def get_rooms_for_game(self, game_id: int) -> List[GameRoom]:
        return [r for r in self._rooms.values() if r.game_id == game_id]

    def all_active_game_ids(self) -> Set[int]:
        return {r.game_id for r in self._rooms.values()}

    def remove_room(self, room_code: str) -> None:
        self._rooms.pop(room_code, None)

    @property
    def active_rooms(self) -> Dict[str, GameRoom]:
        return self._rooms


room_manager = RoomManager()