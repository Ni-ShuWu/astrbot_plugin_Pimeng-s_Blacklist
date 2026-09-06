"""群事件 notice 处理模块 - 处理 Bot 被禁言/被踢出事件并自动上报云黑库"""

import time
from typing import Optional, Tuple

from astrbot.api.event import AstrMessageEvent # type: ignore

from .api import PimengAPI


REPORT_COOLDOWN_SECONDS = 600

# 全员禁言时 user_id 的常见取值（OneBot/NapCat 为 0，部分实现为 2558217306）
WHOLE_GROUP_BAN_USER_IDS = {"0", "2558217306"}


class NoticeHandler:
    """群 notice 事件处理器。

    职责：
    1. 从事件中解析 OneBot notice（优先 raw_message 结构化字段，正则兜底）
    2. Bot 被禁言超过阈值 -> 上报群 + 执行禁言的管理员
    3. Bot 被踢出群 -> 上报群 + 执行踢出的管理员

    上报动作通过后台任务执行，不阻塞事件流；同一 (类型, 群, 管理员)
    在冷却期内只上报一次。
    """

    def __init__(
        self,
        api: PimengAPI,
        enable_report_on_mute: bool,
        mute_threshold_minutes: int,
        enable_report_on_kick: bool,
        report_level: int,
        logger,
    ):
        self.api = api
        self.enable_report_on_mute = enable_report_on_mute
        self.mute_threshold_seconds = max(1, int(mute_threshold_minutes)) * 60
        self.enable_report_on_kick = enable_report_on_kick
        self.report_level = max(1, min(int(report_level), 3))
        self.logger = logger

        # 防重复上报记录: {(kind, group_id, operator_id): timestamp}
        self._reported: dict = {}

    async def handle_notice(self, event: AstrMessageEvent) -> Optional[Tuple[str, str, str]]:
        """处理事件中的 notice，若命中上报条件则立即执行上报。

        Args:
            event: AstrBot 消息事件（notice 可能以消息形式透传）。

        Returns:
            上报描述字符串（用于日志），未命中则返回 None。
        """
        notice = self._parse_raw_notice(event)
        if notice is None:
            return None

        notice_type = notice.get("notice_type", "")
        sub_type = notice.get("sub_type", "")
        group_id = str(notice.get("group_id", "") or "")
        operator_id = str(notice.get("operator_id", "") or "")
        bot_id = self._get_self_id(event)

        if notice_type == "group_ban":
            # allow = 解除禁言，忽略
            if sub_type == "allow":
                return None
            if not self.enable_report_on_mute:
                return None
            # user_id 是被禁言者，仅当 Bot 被禁言时触发；全员禁言（user_id 为约定值）不触发
            muted_id = str(notice.get("user_id", ""))
            if muted_id != bot_id:
                return None
            if muted_id in WHOLE_GROUP_BAN_USER_IDS:
                return None
            duration = int(notice.get("duration", 0) or 0)
            if duration < self.mute_threshold_seconds:
                return None
            if not group_id or not operator_id:
                return None
            return await self._report(
                group_id,
                operator_id,
                f"Bot被禁言{duration // 60}分钟",
                "mute",
            )

        if notice_type == "group_decrease" and sub_type == "kick":
            if not self.enable_report_on_kick:
                return None
            # Bot 被踢出群
            if str(notice.get("user_id", "") or notice.get("member_id", "")) != bot_id:
                return None
            if not group_id or not operator_id:
                return None
            return await self._report(
                group_id,
                operator_id,
                "Bot被踢出群聊",
                "kick",
            )

        return None

    def _parse_raw_notice(self, event: AstrMessageEvent) -> Optional[dict]:
        """从事件中提取 OneBot notice 字段。

        优先级：
        1. event.message_obj.raw_message 为 dict 且 post_type == notice
        2. raw_message 为 JSON 字符串
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)

        if isinstance(raw, dict):
            if raw.get("post_type") == "notice":
                return raw
            return None

        if isinstance(raw, str) and raw.lstrip().startswith("{"):
            import json

            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("post_type") == "notice":
                    return data
            except (ValueError, TypeError):
                pass

        return None

    def _get_self_id(self, event: AstrMessageEvent) -> str:
        """获取 Bot 自身 ID。"""
        bot_id = getattr(getattr(event, "message_obj", None), "self_id", None)
        if not bot_id:
            bot_id = getattr(event, "self_id", None)
        return str(bot_id) if bot_id else ""

    def _should_report(self, kind: str, group_id: str, operator_id: str) -> bool:
        """冷却期内去重，并清理过期记录。"""
        key = (kind, group_id, operator_id)
        now = time.time()

        expired = [k for k, ts in self._reported.items() if now - ts > REPORT_COOLDOWN_SECONDS]
        for k in expired:
            self._reported.pop(k, None)

        if key in self._reported:
            return False

        self._reported[key] = now
        return True

    async def _report(self, group_id: str, operator_id: str, reason: str, kind: str) -> Optional[str]:
        """执行上报：群与管理员各一条，失败仅记录日志。

        Returns:
            上报结果描述，被冷却拦截时返回 None。
        """
        if not self._should_report(kind, group_id, operator_id):
            self.logger.debug(f"上报冷却中，跳过 | Kind: {kind} | Group: {group_id}")
            return None

        level = self.report_level
        desc = f"Kind: {kind} | Group: {group_id} | Operator: {operator_id} | Reason: {reason}"

        try:
            group_result = await self.api.add_to_blacklist(
                group_id, "group", reason, level
            )
            if not group_result.get("success"):
                self.logger.error(f"群上报失败: {group_result.get('message')} | {desc}")
            else:
                self.logger.info(f"群已上报云黑库 | {desc}")

            user_result = await self.api.add_to_blacklist(
                operator_id, "user", reason, level
            )
            if not user_result.get("success"):
                self.logger.error(f"管理员上报失败: {user_result.get('message')} | {desc}")
            else:
                self.logger.info(f"管理员已上报云黑库 | {desc}")

            return desc
        except Exception as e:
            self.logger.error(f"上报异常: {type(e).__name__}: {e} | {desc}")
            return desc
