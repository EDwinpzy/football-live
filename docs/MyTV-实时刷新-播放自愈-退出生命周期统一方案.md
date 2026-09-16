# My TV · 实时刷新、播放自愈与退出生命周期统一方案

> 适用范围：TV 版 / 移动版 / Web（其中“彻底退出 App”主要针对 Android TV / Android 移动端）  
> 目标：一次性解决以下三类问题：  
> 1. 页面数据长时间不变化，必须退出重进才看到新内容；  
> 2. 部分足球比赛播放一段时间后卡死/不能播放，退出 App 再进入后又恢复；  
> 3. 用户“退出 App”后进程仍在后台运行，下一次进入时继续复用异常状态，无法真正重新初始化。  
>
> 核心原则：**UI 刷新、播放恢复、进程退出必须分开治理，但统一由生命周期和状态机管理。**

---

## 1. 现象判断：这不只是“刷新问题”

当前现象：

```text
比赛可以正常播放
    ↓
播放一段时间
    ↓
画面卡住 / BUFFERING / 线路失效
    ↓
自动切线没有恢复
    ↓
退出播放页仍无效
    ↓
退出 App
    ↓
重新进入 App
    ↓
又能播放
```

这说明问题很可能不仅是 UI 没刷新。

“重启 App 后恢复”通常意味着以下某一层被重置后恢复：

```text
Media3 Player 实例
HTTP / HLS 网络连接
本机 relay session
签名 URL
StreamDecryptServer
3-WebView 解密池
QuickJS bridge
PlayerViewModel 状态
engineEpoch / session generation
Chaquopy :backend 进程
DNS / socket / connection pool
线路失败黑名单
Coroutine / Job
```

所以必须同时处理：

1. **数据刷新**
2. **播放会话自愈**
3. **后端与解密组件自愈**
4. **App 生命周期与彻底退出**

---

# 2. 总体治理架构

建议统一成：

```text
                        AppLifecycleManager
                                │
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
          ▼                     ▼                     ▼
 RefreshCoordinator     PlaybackSupervisor      ExitCoordinator
 数据刷新调度中心          播放自愈中心              退出管理中心
          │                     │                     │
          ▼                     ▼                     ▼
 Repository/StateFlow      PlayerSession         Activity / Service
          │                     │                 / :backend Process
          ▼                     ▼
      Compose UI        Media3 / Relay / VLC
                                │
                 ┌──────────────┼──────────────┐
                 ▼              ▼              ▼
             Network       Decrypt Pool     Backend
```

不要继续由每个页面、每个 ViewModel、每个播放器组件分别处理刷新/重试/退出。

---

# 3. 第一部分：界面“内容不变化”的解决方案

## 3.1 引入统一 RefreshCoordinator

Android 共享层新增：

```text
data/refresh/
├── RefreshCoordinator.kt
├── RefreshDomain.kt
├── RefreshPolicy.kt
├── RefreshReason.kt
└── RefreshRegistry.kt
```

刷新原因：

```kotlin
enum class RefreshReason {
    INITIAL,
    PAGE_VISIBLE,
    APP_FOREGROUND,
    NETWORK_RECONNECTED,
    TIMER,
    USER,
    FORCE,
    PLAYBACK_RECOVERED,
    BACKEND_RESTARTED
}
```

---

## 3.2 页面不再依赖 ViewModel.init{}

禁止把：

```kotlin
init {
    loadHome()
}
```

当作唯一刷新入口。

因为 ViewModel 没有销毁时：

```text
第一次进入 → load
离开页面
重新回来
ViewModel 仍存在
init 不再执行
```

改成：

```text
Screen 可见
    ↓
onVisible()
    ↓
RefreshCoordinator
    ↓
根据 freshness 判断是否刷新
```

---

## 3.3 UI 统一订阅 StateFlow

推荐：

```kotlin
val state by viewModel.state.collectAsStateWithLifecycle()
```

