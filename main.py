"""Pimeng Cloud Blacklist Plugin - Main entry point"""

import asyncio
from typing import Optional
from astrbot.api.event import filter, AstrMessageEvent # type: ignore
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from functools import wraps

from .api import PimengAPI
from .cache import BlacklistCache
from .service import BlacklistService
from .handler import EventHandler

__version__ = "3.0.0"

# Constants
LEVEL_NAMES = {1: "Minor", 2: "Moderate", 3: "Platform", 4: "Severe"}
LEVEL_EMOJIS = {1: "🟢", 2: "🟡", 3: "🔴", 4: "⛔"}

USER_TYPE_ALIASES = {
    "user": "user", "users": "user", "-u": "user", "u": "user", "用户": "user",
    "group": "group", "groups": "group", "-g": "group", "g": "group", "群组": "group", "群": "group",
}


def require_op(func):
    """Admin permission check decorator."""
    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        if not self._check_op(event):
            yield event.plain_result("❌ 权限不足，仅管理员可用。")
            return
        async for result in func(self, event, *args, **kwargs):
            yield result
    return wrapper


def require_token(func):
    """Bot Token check decorator."""
    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        if not self.api.bot_token:
            yield event.plain_result("❌ 未配置 Bot Token。")
            return
        async for result in func(self, event, *args, **kwargs):
            yield result
    return wrapper


class PimengBlacklistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.logger = logger
        
        self._background_tasks: set = set()
        
        # Configuration
        api_base = config.get("api_base", "https://cloudblack-api.07210700.xyz")
        bot_token = config.get("bot_token", "")
        sync_interval = max(60, min(config.get("sync_interval", 300), 3600))
        enable_auto_kick = config.get("enable_auto_kick", True)
        enable_quit_on_admin_join = config.get("enable_quit_on_admin_join", True)
        enable_message_intercept = config.get("enable_message_intercept", True)
        request_timeout = max(1, min(config.get("request_timeout", 10), 30))
        
        # Initialize modules
        self.api = PimengAPI(api_base, bot_token, request_timeout, self.logger)
        self.cache = BlacklistCache()
        self.service = BlacklistService(self.api, self.cache, sync_interval, self.logger)
        self.handler = EventHandler(self.service, self.cache, enable_auto_kick, enable_quit_on_admin_join, enable_message_intercept, self.logger)
    
    async def initialize(self):
        """Initialize the plugin."""
        if not self.api.bot_token:
            self.logger.warning("Bot Token not configured! Plugin will work in read-only mode.")
        
        await self.service.initialize()
    
    async def terminate(self):
        """Clean up resources."""
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        
        await self.service.terminate()
        await self.api.terminate()
    
    async def _safe_sync_blacklist(self):
        """Safely trigger sync (wrap async task, catch exceptions)."""
        try:
            await self.service.sync_blacklist()
        except Exception as e:
            self.logger.error(f"Background sync failed: {e}")
    
    async def _query_blacklist(self, target_id: str, query_type: str, check_rate_limit: bool = True, query_user_id: str = None) -> str:
        """Query blacklist status (generic method).
        
        Args:
            target_id: Target ID.
            query_type: Query type ("user" or "group").
            check_rate_limit: Whether to check rate limit.
            query_user_id: ID of the user performing the query.
            
        Returns:
            Query result message.
        """
        type_name = "group" if query_type == "group" else "user"
        
        if query_type == "group":
            is_blacklisted = self.service.is_group_blacklisted(target_id)
            data = self.service.get_group_data(target_id) if is_blacklisted else None
        else:
            is_blacklisted = self.service.is_user_blacklisted(target_id)
            data = self.service.get_user_data(target_id) if is_blacklisted else None
        
        if is_blacklisted:
            level = data.get("level", 1) if data else 1
            return (
                f"⚠️ {type_name} is blacklisted (local)\n"
                f"━━━━━━━━━━━━━━\n"
                f"ID: {target_id}\n"
                f"Level: {LEVEL_EMOJIS.get(level, '⚪')} {level} ({LEVEL_NAMES.get(level, 'Unknown')})\n"
                f"Reason: {data.get('reason', 'Unknown') if data else 'Unknown'}\n"
                f"Added at: {data.get('added_at', 'Unknown') if data else 'Unknown'}\n"
                f"Added by: {data.get('added_by', 'Unknown') if data else 'Unknown'}"
            )
        
        cached_result = self.service.get_cached_query(target_id, query_type)
        if cached_result:
            if cached_result.get("in_blacklist"):
                data = cached_result.get("data", {})
                return (
                    f"⚠️ {type_name} is blacklisted (cached)\n"
                    f"ID: {target_id}\n"
                    f"Level: {data.get('level', 1)}\n"
                    f"📝 From cache query"
                )
            else:
                return f"✅ {type_name} {target_id} is not blacklisted (cached)"
        
        if check_rate_limit and query_user_id:
            if not self.service.can_query_api(query_user_id):
                return f"⏳ Rate limited, please try again later"
        
        result = await self.api.check_blacklist(target_id, query_type)
        
        if check_rate_limit and query_user_id:
            self.service.update_query_time(query_user_id)
        
        self.service.set_cached_query(target_id, query_type, result)
        
        # Check if API call succeeded
        if not result.get("success"):
            error_msg = result.get('message', 'Unknown error')
            return f"❌ Query failed: {error_msg}"
        
        # API call succeeded, check if in blacklist
        if result.get("in_blacklist"):
            data = result.get("data", {})
            level = data.get('level', 1)
            message = (
                f"⚠️ {type_name} is blacklisted (real-time)\n"
                f"━━━━━━━━━━━━━━\n"
                f"ID: {target_id}\n"
                f"Level: {LEVEL_EMOJIS.get(level, '⚪')} {level} ({LEVEL_NAMES.get(level, 'Unknown')})\n"
                f"Reason: {data.get('reason', 'Unknown')}\n"
                f"Added at: {data.get('added_at', 'Unknown')}\n"
                f"Added by: {data.get('added_by', 'Unknown')}"
            )
            
            # If not in local cache, trigger incremental sync
            if not is_blacklisted:
                message += f"\n⚠️ Not in local cache, syncing..."
                task = asyncio.create_task(self._safe_sync_blacklist())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            
            return message
        else:
            return f"✅ {type_name} {target_id} is not blacklisted"
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def blacklist_interceptor(self, event: AstrMessageEvent):
        """Intercept blacklisted users."""
        message = await self.handler.handle_message(event, self.context)
        if message:
            yield event.plain_result(message)
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_member_join(self, event: AstrMessageEvent):
        """Handle member join group event."""
        await self.handler.handle_member_join(event, self.context)
    
    def _check_op(self, event: AstrMessageEvent) -> bool:
        """Check if user is admin."""
        is_admin_attr = getattr(event, 'is_admin', None)
        if is_admin_attr is None:
            return False
        # 检查是否为可调用对象（方法）
        if callable(is_admin_attr):
            return is_admin_attr()
        # 否则直接返回布尔值
        return bool(is_admin_attr)
    
    @filter.command("bl_status")
    @require_op
    async def cmd_status(self, event: AstrMessageEvent):
        """View sync status."""
        stats = self.service.get_stats()
        cache_stats = self.cache.get_cache_stats()
        
        yield event.plain_result(
            f"🛡️ 皮梦云黑库 v{__version__}\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 用户黑名单: {stats['user_blacklist']}\n"
            f"👥 群组黑名单: {stats['group_blacklist']}\n"
            f"📝 提醒记录: {cache_stats['private_warned_size']}\n"
            f"🕐 上次同步: {stats['last_sync']}\n"
            f"⏰ 下次同步: {stats['next_sync_in']}分钟后\n"
            f"🔄 同步间隔: {self.service.sync_interval // 60}分钟"
        )
    
    @filter.command("bl_sync")
    @require_op
    async def cmd_sync(self, event: AstrMessageEvent):
        """Force sync (ignore cooldown)."""
        yield event.plain_result("🔄 正在强制同步云黑库...")
        success = await self.service.sync_blacklist(force=True)
        if success:
            yield event.plain_result(
                f"✅ 同步完成\n"
                f"👤 用户: {len(self.service.user_blacklist)}\n"
                f"👥 群组: {len(self.service.group_blacklist)}"
            )
        else:
            yield event.plain_result("❌ 同步失败，请检查网络连接或API配置")
    
    @filter.command("bl_check")
    async def cmd_check(self, event: AstrMessageEvent, target: str = None, user_type: str = None):
        """Check blacklist status - query both user and group if type not specified."""
        if user_type is not None:
            user_type = self._normalize_user_type(user_type)
        
        if target is not None:
            target = str(target)
            if not target.isdigit():
                yield event.plain_result("❌ Parameter error: ID must be a number")
                return
        
        target_id = str(target or event.get_sender_id())
        
        if user_type is None:
            query_user_id = str(event.get_sender_id())

            if not self.service.can_query_api(query_user_id):
                yield event.plain_result("⏳ 查询限流中，请稍后再试")
                return

            user_result = await self._query_blacklist(target_id, "user", check_rate_limit=False)
            group_result = await self._query_blacklist(target_id, "group", check_rate_limit=False)
            
            results.append(f"[User]\n{user_result}")
            results.append(f"[Group]\n{group_result}")
            
            if can_query:
                self.service.update_query_time(query_user_id)
            
            yield event.plain_result("\n\n".join(results))
            return
        
        if user_type not in ("user", "group"):
            yield event.plain_result("❌ Parameter error: user_type must be user/group or use -u/-g")
            return
        
        result = await self._query_blacklist(target_id, user_type, check_rate_limit=True)
        yield event.plain_result(result)
    
    @filter.command("bl_add")
    @require_op
    @require_token
    async def cmd_add(self, event: AstrMessageEvent, user_id: str = None, reason: str = None, level: int = 1, user_type: str = "user"):
        """Add to blacklist - supports user and group, default user."""
        # Try to extract user_id from @mention
        at_id = self._extract_at_from_event(event)
        if at_id:
            user_id = at_id
            # Parser parameters shift when using @, re-parse from message_str
            msg = event.message_str.strip()
            for cmd_prefix in ["/bl_add ", "bl_add "]:
                if msg.startswith(cmd_prefix):
                    rest = msg[len(cmd_prefix):].strip()
                    break
            else:
                rest = msg
            parts = [p for p in rest.split() if p]
            if len(parts) >= 1:
                reason = parts[0]
            if len(parts) >= 2:
                try:
                    level = int(parts[1])
                except (ValueError, TypeError):
                    normalized = self._normalize_user_type(parts[1])
                    if normalized:
                        user_type = normalized
            if len(parts) >= 3:
                normalized = self._normalize_user_type(parts[2])
                if normalized:
                    user_type = normalized
        
        if not user_id or not reason:
            yield event.plain_result("❌ Parameter error: user_id and reason required")
            return
        
        user_id = str(user_id)
        
        try:
            level = int(level)
        except (ValueError, TypeError):
            yield event.plain_result("❌ Parameter error: level must be a number")
            return
        
        if not user_id.isdigit():
            yield event.plain_result("❌ Parameter error: user_id must be a number")
            return
        
        user_type = self._normalize_user_type(user_type) or "user"
        
        # Level range is 1-4
        if not 1 <= level <= 4:
            yield event.plain_result("❌ Parameter error: level must be between 1-4")
            return
        
        # Level 4 requires admin panel operation
        if level == 4:
            yield event.plain_result("❌ Level 4 requires admin panel operation: https://云黑.皮梦.wtf/admin")
            return
        
        result = await self.api.add_to_blacklist(user_id, user_type, reason, level)
        
        if result.get("success"):
            task = asyncio.create_task(self._safe_sync_blacklist())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            
            type_name = "user" if user_type == "user" else "group"
            yield event.plain_result(
                f"✅ Added to blacklist\n"
                f"Type: {type_name}\n"
                f"ID: {user_id}\n"
                f"Level: {LEVEL_EMOJIS.get(level, '⚪')} {level}\n"
                f"Reason: {reason}"
            )
        else:
            yield event.plain_result(f"❌ Add failed: {result.get('message', 'Unknown error')}")
    
    @filter.command("bl_remove")
    @require_op
    @require_token
    async def cmd_remove(self, event: AstrMessageEvent, user_id: str = None, reason: str = "", user_type: str = "user"):
        """Remove from blacklist - supports user and group, default user."""
        if not user_id:
            yield event.plain_result("❌ Parameter error: user_id required")
            return
        
        user_id = str(user_id)
        
        if not user_id.isdigit():
            yield event.plain_result("❌ Parameter error: user_id must be a number")
            return
        
        user_type = self._normalize_user_type(user_type) or "user"
        
        result = await self.api.remove_from_blacklist(user_id, user_type, reason or "Admin removal")
        
        if result.get("success"):
            # Remove from corresponding blacklist based on type
            if user_type == "user":
                self.service.remove_user(user_id)
                self.cache.remove_private_warn(user_id)
            else:
                self.service.remove_group(user_id)
            
            type_name = "user" if user_type == "user" else "group"
            yield event.plain_result(f"✅ Removed from blacklist: {type_name} {user_id}")
        else:
            yield event.plain_result(f"❌ Remove failed: {result.get('message', 'Unknown error')}")
    
    @filter.command("bl_list")
    @require_op
    async def cmd_list(self, event: AstrMessageEvent, page: int = 1):
        """View blacklist."""
        try:
            page = int(page)
        except (ValueError, TypeError):
            yield event.plain_result("❌ Parameter error: page must be a number")
            return
        
        # Merge user and group blacklists
        all_items = [
            (uid, data, "user") 
            for uid, data in self.service.user_blacklist.items()
        ] + [
            (uid, data, "group") 
            for uid, data in self.service.group_blacklist.items()
        ]
        
        # 使用辅助方法格式化分页
        result = self._format_blacklist_page(all_items, page)
        yield event.plain_result(result)
    
    def _format_blacklist_page(self, all_items: list, page: int, per_page: int = 15) -> str:
        """Format blacklist pagination.
        
        Args:
            all_items: All blacklist items, format: [(uid, data, type_name), ...]
            page: Current page number.
            per_page: Items per page.
            
        Returns:
            Formatted pagination message.
        """
        if not all_items:
            return "✅ Blacklist is empty"
        
        total = len(all_items)
        pages = (total + per_page - 1) // per_page
        page = min(max(page, 1), pages)  # Ensure page is within valid range
        
        start = (page - 1) * per_page
        page_items = all_items[start:start + per_page]
        
        lines = [f"📋 Blacklist ({total}) Page {page}/{pages}", "━━━━━━━━━━━━━━"]
        
        for uid, data, type_name in page_items:
            level = data.get("level", 1)
            emoji = LEVEL_EMOJIS.get(level, "⚪")
            reason = data.get('reason', 'N/A')[:12]
            lines.append(f"{emoji} [{type_name[0]}] {uid} | L{level} | {reason}...")
        
        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"Use /bl_list <page> to see more")
        
        return "\n".join(lines)
    
    def _normalize_user_type(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        return USER_TYPE_ALIASES.get(raw.lower().strip())
    
    def _extract_at_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        """Extract first @mention QQ number from event message chain."""
        try:
            message_obj = getattr(event, 'message_obj', None)
            if message_obj:
                msg_chain = getattr(message_obj, 'message', None)
                if msg_chain:
                    for comp in msg_chain:
                        if getattr(comp, 'type', None) == 'at':
                            qq = getattr(comp, 'qq', None)
                            if qq:
                                return str(qq)
        except Exception:
            pass
        return None
    
    @filter.command("bl_help")
    async def cmd_help(self, event: AstrMessageEvent):
        """Show help."""
        is_op = self._check_op(event)
        
        lines = [
            f"🛡️ 皮梦云黑库 v{__version__}",
            "━━━━━━━━━━━━━━",
            "📋 命令列表",
            "━━━━━━━━━━━━━━",
            "/bl_check [ID] [类型] - 检查黑名单状态",
        ]
        
        if is_op:
            lines.extend([
                "━━━━━━━━━━━━━━",
                "🔐 管理员命令",
                "/bl_status - 查看同步状态",
                "/bl_sync - 强制同步",
                "/bl_add <ID> <原因> [等级] [类型] - 添加黑名单",
                "/bl_remove <ID> [原因] [类型] - 移除黑名单",
                "/bl_list [页码] - 查看列表",
            ])
        
        lines.extend([
            "━━━━━━━━━━━━━━",
            "📝 参数说明",
            "━━━━━━━━━━━━━━",
            "• ID: QQ号或群号，支持 @提及 提取",
            "• 类型: user / -u / 用户（默认）| group / -g / 群组",
            "• 等级: 1-轻微 2-一般 3-平台 4-严重（等级4需面板操作）",
            "━━━━━━━━━━━━━━",
            "🌐 申诉: https://云黑.皮梦.wtf",
            f"👤 身份: {'管理员' if is_op else '用户'}",
        ])
        
        yield event.plain_result("\n".join(lines))