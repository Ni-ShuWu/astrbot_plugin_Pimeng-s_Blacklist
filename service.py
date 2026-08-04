"""黑名单逻辑核心模块 - 处理黑名单数据管理和同步"""

import asyncio
from typing import Dict, Optional, Union
from datetime import datetime, timedelta

from .api import PimengAPI
from .cache import BlacklistCache


class BlacklistService:
    """黑名单服务管理器"""
    
    def __init__(self, api: PimengAPI, cache: BlacklistCache, sync_interval: int, logger, handler=None):
        self.api = api
        self.cache = cache
        self.sync_interval = sync_interval
        self.logger = logger
        self.handler = handler
        
        self.user_blacklist: Dict[str, dict] = {}
        self.group_blacklist: Dict[str, dict] = {}
        
        self.last_sync: Optional[datetime] = None
        self.sync_task: Optional[asyncio.Task] = None
        
        self.user_query_times: Dict[str, datetime] = {}
        self.global_last_query_time: Optional[datetime] = None
        self.query_cooldown = 5
        self.max_query_times_size = 1000
        
        self.query_cache: Dict[str, tuple] = {}
        self.query_cache_ttl = 300
        self.max_cache_size = 1000
        
        self._sync_lock = asyncio.Lock()
    
    async def initialize(self):
        try:
            await self.sync_blacklist()
        except Exception as e:
            self.logger.warning(f"首次同步失败，定时任务仍会继续: {e}")
        
        self.sync_task = asyncio.create_task(self._scheduled_sync())
        self.cache_cleanup_task = asyncio.create_task(self._scheduled_cache_cleanup())
        
        self.logger.info(f"BlacklistService initialized | Users: {len(self.user_blacklist)} | Groups: {len(self.group_blacklist)} | Sync: {self.sync_interval//60}min")
    
    async def terminate(self):
        if self.sync_task:
            self.sync_task.cancel()
            try:
                await self.sync_task
            except asyncio.CancelledError:
                pass
        
        if hasattr(self, 'cache_cleanup_task'):
            self.cache_cleanup_task.cancel()
            try:
                await self.cache_cleanup_task
            except asyncio.CancelledError:
                pass
    
    async def _scheduled_sync(self):
        while True:
            try:
                await asyncio.sleep(self.sync_interval)
                await self.sync_blacklist()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Scheduled sync error: {e}")
                await asyncio.sleep(60)
    
    async def _scheduled_cache_cleanup(self):
        while True:
            try:
                await asyncio.sleep(300)
                self._clean_expired_cache()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Cache cleanup error: {e}")
                await asyncio.sleep(60)
    
    async def sync_blacklist(self, force: bool = False) -> bool:
        async with self._sync_lock:
            return await self._sync_blacklist_internal(force)
    
    async def _sync_blacklist_internal(self, force: bool = False) -> bool:
        if not self.api.bot_token:
            self.logger.warning("未配置Bot Token，跳过同步")
            return False

        if not force and self.last_sync:
            time_since_last_sync = datetime.now() - self.last_sync
            if time_since_last_sync.total_seconds() < 60:
                self.logger.debug(f"同步冷却中，上次同步于 {self.last_sync.strftime('%H:%M:%S')}，{int(60 - time_since_last_sync.total_seconds())}秒后可同步")
                return False

        old_user_blacklist = self.user_blacklist.copy()
        old_group_blacklist = self.group_blacklist.copy()

        try:
            self.logger.debug(f"开始同步，Token已配置（长度: {len(self.api.bot_token)}）")
            result = await self.api.get_blacklist()
            
            if not result.get("success"):
                error_msg = result.get('message', 'Unknown')
                self.logger.error(f"Sync failed: {error_msg}")
                
                if "401" in error_msg:
                    self.logger.warning("认证失败 (401): 请检查Bot Token是否正确")
                elif "403" in error_msg:
                    self.logger.warning("权限不足 (403): Token可能已过期或无权访问")
                return False
            
            remote_list = result.get("data", {}).get("blacklist", [])
            if not isinstance(remote_list, list):
                self.logger.error("Sync failed: Invalid blacklist format")
                return False
            
            old_users = len(self.user_blacklist)
            old_groups = len(self.group_blacklist)
            
            new_user_blacklist: Dict[str, dict] = {}
            new_group_blacklist: Dict[str, dict] = {}
            
            for item in remote_list:
                user_id = str(item.get("user_id", ""))
                if not user_id:
                    continue
                
                data = {
                    "level": item.get("level", 1),
                    "reason": item.get("reason", "Unknown"),
                    "added_at": item.get("added_at", "Unknown"),
                    "added_by": item.get("added_by", "Unknown"),
                }
                
                if item.get("user_type") == "group":
                    new_group_blacklist[user_id] = data
                else:
                    new_user_blacklist[user_id] = data
            
            self.user_blacklist = new_user_blacklist
            self.group_blacklist = new_group_blacklist
            
            current_users = set(self.user_blacklist.keys())
            self.cache.clean_expired_records(current_users)
            
            if hasattr(self, 'handler') and hasattr(self.handler, 'quit_groups'):
                current_groups = set(self.group_blacklist.keys())
                self.handler.quit_groups = {g for g in self.handler.quit_groups if g in current_groups}
            
            self.last_sync = datetime.now()
            self.logger.info(f"Sync OK | Users: {old_users}->{len(self.user_blacklist)}, Groups: {old_groups}->{len(self.group_blacklist)}")
            return True
            
        except Exception:
            self.logger.error(f"Sync ERROR |")
            self.user_blacklist = old_user_blacklist
            self.group_blacklist = old_group_blacklist
            self.logger.info(f"Sync rolled back | Users: {len(self.user_blacklist)}, Groups: {len(self.group_blacklist)}")
            return False
    
    def can_query_api(self, user_id: str = None) -> bool:
        if user_id is None:
            if self.global_last_query_time is None:
                return True
            
            time_since_last_query = datetime.now() - self.global_last_query_time
            if time_since_last_query.total_seconds() < self.query_cooldown:
                self.logger.debug(f"全局查询限流中，上次查询于 {self.global_last_query_time.strftime('%H:%M:%S')}，{int(self.query_cooldown - time_since_last_query.total_seconds())}秒后可查询")
                return False
            return True
        
        if user_id not in self.user_query_times:
            return True
        
        last_query_time = self.user_query_times[user_id]
        time_since_last_query = datetime.now() - last_query_time
        if time_since_last_query.total_seconds() < self.query_cooldown:
            self.logger.debug(f"用户 {user_id} 查询限流中，上次查询于 {last_query_time.strftime('%H:%M:%S')}，{int(self.query_cooldown - time_since_last_query.total_seconds())}秒后可查询")
            return False
        
        return True
    
    def update_query_time(self, user_id: str = None):
        if user_id is None:
            self.global_last_query_time = datetime.now()
        else:
            self.user_query_times[user_id] = datetime.now()
            if len(self.user_query_times) > self.max_query_times_size:
                self._cleanup_old_query_times()
    
    def _cleanup_old_query_times(self):
        now = datetime.now()
        expired_users = []
        for uid, last_time in self.user_query_times.items():
            if (now - last_time).total_seconds() >= self.query_cooldown * 10:
                expired_users.append(uid)
        
        for uid in expired_users:
            del self.user_query_times[uid]
        
        if expired_users:
            self.logger.debug(f"清理了 {len(expired_users)} 个过期用户查询记录")
    
    def get_cached_query(self, target_id: str, query_type: str) -> Optional[dict]:
        cache_key = f"{query_type}_{target_id}"
        if cache_key in self.query_cache:
            result, timestamp = self.query_cache[cache_key]
            if (datetime.now() - timestamp).total_seconds() < self.query_cache_ttl:
                self.logger.debug(f"使用缓存查询结果: {cache_key}")
                return result
            else:
                del self.query_cache[cache_key]
        return None
    
    def set_cached_query(self, target_id: str, query_type: str, result: dict):
        cache_key = f"{query_type}_{target_id}"
        
        if len(self.query_cache) >= self.max_cache_size:
            self._clean_expired_cache()
            
            if len(self.query_cache) >= self.max_cache_size:
                excess = len(self.query_cache) - self.max_cache_size + 100
                keys_to_remove = list(self.query_cache.keys())[:excess]
                for key in keys_to_remove:
                    del self.query_cache[key]
                self.logger.warning(f"缓存已满，删除了 {len(keys_to_remove)} 条缓存记录")
        
        self.query_cache[cache_key] = (result, datetime.now())
        self.logger.debug(f"缓存查询结果: {cache_key}")
    
    def _clean_expired_cache(self):
        now = datetime.now()
        expired_keys = []
        for key, (_, timestamp) in self.query_cache.items():
            if (now - timestamp).total_seconds() >= self.query_cache_ttl:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.query_cache[key]
        
        if expired_keys:
            self.logger.debug(f"清理了 {len(expired_keys)} 个过期缓存")
    
    def is_user_blacklisted(self, user_id: str) -> bool:
        return user_id in self.user_blacklist
    
    def is_group_blacklisted(self, group_id: str) -> bool:
        return group_id in self.group_blacklist
    
    def get_user_data(self, user_id: str) -> Optional[dict]:
        return self.user_blacklist.get(user_id)
    
    def get_group_data(self, group_id: str) -> Optional[dict]:
        return self.group_blacklist.get(group_id)
    
    def remove_user(self, user_id: str):
        self.user_blacklist.pop(user_id, None)
    
    def remove_group(self, group_id: str):
        self.group_blacklist.pop(group_id, None)
    
    def get_stats(self) -> Dict[str, Union[int, str]]:
        return {
            "user_blacklist": len(self.user_blacklist),
            "group_blacklist": len(self.group_blacklist),
            "last_sync": self.last_sync.strftime("%H:%M:%S") if self.last_sync else "Never",
            "next_sync_in": self._get_next_sync_minutes()
        }
    
    def _get_next_sync_minutes(self) -> int:
        if not self.last_sync:
            return 0
        
        next_sync = self.last_sync + timedelta(seconds=self.sync_interval)
        time_diff = (next_sync - datetime.now()).total_seconds()
        return max(0, int(time_diff // 60))