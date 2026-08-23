import asyncio
import logging
import re
from datetime import date
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
PROFILE_LIKE_PAGE_SIZE = 50
PROFILE_LIKE_MAX_ROWS = 1000


def normalize_qq_id(value: object) -> Optional[str]:
    qq_id = str(value).strip()
    if not qq_id.isascii() or not qq_id.isdecimal() or int(qq_id) <= 0:
        return None
    return qq_id


def normalize_qq_ids(values: Iterable[object]) -> list[str]:
    normalized = (normalize_qq_id(value) for value in values)
    return list(dict.fromkeys(qq_id for qq_id in normalized if qq_id))


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
    "发送 赞我 自动点赞",
    "2.0.0",
    "https://github.com/Futureppo/astrbot_plugin_zanwo",
)
class ZanwoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._auto_like_tasks: set[asyncio.Task] = set()
        self.white_list_groups = set(
            normalize_qq_ids(config.get("white_list_groups", []))
        )
        self.subscribed_users = normalize_qq_ids(
            config.get("subscribed_users", [])
        )
        self.last_auto_like_date: Optional[str] = config.get("zanwo_date")

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

    def _schedule_auto_like(self, client: CQHttp, self_id: str) -> None:
        today = date.today().isoformat()
        if not self.subscribed_users or self.last_auto_like_date == today:
            return
        self.last_auto_like_date = today
        self.config["zanwo_date"] = today
        self.config.save_config()
        task = asyncio.create_task(
            self._like(
                client, list(self.subscribed_users), self_id, MAX_LIKES
            )
        )
        self._auto_like_tasks.add(task)
        task.add_done_callback(self._handle_auto_like_task)

    def _handle_auto_like_task(self, task: asyncio.Task) -> None:
        self._auto_like_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Auto-like task failed")

    async def terminate(self) -> None:
        tasks = list(self._auto_like_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _like(
        self, client: CQHttp, ids: list[str], self_id: str, amount: int
    ) -> str:
        """向有效且不重复的 QQ 号发送名片赞。"""
        replies = []
        seen_ids: set[str] = set()
        for raw_id in ids:
            qq_id = normalize_qq_id(raw_id)
            if not qq_id:
                invalid_value = str(raw_id).strip() or "空值"
                replies.append(f"无效的QQ号：{invalid_value}")
                continue
            if qq_id in seen_ids:
                continue
            seen_ids.add(qq_id)

            username = qq_id
            try:
                user_info = await client.call_action(
                    "get_stranger_info", user_id=int(qq_id), self_id=self_id
                )
                if isinstance(user_info, dict):
                    username = user_info.get("nickname") or qq_id
            except Error as error:
                logger.warning("Failed to get nickname for QQ %s: %s", qq_id, error)

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

    @filter.regex(r"^赞.*")
    async def like_me(self, event: AiocqhttpMessageEvent):
        messages = event.get_messages()
        command = event.message_str.strip()
        amount_match = re.search(r"(\d+)\s*$", command)
        amount = int(amount_match.group(1)) if amount_match else MAX_LIKES
        command_text = (
            command[: amount_match.start()].strip()
            if amount_match
            else command
        )
        if not 1 <= amount <= MAX_LIKES:
            yield event.plain_result(f"点赞数量必须在1到{MAX_LIKES}之间")
            return

        if command_text == "赞我":
            target_ids = [event.get_sender_id()]
        else:
            self_id = str(event.get_self_id())
            target_ids = [
                str(segment.qq)
                for segment in messages
                if isinstance(segment, Comp.At) and str(segment.qq) != self_id
            ]
        if not target_ids:
            yield event.plain_result("用法：赞我 [数量]，或赞@用户 [数量]")
            return

        result = await self._run_like(event, target_ids, amount)
        if not result:
            return
        yield event.plain_result(result)
        self._schedule_auto_like(event.bot, str(event.get_self_id()))

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
        self._schedule_auto_like(event.bot, str(event.get_self_id()))
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
        yield event.plain_result("订阅成功！插件每天触发时会自动为你点赞")

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
