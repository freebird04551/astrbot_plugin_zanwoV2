import asyncio
import logging
import re
from datetime import datetime, time, timedelta
from typing import Iterable, Optional

from aiocqhttp import CQHttp
from aiocqhttp.exceptions import ActionFailed, Error
import astrbot.api.message_components as Comp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.permission import PermissionType

logger = logging.getLogger(__name__)

MAX_LIKES = 20
DEFAULT_AUTO_LIKE_TIME = "00:00"
AUTO_LIKE_RETRY_SECONDS = 60
AUTO_LIKE_MAX_SLEEP_SECONDS = 300
PROFILE_LIKE_PAGE_SIZE = 50
PROFILE_LIKE_MAX_ROWS = 1000


def normalize_qq_id(value: object) -> Optional[str]:
    qq_id = str(value).strip()
    if not qq_id.isascii() or not qq_id.isdecimal():
        return None
    return qq_id.lstrip("0") or None


def normalize_qq_ids(values: Iterable[object]) -> list[str]:
    normalized = (normalize_qq_id(value) for value in values)
    return list(dict.fromkeys(qq_id for qq_id in normalized if qq_id))


def parse_daily_time(value: object) -> Optional[time]:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(value).strip())
    if not match:
        return None
    return time(hour=int(match.group(1)), minute=int(match.group(2)))


def action_error_message(error: ActionFailed) -> str:
    result = error.result if isinstance(error.result, dict) else {}
    return " ".join(
        str(result[key])
        for key in ("wording", "message", "msg")
        if result.get(key)
    ) or str(error)


def like_failure_reply(error_message: str) -> str:
    lowered = error_message.lower()
    if any(keyword in lowered for keyword in ("已达", "上限", "limit", "quota")):
        return "今天给该用户的赞已达上限"
    if any(
        keyword in lowered
        for keyword in ("权限", "隐私", "permission", "privacy")
    ):
        return "对方的隐私设置不允许当前账号点赞"
    return "点赞失败，QQ协议端返回了错误"