Repository 做唯一数据源：

```text
Remote / Cache
     ↓
Repository
     ↓
StateFlow
     ↓
ViewModel
     ↓
Compose
```

---

## 3.4 禁止原地修改 List / Map

错误：

```kotlin
movies.clear()
movies.addAll(newItems)
```

正确：

```kotlin
state.update {
    it.copy(
        movies = newItems.toList()
    )
}
```

保证：

```text
新 State
新 List
新引用
```

让 Compose 确定发生重组。

---

## 3.5 页面刷新采用 SWR

统一：

```text
进入页面
↓
立即显示缓存
↓
后台重新拉取
↓
有变化
↓
更新 StateFlow
↓
UI 自动刷新
```

不允许：

```text
刷新开始
↓
清空列表
↓
Loading
```

刷新失败时继续保留旧内容。

---

## 3.6 建议刷新周期

| 数据域 | 建议策略 |
|---|---|
| 首页 | 可见时检查，2 分钟刷新 |
| 片库 | 可见时检查，2~3 分钟刷新 |
| 影片详情 | 每次进入 SWR，2 分钟过期 |
| 播放线路 | 播放前检查，必要时强刷 |
| 搜索 | 输入变化触发，不定时轮询 |
| 收藏/历史 | 本地事件实时更新 |
| 足球比赛列表 | 页面可见时 10~15 秒 |
| 足球信号源 | 页面可见时 10~15 秒 |
| IPTV 完整列表 | 保持 30 分钟级缓存 |
| 公告 | 5 分钟 |
| 卡密 | 保持现有 6 小时逻辑 |

---

# 4. 第二部分：足球播放“看一会儿就卡死”的根治方案

## 4.1 不要把它当普通 BUFFERING

需要把播放故障分成至少 6 类。

```text
A. 网络暂时抖动
B. 当前 HLS URL / token 过期
C. 当前源站线路永久失效
D. Relay / 解密会话异常
E. Media3 自身进入不可恢复状态
F. :backend / WebView / QuickJS 等底层组件异常
```

每类采用不同恢复方式。

---

# 5. 建立 PlaybackSupervisor

新增：

```text
playback/
├── PlaybackSupervisor.kt
├── PlaybackHealth.kt
├── PlaybackRecovery.kt
├── PlaybackFailure.kt
└── PlaybackSession.kt
```

PlaybackSupervisor 只负责：

```text
监控播放健康度
判断故障类型
决定恢复级别
控制最大重试
升级恢复策略
```

不要让 PlayerScreen 自己处理全部故障。

---

# 6. 统一 PlaybackSession

每次起播生成：

```text
sessionId
engineEpoch
streamEpoch
sourceId
startedAt
lastFrameAt
lastProgressAt
lastBytesAt
recoverCount
```

例如：

```kotlin
data class PlaybackSession(
    val id: Long,
    val engineEpoch: Long,
    val streamEpoch: Long,
    val sourceId: String,
    val startedAt: Long,
    val lastProgressAt: Long,
    val lastFrameAt: Long
)
```

任何旧异步回调到达时：

```text
callback.sessionId != current.sessionId
```

直接丢弃。

防止：

```text
旧线路异步结果
覆盖新线路
```

---

# 7. 增加“假 READY / 假播放”检测

目前仅判断：

```text
Player.STATE_READY
```

不够。

播放器可能：

```text
STATE_READY
但画面已经 20 秒没动
```

需要同时监控：

```text
playbackPosition 是否增长
视频帧是否持续输出
BUFFERING 时长
网络字节是否继续增长
```

建议定义：

```text
正常：
position 持续增长

异常：
isPlaying=true
但 position 8~10 秒不增长

严重异常：
15 秒无位置增长
且不是暂停/seek
```

触发 PlaybackSupervisor。

---

# 8. 恢复等级设计

不要一卡就直接重启 App。

使用分级自愈：

## Level 0：短暂等待

