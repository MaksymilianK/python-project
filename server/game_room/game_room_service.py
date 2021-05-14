import asyncio
import json
import random
from typing import Optional, Coroutine

from server import PlayerRepository
from server.request import InvalidRequestException, assert_in, parse_vector
from server.game_room.game_room import RankedGameRoom, PrivateGameRoom, GameRoom, GameRoomType
from server.game.game_runner import GameRunner, GameEndStatus
from server.player.player import Player
from shared.chess_engine.move import MOVE_TYPES_BY_CODE, MoveType, Move, Capturing, Castling, EnPassant, Promotion, \
    PromotionWithCapturing
from shared.chess_engine.piece import PIECE_TYPES_FROM_CODE
from shared.chess_engine.position import Vector2d
from shared.game.game_type import GameType, GAME_TYPE_BY_CODE
from shared.game.ranking import PlayerScore, elo_change
from shared.message.message_code import MessageCode
from shared.message.private_room_joining_status import PrivateRoomJoiningStatus

NUM_OF_BUCKETS = 30
ACCESS_KEY_LEN = 6


def _elo_bucket(player: Player, game_type: GameType) -> int:
    bucket = player.elo[game_type] // NUM_OF_BUCKETS
    return bucket if bucket < NUM_OF_BUCKETS else NUM_OF_BUCKETS - 1


async def _on_private_time_end(game_end_status: GameEndStatus):
    message_str = json.dumps({
        "code": MessageCode.GAME_TIME_END.value
    })
    await asyncio.wait([game_end_status.winner.send(message_str), game_end_status.loser.send(message_str)])


def _parse_move(position_from: Vector2d, position_to: Vector2d) -> Move:
    return Move(position_from, position_to)


def _parse_capturing(position_from: Vector2d, position_to: Vector2d) -> Capturing:
    return Capturing(position_from, position_to)


def _parse_castling(move_message: dict, position_from: Vector2d, position_to: Vector2d) -> Castling:
    assert_in(move_message, ("rookFrom", tuple), ("rookTo", tuple))
    return Castling(
        position_from,
        position_to,
        parse_vector(move_message["rookFrom"]),
        parse_vector(move_message["rookTo"])
    )


def _parse_en_passant(move_message: dict, position_from: Vector2d, position_to: Vector2d) -> EnPassant:
    assert_in(move_message, ("capturedPosition", tuple))
    return EnPassant(
        position_from,
        position_to,
        parse_vector(move_message["capturedPosition"])
    )


def _parse_promotion(move_message: dict, position_from: Vector2d, position_to: Vector2d,
                    with_capturing: bool = False) -> Promotion:
    assert_in(move_message, ("pieceType", int))
    try:
        piece_type = PIECE_TYPES_FROM_CODE[move_message["pieceType"]]
    except KeyError:
        raise InvalidRequestException("unrecognized piece type")

    if with_capturing:
        return PromotionWithCapturing(position_from, position_to, piece_type)
    else:
        return Promotion(position_from, position_to, piece_type)