@register(
    "astrbot_plugin_zanwoV2",
    "Futureppo",
    "QQ名片点赞与每日定时点赞",
    "2.1.0",
    "https://github.com/freebird04551/astrbot_plugin_zanwoV2",
)
class ZanwoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._auto_like_task: Optional[asyncio.Task] = None
        self.white_list_groups = set(
            normalize_qq_ids(config.get("white_list_groups", []))
        )
        self.subscribed_users = normalize_qq_ids(
            config.get("subscribed_users", [])
        )
        self.last_auto_like_date: Optional[str] = None
        self.auto_like_time = parse_daily_time(
            config.get("auto_like_time", DEFAULT_AUTO_LIKE_TIME)
        )

    async def initialize(self) -> None:
        stored_date = await self.get_kv_data("last_auto_like_date", None)
        self.last_auto_like_date = stored_date or self.config.get("zanwo_date")
        if self.last_auto_like_date and not stored_date:
            await self.put_kv_data(
                "last_auto_like_date", self.last_auto_like_date
            )
        if self.auto_like_time is None:
            logger.error(
                "Scheduled auto-like is disabled: invalid auto_like_time %r",
                self.config.get("auto_like_time"),
            )
            return
        self._auto_like_task = asyncio.create_task(
            self._auto_like_loop(), name="zanwo-auto-like"
        )

    def _is_group_allowed(self, event: AiocqhttpMessageEvent) -> bool:
        group_id = event.get_group_id()
        if group_id and self.white_list_groups:
            return str(group_id) in self.white_list_groups
        return True

    async def _run_like(
        self,
        event: AiocqhttpMessageEvent,
        target_ids: list[str],
        amount: int,
    ) -> Optional[str]:
        if not self._is_group_allowed(event):
            return None
        return await self._like(
            event.bot, target_ids, str(event.get_self_id()), amount
        )

    def _next_auto_like_delay(self, now: datetime) -> float:
        scheduled = datetime.combine(now.date(), self.auto_like_time)
        if now < scheduled:
            return (scheduled - now).total_seconds()
        if self.last_auto_like_date == now.date().isoformat():
            return (scheduled + timedelta(days=1) - now).total_seconds()
        return 0

    async def _find_auto_like_accounts(self) -> dict[str, tuple[CQHttp, str]]:
        accounts = {}
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name != "aiocqhttp":
                continue
            client = getattr(platform, "bot", None)
            connected_clients = getattr(client, "_wsr_api_clients", {})
            for route_id in list(connected_clients):
                self_id = normalize_qq_id(route_id)
                if self_id:
                    accounts[self_id] = (client, str(route_id))
                elif route_id == "*":
                    try:
                        login_info = await client.call_action(
                            "get_login_info", self_id="*"
                        )
                    except Error:
                        continue
                    if isinstance(login_info, dict):
                        self_id = normalize_qq_id(login_info.get("user_id", ""))
                        if self_id:
                            accounts.setdefault(self_id, (client, "*"))

        return accounts

    async def _auto_like_loop(self) -> None:
        while True:
            try:
                now = datetime.now()
                today = now.date().isoformat()
                delay = self._next_auto_like_delay(now)
                if delay > 0:
                    await asyncio.sleep(min(delay, AUTO_LIKE_MAX_SLEEP_SECONDS))
                    continue

                accounts = {}
                if self.subscribed_users:
                    accounts = await self._find_auto_like_accounts()
                    if not accounts:
                        logger.warning(
                            "Scheduled auto-like is waiting for a connected QQ account"
                        )
                        await asyncio.sleep(AUTO_LIKE_RETRY_SECONDS)
                        continue

                self.last_auto_like_date = today
                await self.put_kv_data("last_auto_like_date", today)
                for self_id, (client, route_id) in accounts.items():
                    await self._like(
                        client,
                        self.subscribed_users,
                        route_id,
                        MAX_LIKES,
                        resolve_nickname=False,
                    )
                    logger.info(
                        "Scheduled auto-like completed for %d users with QQ %s",
                        len(self.subscribed_users),
                        self_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled auto-like failed")
                await asyncio.sleep(AUTO_LIKE_RETRY_SECONDS)

    async def terminate(self) -> None:
        if self._auto_like_task:
            self._auto_like_task.cancel()
            await asyncio.gather(self._auto_like_task, return_exceptions=True)
            self._auto_like_task = None

    async def _like(
        self,
        client: CQHttp,
        ids: list[str],
        self_id: str,
        amount: int,
        resolve_nickname: bool = True,
    ) -> str:
        replies = []
        for qq_id in normalize_qq_ids(ids):
            username = qq_id
            if resolve_nickname:
                try:
                    user_info = await client.call_action(
                        "get_stranger_info", user_id=int(qq_id), self_id=self_id
                    )
                    if isinstance(user_info, dict):
                        username = user_info.get("nickname") or qq_id
                except Error as error:
                    logger.warning(
                        "Failed to get nickname for QQ %s: %s", qq_id, error
                    )

            try:
                await client.call_action(
                    "send_like",
                    user_id=int(qq_id),
                    times=amount,
                    self_id=self_id,
                )
            except ActionFailed as error:
                message = action_error_message(error)
                logger.warning("Failed to like QQ %s: %s", qq_id, message)
                replies.append(like_failure_reply(message))
            except Error as error:
                logger.warning("Like API unavailable for QQ %s: %s", qq_id, error)
                replies.append("点赞失败，暂时无法连接QQ协议端")
            else:
                replies.append(
                    f"给{username}点了{amount}个赞，具体到账几个以QQ显示为准~"
                )

        return "\n".join(replies)

    @filter.regex(r"^赞")
    async def like_me(self, event: AiocqhttpMessageEvent):
        messages = event.get_messages()
        command = event.message_str.strip()
        amount_match = re.search(r"(\d+)\s*$", command)
        try:
            amount = int(amount_match.group(1)) if amount_match else MAX_LIKES
        except ValueError:
            amount = MAX_LIKES + 1
        command_text = (
            command[: amount_match.start()].strip()
            if amount_match
            else command
        )

        target_mentions = [
            segment
            for segment in messages
            if isinstance(segment, Comp.At)
            and normalize_qq_id(segment.qq)
            and str(segment.qq) != str(event.get_self_id())
        ]
        command_without_mentions = command_text
        for mention in target_mentions:
            rendered_mention = f"@{mention.name or ''}({mention.qq})"
            command_without_mentions = re.sub(
                rf"\s*{re.escape(rendered_mention)}\s*",
                "",
                command_without_mentions,
                count=1,
            )

        if command_text == "赞我":
            target_ids = [event.get_sender_id()]
        elif command_without_mentions == "赞":
            target_ids = [str(mention.qq) for mention in target_mentions]
        else:
            return
        if not 1 <= amount <= MAX_LIKES:
            yield event.plain_result(f"点赞数量必须在1到{MAX_LIKES}之间")
            return
        if not target_ids:
            yield event.plain_result("用法：赞我 [数量]，或赞@用户 [数量]")
            return

        result = await self._run_like(event, target_ids, amount)
        if not result:
            return
        yield event.plain_result(result)

    @filter.llm_tool(name="like_qq_profile")
    async def like_qq_profile(
        self,
        event: AiocqhttpMessageEvent,
        target: str = "self",
        amount: int = MAX_LIKES,
    ):
        """给 QQ 名片点赞。

        Args:
            target(string): 点赞目标，可填 self、me、我，或明确的 QQ 号。未明确提供时默认给当前发言者点赞。
            amount(int): 点赞数量，范围为 1 到 20，默认为 20。
        """
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return f"点赞数量必须是1到{MAX_LIKES}之间的整数。"
        if not 1 <= amount <= MAX_LIKES:
            return f"点赞数量必须在1到{MAX_LIKES}之间。"

        normalized_target = target.strip().lower() if target else "self"
        if normalized_target in {"", "self", "me", "我", "自己", "我自己"}:
            target_ids = [event.get_sender_id()]
        else:
            qq_id = normalize_qq_id(target)
            if not qq_id:
                return "只能给当前发言者点赞，或给明确提供的 QQ 号点赞。"
            target_ids = [qq_id]

        result = await self._run_like(event, target_ids, amount)
        if not result:
            return "当前会话不允许使用点赞功能。"
        return result

    def _save_subscribed_users(self) -> None:
        self.config["subscribed_users"] = self.subscribed_users
        self.config.save_config()

    @filter.command("订阅点赞")
    async def subscribe_like(self, event: AiocqhttpMessageEvent):
        """订阅点赞"""
        sender_id = str(event.get_sender_id())
        if sender_id in self.subscribed_users:
            yield event.plain_result("你已经订阅点赞了哦~")
            return
        self.subscribed_users.append(sender_id)
        self._save_subscribed_users()
        if self.auto_like_time is None:
            yield event.plain_result("订阅成功，但自动点赞配置无效，请联系管理员检查")
            return
        scheduled_time = self.auto_like_time.strftime("%H:%M")
        yield event.plain_result(f"订阅成功！插件每天{scheduled_time}会自动为你点赞")

    @filter.command("取消订阅点赞")
    async def unsubscribe_like(self, event: AiocqhttpMessageEvent):
        """取消订阅点赞"""
        sender_id = str(event.get_sender_id())
        if sender_id not in self.subscribed_users:
            yield event.plain_result("你还没有订阅点赞哦~")
            return
        self.subscribed_users.remove(sender_id)
        self._save_subscribed_users()
        yield event.plain_result("已取消订阅！我将不再自动给你点赞")

    @filter.command("订阅点赞列表")
    async def like_list(self, event: AiocqhttpMessageEvent):
        """查看订阅点赞的用户ID列表"""

        if not self.subscribed_users:
            yield event.plain_result("当前没有订阅点赞的用户哦~")
            return
        users_str = "\n".join(self.subscribed_users)
        yield event.plain_result(f"当前订阅点赞的用户ID列表：\n{users_str}")

    @staticmethod
    async def _fetch_profile_like_users(
        client: CQHttp, self_id: str
    ) -> tuple[list[dict], int, bool]:
        users = []
        seen_users = set()

        for start in range(0, PROFILE_LIKE_MAX_ROWS, PROFILE_LIKE_PAGE_SIZE):
            data = await client.call_action(
                "get_profile_like",
                user_id=0,
                start=start,
                count=PROFILE_LIKE_PAGE_SIZE,
                self_id=self_id,
            )
            if not isinstance(data, dict):
                raise ValueError("get_profile_like returned non-object data")
            vote_info = data.get("voteInfo")
            if not isinstance(vote_info, dict):
                raise ValueError("get_profile_like response has no voteInfo")
            page = vote_info.get("userInfos", [])
            if not isinstance(page, list):
                raise ValueError("voteInfo.userInfos is not a list")

            total_count = int(vote_info.get("total_count", 0) or 0)
            for user in page:
                if not isinstance(user, dict):
                    continue
                user_key = user.get("uid") or user.get("uin")
                if not user_key or user_key in seen_users:
                    continue
                seen_users.add(user_key)
                users.append(user)

            if len(page) < PROFILE_LIKE_PAGE_SIZE:
                return users, total_count, False

        return users, total_count, True

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("谁赞了bot", alias={"谁赞了你"})
    async def get_profile_like(self, event: AiocqhttpMessageEvent):
        """获取bot自身点赞列表"""
        self_id = str(event.get_self_id())
        try:
            user_infos, total_count, truncated = (
                await self._fetch_profile_like_users(event.bot, self_id)
            )
        except Error as error:
            logger.warning("Failed to get profile likes: %s", error)
            yield event.plain_result("获取点赞列表失败，当前QQ协议端可能不支持该接口")
            return
        except (TypeError, ValueError) as error:
            logger.warning("Invalid get_profile_like response: %s", error)
            yield event.plain_result("获取点赞列表失败，协议端返回的数据格式不正确")
            return

        lines = [f"资料获赞总数：{total_count}"]
        for user in user_infos:
            try:
                count = int(user.get("count", 0) or 0)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            username = user.get("nick") or user.get("uin") or user.get("uid")
            lines.append(f"【{username}】赞了我{count}次")
        if len(lines) == 1:
            lines.append("暂无有效的点赞用户信息")
        if truncated:
            lines.append(f"列表过长，仅显示前{PROFILE_LIKE_MAX_ROWS}条记录")
        reply = "\n".join(lines)
        url = await self.text_to_image(reply)
        yield event.image_result(url)