适用：

```text
短网络波动
BUFFERING < 3.5s
```

保持现有逻辑。

---

## Level 1：同 URL 重新 prepare

条件：

```text
BUFFERING > 5~8 秒
或播放位置长期不增长
```

执行：

```text
保存当前 position
↓
Media3 stop
↓
重新 setMediaItem
↓
prepare
↓
恢复 position
```

仅一次。

---

## Level 2：重新获取当前线路真实 URL

这是足球直播非常重要的一层。

因为很多线路：

```text
播放器页 URL
解密结果
签名
token
HLS 地址
```

存在有效期。

不要继续拿旧 URL 重播。

流程：

```text
当前源卡死
↓
invalidate 当前 resolved stream
↓
重新调用 /api/stream
↓
重新解析播放器页
↓
重新解密
↓
获得新的真实 HLS URL
↓
重新起播
```

即：

```text
“刷新播放线路”
≠
“重新 prepare 原 URL”
```

---

# 9. 签名 URL / token 到期必须主动处理

对于：

```text
403
401
404
410
HLS manifest reload 失败
连续 segment 403
```

不要等 15 秒看门狗。

立即判断：

```text
可能 URL 过期
```

执行：

```text
重新 resolve 当前 source
```

如果成功：

```text
streamEpoch++
```

无缝换到新地址。

---

# 10. Level 3：切其他信号源

如果：

```text
当前 source 重新解析仍失败
```

执行：

```text
当前 source 降权
↓
选择下一健康 source
↓
resolve
↓
起播
```

注意：

```text
自动切源
不得覆盖 liveLastSrc
```

保持你当前的产品规则。

---

# 11. Level 4：重建 Media3 Player

这是目前很可能缺失的一层。

有些 Media3/Codec/Surface 状态异常：

```text
换 URL
重新 prepare
切源
```

都不能恢复。

但退出 App 后恢复，说明重新创建 Player 能恢复。

所以增加：

```text
PlayerRecreate
```

执行：

```text
release 当前 ExoPlayer
↓
释放 Surface / Decoder
↓
等待极短生命周期切换
↓
创建全新 Player
↓
重新绑定 PlayerView
↓
重新打开当前线路
```

注意：

不是：

```text
player.clearMediaItems()
```

而是真正：

```text
player.release()
new ExoPlayer.Builder(...)
```

---

# 12. Level 5：重建 Relay / 解密会话

如果日志显示：

```text
播放器换线仍失败
重新创建 Player 仍失败
```

则可能是：

```text
本地 relay session
StreamDecryptServer
WebView slot
QuickJS bridge
```

出现异常。

恢复：

```text
invalidate relay session
↓
关闭当前 local HLS relay
↓
创建新 relayId
↓
清理对应 decrypt slot/session
↓
重新 resolve
```

不要整个解密池全清，优先只清当前会话。

---

# 13. Level 6：局部重启 :backend

如果出现：

```text
/api/stream 请求持续超时
/api/matches 异常
relay 创建失败
Chaquopy 内部状态异常
```

连续达到阈值：

```text
3 次
```

允许：

```text
BackendService 局部重启
```

你已经有：

```text
:backend 独立进程
```

因此可以做到：

```text
主 App 不退出
↓
重启 :backend
↓
健康探测
↓
重新 resolve
↓
恢复播放
```

这比要求用户退出 App 干净得多。

---

# 14. 解密池自愈再补一层

你现有：

```text
onRenderProcessGone
12 秒零回调检测
runCatching
```

已经解决部分 WebView 僵死。

建议再增加：

```text
每个 slot：
successCount
failCount
timeoutCount
lastSuccessAt
generation
```

如果单 slot：

```text
连续 2 次 timeout
```

直接：

```text
destroy
↓
recreate
```

不要等整个池全部僵死。

如果：

```text
3 个 slot 在 30 秒内全部失败
```

执行：

