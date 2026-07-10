# 版本历史
- v2.9.3：修复 `/bl_check` 不指定类型时绕过限流的 Bug（会双次调用 API）；将踢人权限检查失败时的降级默认值改为 `False`，无法确认权限时拒绝踢人以避免误踢；同步模块去除重复的 Token 检查并改用统一日志
- v2.9.2：优化拦截策略：1级（轻微）不进行拦截输出，2~4级（一般/平台/严重）才进行拦截并提示
- v2.9.1：优化文案描述；版本号更新
- v2.9.0：踢人/拦截消息显示「添加者」；user/group 参数支持中文别名（用户/群组）和短参数（-u/-g）；重写 README 文档
- v2.8.4：群聊仅Bot互动时拦截提醒；bl_add支持@提及自动提取QQ号；新增强开关配置
- v2.8.3：黑名单用户群内消息全拦截提醒；进群检测管理员/群主自动退群；退群逻辑统一重构
- v2.8.2：移除弃用的@register装饰器（使用metadata.yaml替代），修复super().__init__传参问题，修复license声明
- v2.8.1：修复了同步异常回滚问题，不会再错误的显示同步成功了
- v2.8.0: 添加同步冷却时间机制（1分钟），强制同步命令忽略冷却时间，修复API认证格式问题
- v2.7.0: 代码重构，将功能拆分为模块化架构（api.py、cache.py、service.py、handler.py），修复API路径解析和返回结构问题，增强安全性和稳定性
- v2.6.0: 冗余代码优化，拿GPT跑了一下发现太那啥就改了（，另外版本要求上提至4.15.0
- v2.5.0: 区分用户和群组黑名单，用户黑名单仅拦截，群组黑名单仅自动退群
- v2.4.0: 优化提醒机制，私聊每天仅提醒一次，踢人消息仅在自动踢出启用时发送
- v2.3.1: 修复了踢人失败，消息发送频率太快等
- v2.2.0: 增强配置选项，添加自动踢出开关和请求超时配置
- v2.1.0: 使用http.client替代aiohttp，优化错误处理
- v2.0.0: 完整API接口支持，添加同步机制
- v1.0.0: 初始版本，基础查询功能

## 另外，额

- **这份代码已经越过"能用"的阶段，正在迈向"只能作者本人维护"的阶段**
- 还有，**仅支持onebot协议的napcat**和**QQ官方API**
- 另外，紧急更新了2.8.0版本，防止API刷太多导致IP被封
> **[2026-04-09 10:10:25.395] [Plug] [WARN] [v4.22.0] [astrbot_plugin_pimeng_blacklist.api:63]: API request failed, retrying: HTTP 403: IP 被暂时封禁，请稍后再试
[2026-04-09 10:10:26.689] [Plug] [ERRO] [v4.22.0] [astrbot_plugin_pimeng_blacklist.service:73]: Sync failed: HTTP 403: IP 被暂时封禁，请稍后再试
[2026-04-09 10:10:26.689] [Plug] [WARN] [v4.22.0] [astrbot_plugin_pimeng_blacklist.service:79]: 权限不足 (403): Token可能已过期或无权访问
[2026-04-09 10:10:26.689] [Plug] [INFO] [astrbot_plugin_pimeng_blacklist.service:34]: BlacklistService initialized | Users: 0 | Groups: 0 | Sync: 5min**（艺术来源于生活.png，被打）