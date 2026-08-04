"""网络请求模块 - 处理与皮梦云黑库API的通信"""

import asyncio
import json
import re
import urllib.parse
import aiohttp
import ssl
from typing import Optional


class PimengAPI:
    """皮梦云黑库API客户端"""
    
    def __init__(self, api_base: str, bot_token: str, request_timeout: int, logger):
        self.api_base = api_base.rstrip("/")
        self.bot_token = bot_token
        self.request_timeout = request_timeout
        self.logger = logger
        
        parsed = urllib.parse.urlparse(self.api_base)
        self.scheme = parsed.scheme or "https"
        self.host = parsed.netloc or "cloudblack-api.07210700.xyz"
        self.base_path = parsed.path or ""
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._ssl_context: Optional[ssl.SSLContext] = None
        
        if self.scheme == "https":
            self._ssl_context = ssl.create_default_context()
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session (reuse connection)."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def terminate(self):
        """Close aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def check_blacklist(self, user_id: str, user_type: str = "user") -> dict:
        """Check if user/group is in blacklist."""
        return await self._make_request("POST", "/api/bot/check", {
            "user_id": user_id,
            "user_type": user_type
        })
    
    async def add_to_blacklist(self, user_id: str, user_type: str, reason: str, level: int) -> dict:
        """Add to blacklist."""
        return await self._make_request("POST", "/api/bot/add", {
            "user_id": user_id,
            "user_type": user_type,
            "reason": reason,
            "level": level
        })
    
    async def remove_from_blacklist(self, user_id: str, user_type: str, reason: str) -> dict:
        """Remove from blacklist."""
        return await self._make_request("POST", "/api/bot/delete", {
            "user_id": user_id,
            "user_type": user_type,
            "reason": reason
        })
    
    async def get_blacklist(self) -> dict:
        """Get blacklist."""
        return await self._make_request("GET", "/api/bot/getlist")
    
    async def _make_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Send API request (with smart retry)."""
        max_retries = 2
        retry_delay = 1
        
        for attempt in range(max_retries):
            result = await self._make_async_request(method, endpoint, data)
            
            if result.get("success"):
                return result
            
            error_message = result.get('message', '')
            should_retry = self._should_retry(error_message, attempt, max_retries)
            
            if not should_retry:
                return result
            
            self.logger.warning(f"API请求失败，正在重试 ({attempt + 1}/{max_retries}): {error_message}")
            await asyncio.sleep(retry_delay * (attempt + 1))
        
        return result
    
    def _should_retry(self, error_message: str, attempt: int, max_retries: int) -> bool:
        """Determine if should retry.
        
        Only retry recoverable errors:
        - Network errors (timeout, connection errors, etc.)
        - 5xx server errors
        - Some 4xx errors (e.g., 429 Too Many Requests)
        
        Do not retry:
        - Authentication errors (401, 403)
        - Client errors (400 Bad Request)
        - Resource not found (404)
        """
        if attempt >= max_retries - 1:
            return False
        
        network_errors = ["网络错误", "请求超时", "连接错误", "Timeout", "Connection"]
        if any(err in error_message for err in network_errors):
            return True
        
        status_match = re.search(r'HTTP (\d{3})', error_message)
        if status_match:
            status_code = int(status_match.group(1))
            
            if 500 <= status_code < 600:
                return True
            
            if status_code == 429:
                return True
            
            if 400 <= status_code < 500:
                return False
        
        return False
    
    async def _make_async_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Async HTTP request (using reused aiohttp session)."""
        base_path = self.base_path.rstrip("/")
        endpoint = endpoint.lstrip("/")
        path = f"/{base_path}/{endpoint}" if base_path else f"/{endpoint}"
        url = f"{self.scheme}://{self.host}{path}"
        
        headers = {
            "Authorization": self.bot_token if self.bot_token else "",
            "User-Agent": "PimengBlacklist/2.9.2",
            "Accept": "application/json",
        }
        
        try:
            session = await self._get_session()
            
            if method.upper() == "GET":
                response = await session.get(url, headers=headers, ssl=self._ssl_context)
                try:
                    return await self._handle_response(response)
                finally:
                    response.close()
            else:
                headers["Content-Type"] = "application/json"
                json_data = json.dumps(data) if data else None
                response = await session.request(method, url, headers=headers, data=json_data, ssl=self._ssl_context)
                try:
                    return await self._handle_response(response)
                finally:
                    response.close()
                        
        except asyncio.TimeoutError:
            return {"success": False, "message": f"请求超时 ({self.request_timeout}秒)"}
        except aiohttp.ClientError as e:
            return {"success": False, "message": f"网络错误: {str(e)}"}
        except Exception as e:
            self.logger.error(f"API请求未知异常: {type(e).__name__}: {str(e)}")
            return {"success": False, "message": "内部错误，请查看日志"}
    
    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict:
        """Handle HTTP response."""
        try:
            response_data = await response.text()
            
            if response.status == 200:
                try:
                    api_response = json.loads(response_data)
                    if "success" not in api_response:
                        api_response["success"] = True
                    return api_response
                except json.JSONDecodeError:
                    return {"success": False, "message": f"JSON解析错误: {response_data[:100]}"}
            else:
                error_msg = f"HTTP {response.status}"
                if response_data:
                    try:
                        error_data = json.loads(response_data)
                        if "message" in error_data:
                            error_msg = f"HTTP {response.status}: {error_data['message']}"
                        elif "error" in error_data:
                            error_msg = f"HTTP {response.status}: {error_data['error']}"
                    except json.JSONDecodeError:
                        error_msg = f"HTTP {response.status}: {response_data[:200]}"
                return {"success": False, "message": error_msg}
        except Exception as e:
            return {"success": False, "message": f"响应处理错误: {str(e)}"}