```text
decryptPoolGeneration++
全池重建
```

然后重新解密当前线路。

---

# 15. WebView 渲染进程不能成为“App 重启才能恢复”的资源

目标必须变成：

```text
WebView renderer 死
↓
对应 slot 自动销毁
↓
新 slot 创建
↓
下一次请求自动恢复
```

最终用户不应该感知。

---

# 16. 网络连接池异常也要考虑

如果：

```text
App 重启后才能恢复
```

还有一种可能：

```text
OkHttp / urllib / requests
复用了异常 keep-alive socket
DNS 缓存
连接池坏状态
```

建议在连续网络类故障时支持：

```text
HTTP client generation
```

例如：

```text
连续 3 次：
Connection reset
EOF
ProtocolException
timeout
```

触发：

```text
evictAll()
↓
重建 HTTP client
```

Python requests/httpx 同样可以重建 Session。

不要长期永久复用一个已经异常的 Session。

---

# 17. 播放自愈状态机

推荐：

```text
PLAYING
   │
   ├─ 正常 ───────────────→ PLAYING
   │
   └─ stall
        ↓
      RETRY_URL
        ↓失败
      RERESOLVE_CURRENT
        ↓失败
      SWITCH_SOURCE
        ↓失败
      RECREATE_PLAYER
        ↓失败
      RESET_RELAY_DECRYPT
        ↓失败
      RESTART_BACKEND
        ↓失败
      FATAL
```

每次成功：

```text
recoverCount = 0
```

---

# 18. 防止无限自愈循环

必须设置预算。

例如单次播放 session：

```text
同 URL retry：1 次
当前源重新解析：2 次
自动换源：最多 3 个源
Player recreate：1 次 / 60 秒
Decrypt reset：1 次 / 60 秒
Backend restart：1 次 / 5 分钟
```

否则坏比赛会形成：

```text
无限请求
无限重建
无限 CPU
```

---

# 19. 自动恢复时保持用户观看状态

恢复必须保存：

```text
当前比赛
当前 source
当前全屏状态
控制条状态
音量
字幕/音轨（若有）
```

直播无需强求 seek 到原时间，但点播需要保存 position。

恢复目标：

```text
用户只看到短暂转圈
```

而不是退回详情页。

---

# 20. 播放前进行线路新鲜度检查

当前详情页线路如果已经停留很久：

```text
用户 21:00 打开详情
21:20 才点播放
```

旧线路可能已经失效。

建议：

```text
resolvedAt > 2min
```

播放前：

```text
重新 resolve
```

足球类建议更激进：

```text
resolvedAt > 30~60s
```

重新获取真实地址。

---

# 21. 后台回来时不直接复用旧直播 Session

例如：

```text
正在看足球
↓
Home 键
↓
后台 20 分钟
↓
重新回来
```

旧 HLS token 很可能已经过期。

恢复前台时：

```text
APP_FOREGROUND
↓
检查后台时长
```

如果：

```text
backgroundDuration < 30s
```

可尝试继续。

如果：

```text
> 30~60s
```

足球直播：

```text
重新 resolve 当前 source
```

而不是直接复用旧 URL。

---

# 22. 第三部分：彻底解决“退出了但 App 还在后台”

## 22.1 先区分三个概念

Android：

```text
返回桌面
≠
退出 Activity
≠
杀掉 App 进程
```

例如：

```kotlin
finish()
```

只是当前 Activity 结束。

```kotlin
finishAffinity()
```

只是关闭任务栈 Activity。

它们都不保证：

```text
:backend
Service
Coroutine
Process
```

立即结束。

这就是为什么用户感觉：

```text
“我退出了，但它其实还在后台。”
```

---

# 23. 定义两种明确行为

建议产品层区分：

## 普通返回

遥控器 BACK：

```text
播放器
↓
返回上一级页面
```

不退出 App。

---

## 明确“退出 App”

用户在首页再次 BACK：

```text
弹一次退出确认
```

