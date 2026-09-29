package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.prefs.AppSettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 云端 resolver 必须打 Release 注入的 VPS 入口，而不是本机内嵌后端。
 *
 * 现场（2026-09-29 实测）：VodRepository.fetchResolve 用的是
 * settings.hhkanBaseUrl，默认值 http://127.0.0.1:8090 —— 那是 App 自己的
 * 内嵌后端。于是 L1「云端预匹配」永远打不到服务端，每次播放都回落
 * local_detail，用本地实时匹配的原始线路顺序：实测 12 条线路逐条等满 5s
 * 首帧看门狗，点播放到出画 78.8 秒。
 *
 * 服务端 /vod/resolve/10000020 实测返回 MATCHED 且带完整 preferred
 * （猫眼，match_score 100），能力是有的，只是 App 没打到它。
 */
class CloudResolveBaseTest {

    private fun settings(apiBase: String, hhkanBase: String = "http://127.0.0.1:8090") =
        AppSettings(hhkanBaseUrl = hhkanBase, apiBaseUrl = apiBase)

    @Test
    fun `cloud resolve uses the release api origin`() {
        assertEquals("https://example.tv/web", CloudResolveBase.pick(settings("https://example.tv")))
    }

    @Test
    fun `trailing slashes are trimmed`() {
        assertEquals("https://example.tv/web", CloudResolveBase.pick(settings("https://example.tv/")))
    }

    @Test
    fun `the embedded loopback address never wins over a configured api origin`() {
        val base = CloudResolveBase.pick(settings("https://example.tv", "http://127.0.0.1:8090"))
        assertEquals("https://example.tv/web", base)
    }

    @Test
    fun `an origin that already ends with web is not doubled`() {
        assertEquals(
            "https://example.tv/web",
            CloudResolveBase.pick(settings("https://example.tv/web")),
        )
    }

    @Test
    fun `the ip release origin also gets the web prefix`() {
        // 无正式域名方案：Release 注入的是裸 IP 入口，同样只有 /web 通。
        assertEquals(
            "https://103.236.97.137:35208/web",
            CloudResolveBase.pick(settings("https://103.236.97.137:35208")),
        )
    }

    @Test
    fun `build config origin is used when the setting was never written`() {
        // DataStore 的 apiBaseUrl 默认空串，且启动时没有任何代码写入它
        // （实测：Release 注入了入口，App 却仍打本机内嵌后端，逐条试错 78.8s）。
        // 所以设置项为空时必须兜底到构建期常量，而不是继续回落内嵌后端。
        val fromBuildConfig = CloudResolveBase.pick(settings(""))
        assertTrue(
            "空设置时必须拿到构建期注入的入口，实际=$fromBuildConfig",
            fromBuildConfig.isNotBlank() && fromBuildConfig != "http://127.0.0.1:8090",
        )
    }

    @Test
    fun `loopback detection covers ipv6 and wildcard forms`() {
        // 这些写法都指向本机，不该被当成远端入口而加上 /web 前缀。
        // 直接测判定函数本身：pick() 在 Debug 下会先命中 BuildConfig，
        // 那是另一条分支（见 build config origin 用例）。
        for (loopback in listOf(
            "http://127.0.0.1:8090",
            "http://localhost:8090",
            "http://[::1]:8090",
            "http://0.0.0.0:8090",
        )) {
            assertFalse(
                "回环地址 $loopback 不该被判为远端",
                CloudResolveBase.isRemote(loopback),
            )
        }
        assertTrue(CloudResolveBase.isRemote("https://example.tv"))
    }
}
