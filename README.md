# 足球直播

足球赛事直播应用，覆盖 Android TV、Android 手机/平板、iOS、Windows 和网页端。用户可以查看比赛，选择该场比赛的可用信号源并进入播放器。

## 产品范围

- 足球比赛列表与比赛状态
- 单场比赛的直播信号源选择
- HLS 直播播放、故障恢复与必要的中继
- 足球直播请求和应用更新

## 工程结构

| 路径 | 用途 |
|---|---|
| `android/` | Android TV 客户端 |
| `internal/mobile/` | Android 手机和平板客户端 |
| `ios/` | SwiftUI iOS 客户端 |
| `desktop/` | Windows 桌面启动器 |
| `tools/backend-src/` | 网页端、足球接口及客户端内嵌 Python 后端 |
| `deploy/selfhosted/` | 服务端部署配置 |
| `docs/项目文档.md` | 架构、开发、验证和维护说明 |

## 本地验证

- 后端：从仓库根目录运行 `python -m unittest discover -s tools/backend-src/tests -p "test_*.py"`
- Android TV：在 `android/` 运行 `gradlew.bat testDebugUnitTest`
- Android 手机：在 `internal/mobile/` 运行 `gradlew.bat testDebugUnitTest`
- Windows：进入 `desktop/` 后运行 `python -m unittest discover -s tests -p "test_*.py"`
- 网页脚本语法：运行 `node --check tools/backend-src/www/app.js`

真实比赛可用性取决于上游赛程和信号源；模拟器或测试夹具通过不代表实体电视、手机或所有线路均已实播验收。