或保持你当前的一次退出语义。

确认退出后执行完整 shutdown。

---

# 24. 建立 ExitCoordinator

新增：

```text
app/lifecycle/
├── AppLifecycleManager.kt
├── ExitCoordinator.kt
└── ShutdownReason.kt
```

退出必须集中管理。

禁止：

```text
某页面 finish()
某页面 finishAffinity()
某 Service 自己 stop
```

各自为政。

---

# 25. 完整退出顺序

建议：

```text
用户请求退出
↓
isShuttingDown = true
↓
停止 RefreshCoordinator
↓
停止 PlaybackSupervisor
↓
release Player
↓
关闭 relay session
↓
关闭 StreamDecryptServer 当前资源
↓
取消 applicationScope 中可取消任务
↓
停止前台/后台 Service
↓
通知 :backend graceful shutdown
↓
等待 backend 保存必要状态
↓
stopService(BackendService)
↓
finishAffinity()
↓
必要时结束 backend process
↓
主进程退出
```

---

# 26. 后端独立进程必须单独处理

你的：

```xml
android:process=":backend"
```

意味着：

```text
主 Activity 退出
```

不等于：

```text
:backend 退出
```

因此如果产品定义“彻底退出”：

必须显式停止：

```text
BackendService
```

并确保：

```text
START_NOT_STICKY
```

避免系统自动拉起。

---

# 27. 检查 BackendService 返回值

如果现在使用：

```kotlin
START_STICKY
```

用户退出后 Android 可能再次创建它。

建议对普通内嵌后端：

```kotlin
START_NOT_STICKY
```

除非确实有业务要求后台常驻。

My TV 当前并不需要：

```text
用户退出后继续运行 Python backend
```

所以不建议 STICKY。

---

# 28. 避免 Service 自启动链

检查：

```text
Application.onCreate
Activity.onCreate
BootReceiver
NetworkReceiver
WorkManager
```

是否存在：

```text
发现 BackendService 不在
↓
自动 startService()
```

如果：

```text
isShuttingDown = true
```

必须阻止重新拉起。

---

# 29. 增加 persisted shutdown flag

为了避免：

```text
主进程正在退出
↓
另一个回调又启动 backend
```

增加：

```text
filesDir/app_state.json
```

或者轻量共享状态：

```json
{
  "explicitExit": true
}
```

下次用户主动点图标进入时：

```text
explicitExit = false
↓
正常启动 backend
```

注意：

这是退出控制状态，不建议依赖多进程不安全 DataStore。

你已经有：

```text
hu_state.json
```

同样思路可以沿用文件通道。

---

# 30. 主进程是否需要 killProcess

理想情况：

```text
finishAffinity
+
Service 全停
+
任务全停
```

系统自然回收即可。

但如果你的产品明确要求：

```text
“退出 = 完全退出”
```

且确认所有持久化操作完成后，可以把：

```kotlin
Process.killProcess(Process.myPid())
```

作为最后兜底。

不过不要一开始就粗暴 kill。

正确顺序：

```text
先 graceful shutdown
再 kill fallback
```

否则：

```text
历史进度
缓存
日志
文件写入
```

可能来不及落盘。

---

# 31. :backend 进程不能只 kill 主进程

注意：

```text
Process.killProcess(Process.myPid())
```

在主进程里：

只杀主进程。

`:backend` 有独立 PID。

因此真正的彻底退出必须：

```text
先 stopService(BackendService)
```

让 backend 自己正常结束。

如果超时：

```text
backend 进程内执行 self-stop / exit
```

不要依赖主进程 kill 自己顺便带走 backend。

---

# 32. onTaskRemoved 处理

Service 增加：

```kotlin
override fun onTaskRemoved(rootIntent: Intent?) {
    ...
}
```

如果用户：

```text
从最近任务划掉 App
```

也应：

```text
停止播放器
停止 relay
停止 backend
```

