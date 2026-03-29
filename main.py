from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import asyncio
import http.client
import json
from typing import Dict, Set, List
from datetime import datetime, timedelta

__version__ = "2.6.0"

@register(
    "pimeng_blacklist",
    "N(Ni-ShuWu),P(Pimeng's)",
    "基于皮梦云黑库接入插件，可查询用户是否在黑名单中",
    __version__
)
class PimengBlacklistPlugin(Star):
    # 正确接收 config 参数
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.logger = logger
        self.config = config  # 保存配置对象
        
        # 直接从配置中读取（AstrBot 会将 _conf_schema.json 定义的配置直接传入）
        self.api_base = config.get("api_base", "https://cloudblack-api.07210700.xyz").rstrip("/")
        self.bot_token = config.get("bot_token", "")
        self.sync_interval = config.get("sync_interval", 300)
        self.clean_cache_max = config.get("clean_cache_max", 10000)
        self.enable_auto_kick = config.get("enable_auto_kick", True)
        self.request_timeout = config.get("request_timeout", 10)
        
        self.last_sync = None
        self.sync_task = None
        
        # 本地云黑库
        self.blacklist_db: Dict[str, dict] = {}
        
        # 用户黑名单（仅拦截用户）
        self.user_blacklist: Dict[str, dict] = {}
        
        # 群组黑名单（仅踢出群组）
        self.group_blacklist: Dict[str, dict] = {}
        
        self.clean_cache: Set[str] = set()
        
        # 私聊提醒记录 {user_id: last_warn_time}
        self.private_warned: Dict[str, datetime] = {}
        
        # 群聊提醒记录 {user_id: {group_id: last_warn_time}}
        self.group_warned: Dict[str, Dict[str, datetime]] = {}
        
        # 新用户首次发言记录 {user_id: {group_id: bool}}
        self.new_user_warned: Dict[str, Dict[str, bool]] = {}
        
    async def initialize(self):
        """初始化"""
        if not self.bot_token:
            self.logger.warning("Bot Token not configured! Plugin will work in read-only mode.")
        
        await self._sync_blacklist()
        self.sync_task = asyncio.create_task(self._scheduled_sync())
        
        self.logger.info(f"PimengBlacklist v{__version__} | DB: {len(self.blacklist_db)} | Sync: {self.sync_interval//60}min")
    
    async def terminate(self):
        """清理资源"""
        if self.sync_task:
            self.sync_task.cancel()
            try:
                await self.sync_task
            except asyncio.CancelledError:
                pass
    
    async def _scheduled_sync(self):
        """定时同步"""
        while True:
            try:
                await asyncio.sleep(self.sync_interval)
                await self._sync_blacklist()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync error: {e}")
                await asyncio.sleep(60)
    
    async def _sync_blacklist(self):
        """同步云黑库"""
        if not self.bot_token:
            self.logger.warning("Bot Token not configured! Sync skipped.")
            return
        
        try:
            self.logger.info("Starting sync with cloud blacklist API...")
            result = await self._api_request("GET", "/api/bot/getlist")
            
            if not result.get("success"):
                error_msg = result.get('message', 'Unknown error')
                self.logger.error(f"Sync failed: {error_msg}")
                return
            
            data = result.get("data", {})
            remote_list = data.get("blacklist", [])
            
            if not isinstance(remote_list, list):
                self.logger.error("Sync failed: Invalid blacklist format from API")
                return
            
            old_count = len(self.user_blacklist) + len(self.group_blacklist)
            self.user_blacklist.clear()
            self.group_blacklist.clear()
            self.clean_cache.clear()
            
            for item in remote_list:
                user_id = str(item.get("user_id", ""))
                user_type = item.get("user_type", "user")
                
                if user_id:
                    if user_type == "group":
                        # 群组黑名单
                        self.group_blacklist[user_id] = {
                            "level": item.get("level", 1),
                            "reason": item.get("reason", "Unknown"),
                            "added_at": item.get("added_at", "Unknown"),
                            "added_by": item.get("added_by", "Unknown"),
                            "user_type": "group"
                        }
                    else:
                        # 用户黑名单
                        self.user_blacklist[user_id] = {
                            "level": item.get("level", 1),
                            "reason": item.get("reason", "Unknown"),
                            "added_at": item.get("added_at", "Unknown"),
                            "added_by": item.get("added_by", "Unknown"),
                            "user_type": "user"
                        }
            
            # 清理已不在黑名单中的用户的提醒记录
            current_users = set(self.user_blacklist.keys())
            current_groups = set(self.group_blacklist.keys())
            
            # 清理私聊提醒记录
            private_warned_users = set(self.private_warned.keys())
            for user_id in private_warned_users - current_users:
                self.private_warned.pop(user_id, None)
            
            # 清理群聊提醒记录
            group_warned_users = set(self.group_warned.keys())
            for user_id in group_warned_users - current_users:
                self.group_warned.pop(user_id, None)
            
            # 清理新用户首次发言记录
            new_user_warned_users = set(self.new_user_warned.keys())
            for user_id in new_user_warned_users - current_users:
                self.new_user_warned.pop(user_id, None)
            
            self.last_sync = datetime.now()
            new_count = len(self.blacklist_db)
            
            self.logger.info(f"Sync OK | {old_count} -> {new_count} users")
            
        except Exception as e:
            self.logger.error(f"Sync exception: {e}")
            self.logger.error(f"API URL: {self.api_base}/api/bot/getlist")
    
    # 使用消息拦截装饰器
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def blacklist_interceptor(self, event: AstrMessageEvent):
        """拦截云黑用户"""
        user_id = str(event.get_sender_id())
        
        # 判断是否为群聊
        is_group = hasattr(event, 'get_group_id') and event.get_group_id() is not None
        
        # 检查干净缓存
        if user_id in self.clean_cache:
            return
        
        # 检查云黑库
        # 检查用户黑名单（仅拦截用户）
        if user_id in self.user_blacklist:
            data = self.user_blacklist[user_id]
            level = data.get("level", 1)
            
            # 判断是否主动唤醒bot（检查是否@bot或使用唤醒词）
            is_wake_up = False
            if hasattr(event, 'is_wake_up'):
                is_wake_up = event.is_wake_up()
            
            # 私聊模式：全部拦截，仅主动唤醒时每天提醒一次
            if not is_group:
                now = datetime.now()
                last_warn = self.private_warned.get(user_id)
                
                # 检查是否需要提醒（仅主动唤醒时）
                should_warn = False
                if is_wake_up:
                    # 检查是否在同一天提醒过
                    if last_warn is None or last_warn.date() != now.date():
                        # 首次提醒或新的一天，提醒
                        should_warn = True
                        self.private_warned[user_id] = now
                        self.logger.info(f"Private Warn | User: {user_id} | Level: {level}")
                
                if should_warn:
                    # 发送提醒并拦截消息
                    yield event.plain_result(
                        f"⚠️ 您已被列入云黑名单。\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"违规等级: {level}\n"
                        f"原因: {data.get('reason', '未知')}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"详情与申诉: https://云黑.皮梦.wtf"
                    )
                
                # 停止事件传播，拦截消息
                event.stop_event()
                return
        
        # 检查群组黑名单（仅用于自动踢出和退群）
        if is_group:
            group_id = str(event.get_group_id())
            
            # 检查群组是否在黑名单中
            if group_id in self.group_blacklist:
                group_data = self.group_blacklist[group_id]
                group_level = group_data.get("level", 1)
                
                # Bot在黑名单群组中，自动退群
                self.logger.info(f"Bot in Blacklisted Group | Group: {group_id} | Level: {group_level}")
                
                # 尝试退群
                try:
                    # 尝试使用context的方法
                    if hasattr(self.context, 'quit_group'):
                        await self.context.quit_group(group_id)
                        self.logger.info(f"Bot Quit Group | Group: {group_id}")
                except Exception as e:
                    self.logger.error(f"Quit Group Failed | Group: {group_id} | Error: {e}")
                
                # 停止事件传播
                event.stop_event()
                return
        
        # 检查群组黑名单（仅用于自动退群）
        if is_group:
            group_id = str(event.get_group_id())
            
            # 检查群组是否在黑名单中
            if group_id in self.group_blacklist:
                data = self.group_blacklist[group_id]
                level = data.get("level", 1)
                
                # 自动退出黑名单群组
                if self.enable_auto_kick and level >= 3:
                    try:
                        await self._kick_member(group_id, data.get("user_id", ""))
                        self.logger.info(f"Auto Kick Group | Group: {group_id} | Level: {level}")
                    except Exception as e:
                        self.logger.error(f"Kick Group Failed | Group: {group_id} | Error: {e}")
        
        # 加入干净缓存
        if len(self.clean_cache) >= self.clean_cache_max:
            self.clean_cache = set(list(self.clean_cache)[self.clean_cache_max//2:])
        self.clean_cache.add(user_id)
    
    def _check_op(self, event: AstrMessageEvent) -> bool:
        # 检查事件发送者是否为管理员
        # 使用 AstrBot 内置的管理员系统
        return hasattr(event, 'is_admin') and event.is_admin()
    

    
    @filter.command("bl_status")
    async def cmd_status(self, event: AstrMessageEvent):
        '''同步状态'''
        if not self._check_op(event):
            yield event.plain_result("❌ Permission denied. OP only.")
            return
        
        if not self.last_sync:
            yield event.plain_result("❌ First sync not completed.")
            return
        
        next_sync = self.last_sync + timedelta(seconds=self.sync_interval)
        time_left = next_sync - datetime.now()
        
        yield event.plain_result(
            f"皮梦云黑库 v{__version__}\n"
            f"━━━━━━━━━━━━━━\n"
            f"用户黑名单: {len(self.user_blacklist)} 用户\n"
            f"群组黑名单: {len(self.group_blacklist)} 群组\n"
            f"干净缓存: {len(self.clean_cache)} 用户\n"
            f"上次同步: {self.last_sync.strftime('%H:%M:%S')}\n"
            f"下次同步: {max(0, time_left.seconds//60)}分钟\n"
            f"同步间隔: {self.sync_interval//60}分钟"
        )
    
    @filter.command("bl_sync")
    async def cmd_sync(self, event: AstrMessageEvent):
        '''强制同步'''
        if not self._check_op(event):
            yield event.plain_result("❌ Permission denied. OP only.")
            return
        
        yield event.plain_result("🔄 正在同步...")
        await self._sync_blacklist()
        
        yield event.plain_result(
            f"✅ 同步完成\n"
            f"数据库大小: {len(self.blacklist_db)} 用户"
        )
    
    @filter.command("bl_check")
    async def cmd_check(self, event: AstrMessageEvent, target: str = None, user_type: str = "user"):
        '''检查状态'''
        # 参数验证：如果提供了target，转换为字符串后再验证
        if target is not None:
            target = str(target)
            if not target.isdigit():
                yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。QQ号必须是数字。该指令完整参数: target(str)=,user_type(str)=user")
                return
        
        # 验证 user_type 参数
        if user_type not in ["user", "group"]:
            yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。user_type 必须是 'user' 或 'group'。该指令完整参数: target(str)=,user_type(str)=user")
            return
        
        # 查询前先更新云黑库
        await self._sync_blacklist()
        
        target_id = str(target or event.get_sender_id())
        
        # 根据类型选择黑名单
        if user_type == "group":
            blacklist = self.group_blacklist
            type_name = "群组"
        else:
            blacklist = self.user_blacklist
            type_name = "用户"
        
        # 检查本地黑名单
        if target_id in blacklist:
            data = blacklist[target_id]
            level = data.get("level", 1)
            level_names = {1: "轻微", 2: "一般", 3: "平台", 4: "严重"}
            
            yield event.plain_result(
                f"⚠️ 黑名单检查 (本地)\n"
                f"━━━━━━━━━━━━━━\n"
                f"{type_name}: {target_id}\n"
                f"状态: 🚫 已拉黑\n"
                f"等级: {level} ({level_names.get(level, '未知')})\n"
                f"原因: {data.get('reason', '未知')}\n"
                f"添加时间: {data.get('added_at', '未知')}\n"
                f"添加者: {data.get('added_by', '未知')}"
            )
        else:
            # 实时检查
            result = await self._api_check(target_id, user_type)
            
            if result.get("in_blacklist"):
                data = result.get("data", {})
                yield event.plain_result(
                    f"⚠️ 黑名单检查 (实时)\n"
                    f"{type_name}: {target_id}\n"
                    f"状态: 🚫 已拉黑\n"
                    f"等级: {data.get('level', 1)}\n"
                    f"⚠️ 不在本地缓存，将在一段时间内同步"
                )
            else:
                yield event.plain_result(
                    f"✅ 黑名单检查\n"
                    f"{type_name}: {target_id}\n"
                    f"状态: 未被拉黑"
                )
    
    @filter.command("bl_add")
    async def cmd_add(self, event: AstrMessageEvent, user_id: str = None, reason: str = None, level: int = 1):
        '''添加到黑名单'''
        # 参数验证
        if user_id is None or reason is None:
            yield event.plain_result("❌ 插件 pimeng_blacklist: 必要参数缺失。该指令完整参数: user_id(str),reason(str),level(int)=1")
            return
        
        # 将 user_id 转换为字符串（AstrBot 可能会将纯数字解析为 int）
        user_id = str(user_id)
        
        # 将 level 转换为整数（AstrBot 可能会解析为字符串）
        try:
            level = int(level)
        except (ValueError, TypeError):
            yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。等级必须是数字。")
            return
        
        # 参数类型验证
        if not user_id.isdigit():
            yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。QQ号必须是数字。")
            return
        
        if level < 1 or level > 4:
            yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。等级必须在1-3之间（等级4需在管理面板操作）。")
            return
        
        if not self._check_op(event):
            yield event.plain_result("❌ 权限不足，仅管理员可用。")
            return
        
        if not self.bot_token:
            yield event.plain_result("❌ 未配置 Bot Token。")
            return
        
        if level >= 4:
            yield event.plain_result("❌ 等级 4 需要在管理面板操作: https://云黑.皮梦.wtf/admin")
            return
        
        result = await self._api_request("POST", "/api/bot/add", {
            "user_id": user_id,
            "user_type": "user",
            "reason": reason,
            "level": level
        })
        
        if result.get("success"):
            self.blacklist_db[user_id] = {
                "level": level,
                "reason": reason,
                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "added_by": f"op:{event.get_sender_id()}",
                "user_type": "user"
            }
            self.clean_cache.discard(user_id)
            
            # 添加后同步云黑库
            await self._sync_blacklist()
            
            yield event.plain_result(
                f"✅ 已添加到黑名单\n"
                f"用户: {user_id} | 等级: {level}\n"
                f"原因: {reason}\n"
                f"立即生效"
            )
        else:
            yield event.plain_result(f"❌ 失败: {result.get('message', '未知错误')}")
    
    @filter.command("bl_remove")
    async def cmd_remove(self, event: AstrMessageEvent, user_id: str = None, reason: str = ""):
        '''从黑名单移除'''
        # 参数验证
        if user_id is None:
            yield event.plain_result("❌ 插件 pimeng_blacklist: 必要参数缺失。该指令完整参数: user_id(str),reason(str)=")
            return
        
        # 将 user_id 转换为字符串（AstrBot 可能会将纯数字解析为 int）
        user_id = str(user_id)
        
        # 参数类型验证
        if not user_id.isdigit():
            yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。QQ号必须是数字。")
            return
        
        if not self._check_op(event):
            yield event.plain_result("❌ 权限不足，仅管理员可用。")
            return
        
        if not self.bot_token:
            yield event.plain_result("❌ 未配置 Bot Token。")
            return
        
        result = await self._api_request("POST", "/api/bot/delete", {
            "user_id": user_id,
            "user_type": "user",
            "reason": reason or "OP removed"
        })
        
        if result.get("success"):
            existed = self.blacklist_db.pop(user_id, None)
            
            # 清理该用户的所有提醒记录
            self.private_warned.pop(user_id, None)
            self.group_warned.pop(user_id, None)
            self.new_user_warned.pop(user_id, None)
            
            # 删除后同步云黑库
            await self._sync_blacklist()
            
            yield event.plain_result(
                f"✅ 已从黑名单移除\n"
                f"用户: {user_id}\n"
                f"{'立即生效' if existed else '将很快同步'}"
            )
        else:
            yield event.plain_result(f"❌ 失败: {result.get('message', '未知错误')}")
    
    @filter.command("bl_list")
    async def cmd_list(self, event: AstrMessageEvent, page: int = 1):
        '''查看黑名单'''
        # 将 page 转换为整数（AstrBot 可能会解析为字符串）
        try:
            page = int(page)
        except (ValueError, TypeError):
            yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。页码必须是数字。该指令完整参数: page(int)=1")
            return
        
        # 参数验证：page必须是正整数
        if page < 1:
            yield event.plain_result("❌ 插件 pimeng_blacklist: 参数错误。页码必须是正整数。该指令完整参数: page(int)=1")
            return
        
        if not self._check_op(event):
            yield event.plain_result("❌ 权限不足，仅管理员可用。")
            return
        
        if not self.blacklist_db:
            yield event.plain_result("✅ 黑名单为空。")
            return
        
        items = list(self.blacklist_db.items())
        per_page = 15
        total = len(items)
        pages = (total + per_page - 1) // per_page
        page = max(1, min(page, pages))
        
        start = (page - 1) * per_page
        page_items = items[start:start + per_page]
        
        lines = [f"📋 黑名单 ({total}) 第 {page}/{pages}页", "━━━━━━━━━━━━━━"]
        
        for uid, data in page_items:
            level = data.get("level", 1)
            emoji = "🔴" if level >= 3 else "🟡" if level == 2 else "🟢"
            reason = data.get('reason', 'N/A')
            lines.append(f"{emoji} {uid} | L{level} | {reason[:12]}...")
        
        lines.append("━━━━━━━━━━━━━━")
        lines.append("使用 /bl_list <页码> 查看更多")
        
        yield event.plain_result("\n".join(lines))
    
    # 移除 bl_op 指令，使用 AstrBot 内置的管理员管理系统
    
    @filter.command("bl_help")
    async def cmd_help(self, event: AstrMessageEvent):
        '''显示帮助'''
        # 检查是否为管理员
        is_op = hasattr(event, 'is_admin') and event.is_admin()
        
        msg = (
            f"🛡️皮梦云黑库 v{__version__}\n"
            f"━━━━━━━━━━━━━━\n"
            f"📋 命令列表\n"
            f"━━━━━━━━━━━━━━\n"
            f"/bl_check [target(str)=] [user_type(str)=user] - 检查状态\n"
        )
        
        if is_op:
            msg += (
                f"━━━━━━━━━━━━━━\n"
                f"[OP] /bl_status - 同步状态\n"
                f"[OP] /bl_sync - 强制同步\n"
                f"[OP] /bl_add user_id(str) reason(str) [level(int)=1] - 添加黑名单\n"
                f"[OP] /bl_remove user_id(str) [reason(str)=] - 移除黑名单\n"
                f"[OP] /bl_list [page(int)=1] - 查看列表\n"
            )
        
        msg += (
            f"━━━━━━━━━━━━━━\n"
            f"📝 参数说明\n"
            f"━━━━━━━━━━━━━━\n"
            f"• target: QQ号或群号（必须为数字）\n"
            f"• user_type: 类型 'user'(用户) 或 'group'(群组)，默认 user\n"
            f"• reason: 原因（必填）\n"
            f"• level: 等级 1-3（默认1，等级4需在管理面板操作）\n"
            f"• page: 页码（必须为正整数，默认1）\n"
            f"━━━━━━━━━━━━━━\n"
            f"⚠️ 参数错误时会显示完整参数格式\n"
            f"📊 等级: 1-轻微 2-一般 3-平台 4-严重\n"
            f"🔄 同步: 5分钟 | 📝 申诉: https://云黑.皮梦.wtf\n"
            f"📢 拦截: 全部拦截，仅主动唤醒bot时回复\n"
            f"📢 频率: 私聊每天一次，群聊不提醒\n"
            f"👤 你: {'管理员' if is_op else '用户'}"
        )
        
        yield event.plain_result(msg)
    
    # 检查Bot加入群组事件（通过消息内容判断）
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_member_join(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id())
        message_str = event.message_str.lower() if hasattr(event, 'message_str') else ""
        
        # 检查是否为Bot加入群组事件（通过消息内容判断）
        is_bot_join = False
        if message_str:
            # 检查是否包含Bot加入的关键词
            bot_join_keywords = ["邀请", "加入了群聊", "加入了", "加入群", "加群"]
            if any(keyword in message_str for keyword in bot_join_keywords):
                is_bot_join = True
        
        # 如果不是Bot加入事件，直接返回
        if not is_bot_join:
            return
        
        # 检查群组是否在黑名单中
        if group_id not in self.group_blacklist:
            return
        
        data = self.group_blacklist[group_id]
        level = data.get("level", 1)
        
        # 重置该用户在此群的新用户标记，确保首次发言时会提醒
        if user_id not in self.new_user_warned:
            self.new_user_warned[user_id] = {}
        self.new_user_warned[user_id][group_id] = False
        
        # Bot在黑名单群组中，自动退群
        self.logger.info(f"Bot in Blacklisted Group | Group: {group_id} | Level: {level}")
        
        # 尝试退群
        try:
            if hasattr(self.context, 'quit_group'):
                await self.context.quit_group(group_id)
                self.logger.info(f"Bot Quit Group | Group: {group_id}")
        except Exception as e:
            self.logger.error(f"Quit Group Failed | Group: {group_id} | Error: {e}")
    
    async def _api_check(self, user_id: str, user_type: str = "user") -> dict:
        return await self._api_request("POST", "/api/bot/check", {
            "user_id": user_id,
            "user_type": user_type
        })
    
    async def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """使用 http.client 发送请求，失败时自动重试一次"""
        
        def _sync_request():
            conn = http.client.HTTPSConnection("cloudblack-api.07210700.xyz")
            try:
                headers = {
                    "Authorization": self.bot_token,
                    "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
                    "Accept": "*/*",
                    "Host": "cloudblack-api.07210700.xyz",
                    "Connection": "keep-alive"
                }
                
                # 非GET请求才需要设置Content-Type
                if method.upper() != "GET":
                    headers["Content-Type"] = "application/json"
                    payload = json.dumps(data) if data else ""
                else:
                    payload = ""
                
                conn.request(method, endpoint, payload, headers)
                res = conn.getresponse()
                response_data = res.read().decode("utf-8")
                
                if res.status == 200:
                    try:
                        return json.loads(response_data)
                    except json.JSONDecodeError:
                        return {"success": False, "message": f"JSON解析错误: {response_data}"}
                else:
                    return {"success": False, "message": f"HTTP {res.status}: {response_data}"}
            except Exception as e:
                return {"success": False, "message": str(e)}
            finally:
                conn.close()
        
        # 在异步环境中运行同步的 http.client 请求，最多重试一次
        max_retries = 2
        last_error = None
        
        for attempt in range(max_retries):
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(_sync_request),
                    timeout=self.request_timeout
                )
                
                # 如果成功，直接返回
                if result.get("success"):
                    return result
                
                # 如果失败，记录错误并继续重试
                last_error = result.get("message", "未知错误")
                self.logger.warning(f"API请求失败 (尝试 {attempt + 1}/{max_retries}): {last_error}")
                
                # 如果不是最后一次尝试，等待1秒后重试
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    
            except asyncio.TimeoutError:
                last_error = f"请求超时 ({self.request_timeout}秒)"
                self.logger.warning(f"API请求超时 (尝试 {attempt + 1}/{max_retries})")
                
                # 如果不是最后一次尝试，等待1秒后重试
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    
            except Exception as e:
                last_error = str(e)
                self.logger.warning(f"API请求异常 (尝试 {attempt + 1}/{max_retries}): {last_error}")
                
                # 如果不是最后一次尝试，等待1秒后重试
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        
        # 两次都失败，返回错误
        return {"success": False, "message": f"API请求失败 (已重试1次): {last_error}"}
    
    async def _kick_member(self, group_id: str, user_id: str):
        """踢出成员"""
        try:
            # 尝试使用context的方法
            if hasattr(self.context, 'kick_group_member'):
                await self.context.kick_group_member(group_id, user_id)
                return
        except:
            pass
        
        # 尝试获取适配器
        for adapter_name in ["aiocqhttp", "onebot_v11", "napcat"]:
            try:
                adapter = self.context.get_adapter(adapter_name)
                if adapter and hasattr(adapter, 'kick_group_member'):
                    await adapter.kick_group_member(group_id=group_id, user_id=user_id)
                    return
            except:
                continue
        
        self.logger.warning(f"Cannot kick member {user_id} from group {group_id}: no suitable adapter")