class GameRoomService:
    def __init__(self, player_repo: PlayerRepository):
        self.player_repo = player_repo
        self.ranked_rooms: dict[Player, RankedGameRoom] = {}
        self.private_rooms_by_player: dict[Player, PrivateGameRoom] = {}
        self.private_rooms_by_access_key: dict[str, PrivateGameRoom] = {}
        self.ranked_queue: dict[GameType, list[set[Player]]] = {}

        for game_type in GameType:
            self.ranked_queue[game_type] = [set() for _ in range(NUM_OF_BUCKETS)]

    async def join_ranked_queue(self, message: dict, sender: Player):
        assert_in(message, ("gameType", int))

        try:
            game_type = GAME_TYPE_BY_CODE[message["gameType"]]
        except KeyError:
            raise InvalidRequestException("unknown game type")

        if self._player_in_room_or_queue(sender):
            return

        bucket = _elo_bucket(sender, game_type)
        self.ranked_queue[game_type][bucket].add(sender)

        await sender.send(json.dumps({
            "code": MessageCode.JOIN_RANKED_QUEUE.value
        }))

    async def cancel_joining_ranked(self, sender: Player):
        for game_type in GameType:
            bucket = _elo_bucket(sender, game_type)
            if sender in self.ranked_queue[game_type][bucket]:
                self.ranked_queue[game_type][bucket].remove(sender)

                await sender.send(json.dumps({
                    "code": MessageCode.CANCEL_JOINING_RANKED.value
                }))
                return

    async def create_private_room(self, sender: Player):
        if self._player_in_room_or_queue(sender):
            return

        access_key = self._generate_access_key()
        room = PrivateGameRoom(sender, GameRunner(), access_key)
        self.private_rooms_by_access_key[access_key] = room
        self.private_rooms_by_player[sender] = room

        await sender.send(json.dumps({
            "code": MessageCode.CREATE_PRIVATE_ROOM.value,
            "accessKey": access_key
        }))

    async def join_private_room(self, message: dict, sender: Player):
        if self._player_in_room_or_queue(sender):
            return

        assert_in(message, ("accessKey", str))
        access_key = message["accessKey"]

        try:
            room = self.private_rooms_by_access_key[access_key]
        except KeyError:
            await sender.send(json.dumps({
                "code": MessageCode.JOIN_PRIVATE_ROOM.value,
                "status": PrivateRoomJoiningStatus.ROOM_NOT_EXIST.value
            }))
            return

        if room.full:
            await sender.send(json.dumps({
                "code": MessageCode.JOIN_PRIVATE_ROOM.value,
                "status": PrivateRoomJoiningStatus.ROOM_FULL.value
            }))
            return
        elif sender in room.kicked:
            await sender.send(json.dumps({
                "code": MessageCode.JOIN_PRIVATE_ROOM.value,
                "status": PrivateRoomJoiningStatus.KICKED_FROM_ROOM.value
            }))
            return

        room.guest = sender
        await room.send(json.dumps({
            "code": MessageCode.JOIN_PRIVATE_ROOM.value,
            "status": PrivateRoomJoiningStatus.SUCCESS.value
        }))

    async def leave_private_room(self, sender: Player):
        try:
            room = self.private_rooms_by_player[sender]
        except KeyError:
            return

        if sender is room.host:
            player_who_left = room.host
            self._remove_private(room)
        else:
            player_who_left = room.guest
            room.guest = None

        await room.send(json.dumps({
            "code": MessageCode.LEAVE_PRIVATE_ROOM.value,
            "player": player_who_left.as_response()
        }))

    async def kick_from_private_room(self, sender: Player):
        try:
            room = self.private_rooms_by_player[sender]
        except KeyError:
            return

        if not room.guest:
            return

        guest = room.guest
        room.runner.clean()
        self.private_rooms_by_player.pop(room.guest)
        room.kicked.add(room.guest)
        room.guest = None

        message_str = json.dumps({
            "code": MessageCode.KICK_FROM_PRIVATE_ROOM.value
        })
        await asyncio.wait([room.host.send(message_str), guest.send(message_str)])

    async def start_private_game(self, message: dict, sender: Player):
        try:
            room = self.private_rooms_by_player[sender]
        except KeyError:
            return

        if sender is not room.host:
            raise InvalidRequestException("player is not a host")

        if not room.guest:
            return

        assert_in(message, ("gameType", int))
        try:
            game_type = GAME_TYPE_BY_CODE[message["gameType"]]
        except KeyError:
            raise InvalidRequestException("unknown game type")

        room.runner.start(room.host, room.guest, game_type, _on_private_time_end)

        await room.send(json.dumps({
            "code": MessageCode.START_PRIVATE_GAME,
            "gameType": game_type.value,
            "teams": {p.as_response(): t.value for p, t in room.runner.teams.items()}
        }))

    async def surrender(self, sender: Player):
        room = self._room_by_player(sender)
        if not room:
            return

        game_end_status = room.runner.on_surrender(sender)
        if room.type == GameRoomType.RANKED:
            await self._remove_ranked(game_end_status)

        await room.send(json.dumps({
            "code": MessageCode.GAME_SURRENDER.value,
            "player": sender.as_response()
        }))

    async def offer_draw(self, sender: Player):
        room = self._room_by_player(sender)
        if not room:
            return

        if room.runner.on_draw_offer(sender):
            await room.send(json.dumps({
                "code": MessageCode.GAME_OFFER_DRAW,
                "player": sender.as_response()
            }))

    async def respond_to_draw_offer(self, message: dict, sender: Player):
        room = self._room_by_player(sender)
        if not room:
            return

        assert_in(message, ("accepted", bool))
        accepted = message["accepted"]

        if accepted:
            game_end_status = room.runner.on_draw_offer_accepted(sender)
            if room.type == GameRoomType.RANKED:
                await self._remove_ranked(game_end_status)

        await room.send(json.dumps({
            "code": MessageCode.GAME_RESPOND_TO_DRAW_OFFER.value,
            "accepted": accepted
        }))

    async def claim_draw(self, sender: Player):
        room = self._room_by_player(sender)
        if not room:
            return

        game_end_status = room.runner.on_draw_claim(sender)
        if not game_end_status:
            return

        if room.type == GameRoomType.RANKED:
            await self._remove_ranked(game_end_status)

        await room.send(json.dumps({
            "code": MessageCode.GAME_CLAIM_DRAW.value,
            "player": sender.as_response()
        }))

    async def move(self, message: dict, sender: Player):
        room = self._room_by_player(sender)
        if not room:
            return

        assert_in(message, ("move", dict))
        move_message = message["move"]
        assert_in(move_message, ("type", int))

        try:
            move_type = MOVE_TYPES_BY_CODE[message["type"]]
        except KeyError:
            raise InvalidRequestException("unrecognized move type")

        assert_in(message, ("positionFrom", tuple), ("positionTo", tuple))
        position_from, position_to = parse_vector(message["positionFrom"]), parse_vector(message["positionTo"])

        if move_type == MoveType.MOVE:
            move = _parse_move(position_from, position_to)
        elif move_type == MoveType.CAPTURING:
            move = _parse_capturing(position_from, position_to)
        elif move_type == MoveType.CASTLING:
            move = _parse_castling(move_message, position_from, position_to)
        elif move_type == MoveType.EN_PASSANT:
            move = _parse_en_passant(move_message, position_from, position_to)
        elif move_type == MoveType.PROMOTION:
            move = _parse_promotion(move_message, position_from, position_to)
        else:
            move = _parse_promotion(message, position_from, position_to, True)

        move_status = room.runner.on_move(move, sender)
        if not move_status.successful:
            return

        if room.type == GameRoomType.RANKED:
            await self._remove_ranked(move_status.game_end_status)

        await room.send(json.dumps({
            "code": MessageCode.GAME_MOVE.value,
            "move": move_message
        }))

    def _generate_access_key(self) -> str:
        access_key = "".join([chr(random.randint(ord('A'), ord('Z'))) for _ in range(ACCESS_KEY_LEN)])
        while access_key in self.private_rooms_by_access_key:
            access_key = "".join([chr(random.randint(ord('A'), ord('Z'))) for _ in range(ACCESS_KEY_LEN)])

        return access_key

    def _player_in_room_or_queue(self, player: Player) -> bool:
        if self.ranked_rooms[player] or self.private_rooms_by_player[player]:
            return True

        for game_type, buckets in self.ranked_queue.items():
            player_bucket = _elo_bucket(player, game_type)
            if player in buckets[player_bucket]:
                return True

        return False

    def _room_by_player(self, player: Player) -> Optional[GameRoom]:
        if player in self.ranked_rooms:
            return self.ranked_rooms[player]
        elif player in self.private_rooms_by_player[player]:
            return self.private_rooms_by_player[player]

    async def _remove_ranked(self, game_end_status: GameEndStatus):
        self.ranked_rooms[game_end_status.winner].runner.clean()
        self.ranked_rooms.pop(game_end_status.winner)
        self.ranked_rooms.pop(game_end_status.loser)

        player1 = game_end_status.winner
        player2 = game_end_status.loser
        game_type = game_end_status.game_type

        if game_end_status.draw:
            player1_score = PlayerScore.DRAW
        else:
            player1_score = PlayerScore.WIN

        player_elo_change = elo_change(
            player1.elo[game_type],
            player2.elo[game_type],
            player1_score
        )
        player1.elo[game_type] += player_elo_change
        player1.elo[game_type] -= player_elo_change

        await asyncio.wait([
            self.player_repo.update_elo(player1.nick, player1.elo[game_type], game_type),
            self.player_repo.update_elo(player2.nick, player2.elo[game_type], game_type)
        ])

    def _remove_private(self, room: PrivateGameRoom):
        room.runner.clean()
        self.private_rooms_by_player.pop(room.host)
        if room.guest:
            self.private_rooms_by_player.pop(room.guest)

        self.private_rooms_by_access_key.pop(room.access_key)

    def _create_ranked(self, player1: Player, player2: Player, game_type: GameType):
        room = RankedGameRoom(player1, player2, GameRunner())
        self.ranked_rooms[player1] = room
        self.ranked_rooms[player2] = room
        room.runner.start(player1, player2, game_type, self._on_ranked_time_end)

    async def _on_ranked_time_end(self, game_end_status: GameEndStatus):
        await self._remove_ranked(game_end_status)
        message_str = json.dumps({
            "code": MessageCode.GAME_TIME_END.value
        })
        await asyncio.wait([game_end_status.winner.send(message_str), game_end_status.loser.send(message_str)])

    def match_players(self):
        left: Optional[Player] = None
        rooms: list[Coroutine] = []

        for game_type in GameType:
            for group in self.ranked_queue[game_type]:
                if left and len(group) > 0:
                    self._create_ranked(left, group.pop(), game_type)

                while len(group) >= 2:
                    self._create_ranked(group.pop(), group.pop(), game_type)

                if len(group) == 0:
                    left = None
                else:
                    left = group.pop()

        await asyncio.gather(*rooms)
        await asyncio.sleep(5)