但是要区分：

```text
系统暂时把 Activity 移除
```

和：

```text
用户明确退出
```

建议使用：

```text
explicitExit flag
```

辅助判断。

---

# 33. 不建议后台常驻播放器

除非移动版未来要做：

```text
后台音频播放
画中画
```

否则：

App 进入后台超过一定时间：

```text
直播 Player
Relay
Decrypt
```

应该主动降级或释放。

TV 版尤其可以更激进。

建议：

```text
后台 <30s：
保持

后台 30s~5min：
暂停 / 保留轻量状态

后台 >5min：
释放 Player + relay
保留播放上下文

回前台：
重新 resolve + 重建 Player
```

这样比长期保留一个容易腐化的直播会话稳定得多。

---

# 34. 为什么“退出再进就好”是关键线索

因为退出重进做了很多隐藏的 reset：

```text
ExoPlayer 重建
Codec 重建
Surface 重建
HTTP socket 重建
DNS 重查
HLS manifest 重拉
解密 session 重建
WebView 生命周期重建
Python 请求 Session 重建
Backend 状态重建
Coroutine 全取消
ViewModel 全重建
```

所以当前目标就是：

> 把“必须重启 App 才能发生的 reset”，拆成可以自动执行的局部 reset。

最终：

```text
播放器坏了
```

只重建播放器。

```text
解密池坏了
```

只重建解密池。

```text
backend 坏了
```

只重启 :backend。

不要让用户承担“杀 App”的成本。

---

# 35. 日志体系必须同步升级

新增统一 Tag：

```text
OTV_REFRESH
OTV_PLAYBACK
OTV_RECOVERY
OTV_BACKEND
OTV_DECRYPT
OTV_EXIT
```

---

# 36. 播放日志示例

正常：

```text
OTV_PLAYBACK session=81 state=PLAYING src=bb
```

卡顿：

```text
OTV_PLAYBACK session=81 stall=9120ms positionFrozen=true
```

恢复：

```text
OTV_RECOVERY session=81 level=2 action=RERESOLVE_SOURCE
```

新线路：

```text
OTV_RECOVERY session=81 oldUrlHash=8af1 newUrlHash=2c91 success=true
```

播放器重建：

```text
OTV_RECOVERY session=81 level=4 action=RECREATE_PLAYER
```

backend：

```text
OTV_RECOVERY session=81 level=6 action=RESTART_BACKEND
```

---

# 37. 退出日志

```text
OTV_EXIT reason=USER_REQUEST begin
OTV_EXIT refresh stopped
OTV_EXIT playback released
OTV_EXIT relay stopped
OTV_EXIT backend stop requested
OTV_EXIT backend stopped
OTV_EXIT activity finishAffinity
OTV_EXIT complete
```

这样测试时可以明确知道：

```text
到底是谁没退出。
```

---

# 38. 增加播放器健康指标

建议每个 session 记录：

```text
startupMs
bufferCount
bufferTotalMs
stallCount
reResolveCount
sourceSwitchCount
playerRecreateCount
decryptResetCount
backendRestartCount
fatalReason
```

先写本地日志即可。

不一定上传云端。

这样很容易发现：

```text
某种线路 20 分钟后特别容易 stale token
```

或：

```text
某机型 decoder 更容易假死。
```

---

# 39. Android 生命周期建议

统一：

```text
ProcessLifecycleOwner
```

监听：

```text
ON_START → APP_FOREGROUND
ON_STOP  → APP_BACKGROUND
```

前台：

```text
RefreshCoordinator.refreshStaleDomains()
PlaybackSupervisor.onForeground()
```

后台：

```text
RefreshCoordinator.pauseTimers()
PlaybackSupervisor.onBackground()
```

---

# 40. TV / Mobile 差异

## TV

建议：

```text
后台 30~60 秒以上
```

释放直播 Player。

因为 TV 一般没有后台播放必要。

---

## Mobile

