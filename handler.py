"""事件处理模块 - 处理消息拦截、踢人权限检查等事件"""

import re
from datetime import datetime
from typing import Optional, Set

from astrbot.api.event import AstrMessageEvent

from .service import BlacklistService
from .cache import BlacklistCache


class EventHandler:
    """事件处理器"""
    
    def __init__(self, service: BlacklistService, cache: BlacklistCache, enable_auto_kick: bool, enable_quit_on_admin_join: bool, enable_message_intercept: bool, logger):
        self.service = service
        self.cache = cache
        self.enable_auto_kick = enable_auto_kick
        self.enable_quit_on_admin_join = enable_quit_on_admin_join
        self.enable_message_intercept = enable_message_intercept
        self.logger = logger
        
        self.BOT_JOIN_KEYWORDS = ["邀请", "加入了群聊", "加入了", "加入群", "加群"]
        
        self.quit_groups: Set[str] = set()
    
    async def handle_message(self, event: AstrMessageEvent, context) -> Optional[str]:
        """处理消息事件 - 返回需要发送的消息，或None表示不发送"""
        user_id = str(event.get_sender_id())
        
        is_group = hasattr(event, 'get_group_id') and event.get_group_id() is not None
        
        is_wake_up = False
        if hasattr(event, 'is_wake_up'):
            is_wake_up = event.is_wake_up()
        
        if self.service.is_user_blacklisted(user_id):
            user_data = self.service.get_user_data(user_id)
            level = user_data.get("level", 1) if user_data else 1

            if not is_group:
                # 1级不进行拦截输出，直接放行
                if level <= 1:
                    return None

                now = datetime.now()
                last_warn = self.cache.get_private_warn_time(user_id)

                reason = user_data.get('reason', '未知') if user_data else '未知'
                added_by = user_data.get('added_by', None) if user_data else None
                added_by_line = f"\n添加者：{added_by}" if added_by else ""

                should_warn = False
                if is_wake_up:
                    if last_warn is None or last_warn.date() != now.date():
                        should_warn = True
                        self.cache.set_private_warn_time(user_id, now)
                        self.logger.info(f"Private Warn | User: {user_id} | Level: {level}")

                event.stop_event()

                if should_warn:
                    return (
                        f"⚠️ 您已被列入云黑名单。\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"违规等级: {level}\n"
                        f"原因: {reason}{added_by_line}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"详情与申诉: https://云黑.皮梦.wtf"
                    )

                return None
        
        if is_group:
            group_id = str(event.get_group_id())
            
            if self.service.is_group_blacklisted(group_id):
                group_data = self.service.get_group_data(group_id)
                group_level = group_data.get("level", 1) if group_data else 1
                
                self.logger.info(f"Bot in Blacklisted Group | Group: {group_id} | Level: {group_level}")
                
                await self._quit_group_if_possible(group_id, context)
                event.stop_event()
                return None
            
            if self.service.is_user_blacklisted(user_id):
                user_data = self.service.get_user_data(user_id)
                user_level = user_data.get("level", 1) if user_data else 1
                
                reason = user_data.get('reason', '未知') if user_data else '未知'
                added_by = user_data.get('added_by', None) if user_data else None
                added_by_line = f"\n添加者：{added_by}" if added_by else ""
                
                if self.enable_auto_kick and user_level >= 3:
                    can_kick = await self._check_kick_permission(event, group_id, user_id)

                    if can_kick:
                        kicked = await self._kick_user(group_id, user_id, event, context)
                        if kicked:
                            self.logger.info(f"Kicked blacklisted user | User: {user_id} | Level: {user_level} | Group: {group_id}")
                            return (
                                f"⚠️ 已踢出云黑用户\n"
                                f"━━━━━━━━━━━━━━\n"
                                f"用户：{user_id}\n"
                                f"等级：{user_level}\n"
                                f"原因：{reason}{added_by_line}"
                            )

                # 1级不进行拦截输出，2~4级才拦截并提示
                if user_level <= 1:
                    return None

                if is_wake_up and self.enable_message_intercept:
                    event.stop_event()
                    self.logger.info(f"Intercepted blacklisted user | User: {user_id} | Level: {user_level} | Group: {group_id}")
                    return (
                        f"⚠️ 云黑用户已被拦截\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"用户：{user_id}\n"
                        f"等级：{user_level}\n"
                        f"原因：{reason}{added_by_line}"
                    )
        
        return None
    
    async def handle_member_join(self, event: AstrMessageEvent, context) -> bool:
        """处理成员加入群组事件 - 返回True表示处理了事件
        
        处理两种类型的加入事件：
        1. Bot加入群聊：检查群组是否在黑名单中，如果是则Bot退群
        2. 普通成员加入群聊：检查成员是否在黑名单中，如果是且等级≥3，则尝试踢出
        """
        group_id = str(event.get_group_id())
        message_str = getattr(event, 'message_str', '') or ''
        
        is_system_notification = False
        system_patterns = ["邀请", "加入了群聊", "通过扫描", "通过分享", "通过搜索", "加入了群", "加入群聊"]
        if any(pattern in message_str for pattern in system_patterns):
            if '@' not in message_str and len(message_str) < 150:
                is_system_notification = True
        
        if not is_system_notification:
            return False
        
        joined_user_id = self._extract_user_id_from_message(message_str)
        
        if not joined_user_id:
            self.logger.debug(f"无法从消息中提取用户ID: {message_str}")
            return False
        
        is_bot_join = self._is_bot_join_message(event, joined_user_id)
        
        if is_bot_join:
            return await self._handle_bot_join(group_id, context)
        else:
            return await self._handle_member_join(group_id, joined_user_id, event, context)
    
    def _is_bot_join_message(self, event: AstrMessageEvent, extracted_user_id: str) -> bool:
        bot_id = None
        
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'self_id'):
            bot_id = str(event.message_obj.self_id)
        
        if not bot_id:
            bot_id = getattr(event, 'self_id', None)
            if bot_id:
                bot_id = str(bot_id)
        
        if not bot_id and hasattr(event, 'bot'):
            bot_id = getattr(event.bot, 'self_id', None)
            if bot_id:
                bot_id = str(bot_id)
        
        if bot_id and extracted_user_id:
            is_bot = (bot_id == extracted_user_id)
            if is_bot:
                self.logger.debug(f"Bot joined group | Bot: {bot_id}")
            return is_bot
        
        invite_pattern = r'(\d{5,10})\s*邀请\s*(\d{5,10})'
        if re.search(invite_pattern, getattr(event, 'message_str', '')):
            return False
        
        return False
    
    def _extract_user_id_from_message(self, message_str: str) -> Optional[str]:
        """从入群消息中提取用户ID
        
        改进的提取逻辑：
        1. 优先匹配"用户123456"格式
        2. 验证提取的数字是否在合理范围内
        3. 避免误匹配消息中其他位置的数字
        """
        if not message_str:
            return None
        
        pattern1 = r'用户\s*(\d{5,10})'
        match1 = re.search(pattern1, message_str)
        if match1:
            qq = match1.group(1)
            if self._is_valid_qq_number(qq):
                return qq
        
        pattern2 = r'^(\d{5,10})'
        match2 = re.match(pattern2, message_str)
        if match2:
            qq = match2.group(1)
            if self._is_valid_qq_number(qq):
                return qq
        
        pattern3 = r'(\d{5,10})\s*(?:加入了|邀请)'
        match3 = re.search(pattern3, message_str)
        if match3:
            qq = match3.group(1)
            if self._is_valid_qq_number(qq):
                return qq
        
        qq_pattern = r'(\d{5,10})'
        matches = re.findall(qq_pattern, message_str)
        for qq in matches:
            if self._is_valid_qq_number(qq):
                return qq
        
        return None
    
    def _is_valid_qq_number(self, qq: str) -> bool:
        """验证QQ号是否有效
        
        QQ号特点：
        1. 5-10位数字
        2. 最小QQ号约为10000
        """
        if not qq or len(qq) < 5 or len(qq) > 10:
            return False
        
        try:
            qq_int = int(qq)
            if qq_int < 10000:
                return False
            if qq_int > 9999999999:
                return False
            return True
        except ValueError:
            return False
    
    async def _kick_user(self, group_id: str, user_id: str, event: AstrMessageEvent = None, context = None) -> bool:
        """统一踢人方法
        
        尝试使用多种方式踢人：
        1. event.bot.set_group_kick (推荐)
        2. context.set_group_kick
        3. context.kick_group_member
        
        Returns:
            bool: 是否成功踢出
        """
        try:
            if event and hasattr(event, 'bot') and hasattr(event.bot, 'set_group_kick'):
                await event.bot.set_group_kick(
                    group_id=int(group_id),
                    user_id=int(user_id)
                )
                return True
            
            if context and hasattr(context, 'set_group_kick'):
                await context.set_group_kick(group_id=group_id, user_id=user_id)
                return True
            
            if context and hasattr(context, 'kick_group_member'):
                await context.kick_group_member(group_id, user_id)
                return True
            
            return False
        except Exception as e:
            self.logger.error(f"Kick failed | User: {user_id} | Group: {group_id} | Error: {e}")
            return False
    
    async def _handle_bot_join(self, group_id: str, context) -> bool:
        """处理Bot加入群聊事件"""
        if not self.service.is_group_blacklisted(group_id):
            return False
        
        group_data = self.service.get_group_data(group_id)
        level = group_data.get("level", 1) if group_data else 1
        
        self.logger.info(f"Bot joined blacklisted group | Group: {group_id} | Level: {level}")
        
        return await self._quit_group_if_possible(group_id, context)
    
    async def _get_user_group_role(self, event: AstrMessageEvent, group_id: str, user_id: str) -> Optional[str]:
        """获取用户在群组中的角色（owner/admin/member）"""
        bot = getattr(event, 'bot', None)
        if not bot or not hasattr(bot, 'api') or not hasattr(bot.api, 'call_action'):
            return None
        
        try:
            user_info = await bot.api.call_action(
                'get_group_member_info',
                user_id=int(user_id),
                group_id=int(group_id),
                no_cache=True
            )
            return user_info.get("role", "member")
        except Exception:
            return None
    
    async def _quit_group_if_possible(self, group_id: str, context) -> bool:
        """尝试退出群组，带防重入和错误处理"""
        if group_id in self.quit_groups:
            self.logger.debug(f"Already quit group, ignoring | Group: {group_id}")
            return True
        
        self.quit_groups.add(group_id)
        
        try:
            if hasattr(context, 'quit_group'):
                await context.quit_group(group_id)
                self.logger.info(f"Bot quit group | Group: {group_id}")
                return True
        except Exception as e:
            self.logger.error(f"Quit group failed | Group: {group_id} | Error: {e}")
            self.quit_groups.discard(group_id)
        
        return False
    
    async def _handle_member_join(self, group_id: str, user_id: str, event: AstrMessageEvent, context) -> bool:
        """处理普通成员加入群聊事件
        
        1. 黑名单用户为群主/管理员 → Bot自动退群
        2. 黑名单用户等级≥3且Bot有权限 → 踢出
        3. 黑名单用户等级<3 或 无权限 → 仅记录日志
        """
        if not self.service.is_user_blacklisted(user_id):
            return False
        
        user_data = self.service.get_user_data(user_id)
        level = user_data.get("level", 1) if user_data else 1
        
        self.logger.info(f"Blacklisted user joined group | Group: {group_id} | User: {user_id} | Level: {level}")
        
        user_role = await self._get_user_group_role(event, group_id, user_id)
        
        if self.enable_quit_on_admin_join and user_role in ("owner", "admin"):
            self.logger.warning(f"Blacklisted user is {user_role} of group, bot will quit | Group: {group_id} | User: {user_id}")
            await self._quit_group_if_possible(group_id, context)
            return True
        
        if not self.enable_auto_kick:
            self.logger.debug(f"Auto kick disabled, skipping | User: {user_id} | Group: {group_id}")
            return False
        
        if level < 3:
            self.logger.debug(f"User level too low, not kicking | User: {user_id} | Level: {level}")
            return False
        
        if not await self._check_kick_permission(event, group_id, user_id):
            self.logger.warning(f"No permission to kick user | Group: {group_id} | User: {user_id}")
            return False
        
        kicked = await self._kick_user(group_id, user_id, event, context)
        if kicked:
            self.logger.info(f"Kicked blacklisted user | Group: {group_id} | User: {user_id} | Level: {level}")
        
        return kicked
    
    async def _check_kick_permission(self, event: AstrMessageEvent, group_id: str, user_id: str, fallback: bool = False) -> bool:
        """检查 Bot 是否有权限踢出对方

        Args:
            fallback: 当API调用失败时是否允许降级踢人（跳过权限检查）

        返回 True 表示可以踢出，False 表示不能踢出
        检查逻辑：
        1. Bot 必须是管理员或群主
        2. 对方不能是管理员或群主

        默认 fallback=False 以保证安全：无法确认权限时拒绝踢人。
        """
        bot = getattr(event, 'bot', None)
        if not bot:
            self.logger.warning(f"无法获取 Bot 实例")
            return fallback

        if not hasattr(bot, 'api') or not hasattr(bot.api, 'call_action'):
            self.logger.warning(f"Bot 不支持 API 调用")
            return fallback

        bot_id = None
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'self_id'):
            bot_id = event.message_obj.self_id

        if not bot_id:
            bot_id = getattr(event, 'self_id', None)

        if not bot_id and hasattr(bot, 'self_id'):
            bot_id = getattr(bot, 'self_id', None)

        if not bot_id:
            self.logger.warning(f"无法获取 Bot ID | Group: {group_id}")
            return fallback

        try:
            bot_info = await bot.api.call_action(
                'get_group_member_info',
                user_id=int(bot_id),
                group_id=int(group_id),
                no_cache=True
            )
            user_info = await bot.api.call_action(
                'get_group_member_info',
                user_id=int(user_id),
                group_id=int(group_id),
                no_cache=True
            )
        except Exception as e:
            self.logger.warning(f"群成员信息获取失败，拒绝踢人: {type(e).__name__}: {str(e)} | Group: {group_id} | User: {user_id}")
            return fallback

        bot_role = bot_info.get("role", "member")
        user_role = user_info.get("role", "member")

        if bot_role not in ["admin", "owner"]:
            self.logger.warning(f"Bot 不是管理员，无法踢人 | Bot: {bot_id} | Role: {bot_role} | Group: {group_id}")
            return False

        if user_role in ["admin", "owner"]:
            self.logger.warning(f"对方是管理员/群主，无法踢出 | User: {user_id} | Role: {user_role} | Group: {group_id}")
            return False

        return True