如果未来保留：

```text
画中画
DLNA
后台播放
```

则根据状态判断。

例如 DLNA 正在投屏：

```text
不能因为 Activity 后台就停止 cast session
```

所以：

```text
ExitCoordinator
```

必须区分：

```text
APP_BACKGROUND
USER_EXIT
```

这两个绝对不是一回事。

---

# 41. Web 端对应方案

Web 没有 Android 进程退出问题，但需要：

```javascript
visibilitychange
online
focus
pageshow
```

回来时：

```text
刷新比赛
重新验证当前 stream URL
必要时重新建立 hls.js
```

如果 hls.js：

```text
MEDIA_ERROR
NETWORK_ERROR
```

优先：

```text
recoverMediaError()
startLoad()
```

失败：

```text
destroy()
↓
new Hls()
↓
重新 resolve stream
```

相当于 Android 的：

```text
RECREATE_PLAYER
```

---

# 42. Python Backend 缓存刷新

继续采用：

```text
TTL + staleTTL + SWR
```

主要 API 返回：

```json
{
  "data": {},
  "meta": {
    "version": "...",
    "fetchedAt": 0,
    "stale": false,
    "source": "..."
  }
}
```

UI 可以判断：

```text
接口请求成功
但其实仍然是 stale cache
```

避免把：

```text
“HTTP 200”
```

误认为：

```text
“拿到了最新数据”。
```

---

# 43. 比赛接口建议增加 source revision

例如：

```json
{
  "matchId": "...",
  "revision": "a83fd1",
  "sources": [...]
}
```

如果比赛线路发生变化：

```text
revision 改变
```

前端立即更新。

---

# 44. 真实播放 URL 不建议长时间持久缓存

尤其足球：

```text
真实 m3u8
签名 URL
解密结果
```

建议只做短缓存。

例如：

```text
30~60 秒
```

甚至只缓存：

```text
解析上下文
```

而不是长期缓存最终 HLS URL。

因为：

```text
最终 URL 经常是最容易过期的部分。
```

---

# 45. 推荐实施顺序

## P0：先解决“必须重启 App 才能恢复播放”

优先实现：

```text
PlaybackSupervisor
假播放检测
重新 resolve 当前 source
Player 真正 release/recreate
relay/decrypt session reset
```

这是当前用户体验影响最大的。

---

## P0：修正 App 退出

同时检查：

```text
BackendService START_STICKY
finishAffinity 后 backend 是否仍存活
是否有 WorkManager/Receiver 重新启动 backend
播放器/relay 是否仍存活
```

建立：

```text
ExitCoordinator
```

---

## P1：统一页面刷新

实现：

```text
RefreshCoordinator
onVisible
foreground refresh
network reconnect refresh
StateFlow immutable 更新
```

---

## P1：backend 自愈

实现：

```text
backend health
连续失败重启 :backend
```

这样彻底代替：

```text
用户退出 App → 再进入。
```

---

## P2：ETag / version

降低：

```text
10~15 秒比赛轮询
```

带来的流量。

---

## P2：完整观测

完善：

```text
OTV_REFRESH
OTV_RECOVERY
OTV_EXIT
```

后续所有类似问题都能快速定位。

---

# 46. 推荐代码结构

```text
android/.../data/
├── refresh/
│   ├── RefreshCoordinator.kt
│   ├── RefreshDomain.kt
│   ├── RefreshPolicy.kt
│   └── RefreshReason.kt
│
playback/
├── PlaybackSupervisor.kt
├── PlaybackSession.kt
├── PlaybackHealth.kt
├── PlaybackRecovery.kt
└── PlaybackFailure.kt

app/lifecycle/
├── AppLifecycleManager.kt
├── ExitCoordinator.kt
└── ShutdownReason.kt
```

共享层改动后：

```bash
python tools/sync_shared.py
```

必须保证 TV / Mobile 一致。

---

# 47. 建议重点检查现有代码位置

重点搜索：

```text
init {
```

确认是否存在：

```text
只初始化一次、不再刷新的 ViewModel。
```

搜索：

```text
MutableList
clear()
addAll()
```

确认是否存在：

```text
Compose 状态原地修改。
```

搜索：

```text
START_STICKY
```

确认：

```text
BackendService 是否会在退出后自动重启。
```

搜索：

```text
finish()
finishAffinity()
```

确认：

```text
所谓“退出”到底只结束了哪个 Activity。
```

搜索：

```text
ExoPlayer.Builder
release()
```

确认：

```text
播放故障时有没有真正重新创建 Player。
```

搜索：

```text
StreamDecryptServer
relay
engineEpoch
```

确认：

```text
旧 session 是否可能污染新 session。
```

---

# 48. 验收方案

## 测试 A：UI 实时刷新

```text
首页保持打开 10 分钟
```

验证：

```text
数据源变化后
无需退出页面
内容自动更新
```

---

## 测试 B：App 后台恢复

```text
打开首页
Home 键后台 20 分钟
重新进入
```

验证：

```text
页面立即显示缓存
随后自动刷新新数据
```

---

## 测试 C：直播 URL 失效

人为让当前 m3u8 返回：

```text
403
```

验证：

```text
自动重新 resolve
无需退出播放页
恢复播放
```

---

## 测试 D：Media3 假死

模拟：

```text
Player READY
但 position 不增长
```

验证：

```text
PlaybackSupervisor 自动发现
↓
recreate Player
↓
恢复
```

---

## 测试 E：WebView 渲染进程死亡

继续使用现有：

```text
kill -9 renderer
```

验证：

```text
slot 自动重建
当前播放可自动恢复
无需杀 App
```

---

## 测试 F：backend 异常

人为结束：

```text
:backend
```

验证：

```text
主 App 存活
BackendService 自动重建
健康检查通过
播放重新 resolve
```

---

## 测试 G：明确退出 App

用户执行：

```text
退出
```

ADB 验证：

```text
主进程不存在
:backend 不存在
播放器服务不存在
relay 端口不存在
8090 不再监听
8091 不再监听
```

这才叫：

```text
彻底退出。
```

---

# 49. ADB 验收建议

退出前：

```bash
adb shell ps -A | grep optimaltv
```

退出后再次：

```bash
adb shell ps -A | grep optimaltv
```

应不存在：

```text
com.qiubo.optimaltv.public
com.qiubo.optimaltv.public:backend
```

检查端口：

```bash
adb shell ss -ltnp | grep -E '8090|8091'
```

退出后应为空。

---

# 50. 最终目标

改造完成后：

## 页面数据

```text
不用退出 App
不用切 Tab
自动刷新
```

## 比赛直播

```text
线路过期
↓
自动重新解析

播放器假死
↓
自动重建

解密池异常
↓
局部重建

backend 异常
↓
只重启 :backend
```

用户不需要：

```text
退出 App
重新进入
```

才能恢复。

## 用户明确退出

```text
Activity
Player
Relay
Decrypt
BackendService
:backend
```

全部结束。

下一次打开：

```text
从干净状态启动。
```

---

# 51. 最终架构结论

My TV 应统一成三套核心机制：

```text
RefreshCoordinator
    解决“数据不变”

PlaybackSupervisor
    解决“播放一段时间后卡死”

ExitCoordinator
    解决“退出后仍残留异常后台状态”
```

再配合：

```text
AppLifecycleManager
Backend Health Check
Session Generation
SWR
StateFlow
Player Recreate
Relay / Decrypt Reset
Backend Process Restart
```

最终达到：

> **页面不用重进就能变新；直播不用重启 App 就能自愈；用户真正退出时，所有播放和后端资源都彻底释放。**

这比单纯提高“刷新频率”更适合 My TV 当前的架构，也更能解释并解决“退出再进就恢复”的问题。
