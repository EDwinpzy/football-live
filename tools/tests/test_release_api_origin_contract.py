"""My TV Release 入口门槛契约测试（Task 2）。

两条腿走路：
1. 行为腿：把两套 build.gradle 里逐字节相同的 OTV-RELEASE-ORIGIN-GATE 代码块
    抽出来，用真实 Groovy 引擎（Gradle 自带的 groovy jar）配合最小 gradle/project
    桩求值，直接跑真实的接受/拒绝判定。这不是 mock——被求值的就是发布用的那段代码。
2. 静态腿：不依赖 Groovy 环境的对称性与资源断言，保证任何环境下都不会恒真。
"""

import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[2]
GRADLE_MODULES = {
    "tv": ROOT / "android" / "app",
    "mobile": ROOT / "internal" / "mobile" / "app",
}
RELEASE_URL = "https://tv.example.com"
MIGRATION_ORIGIN = "https://103.236.97.137:35208"

GATE_BEGIN = "OTV-RELEASE-ORIGIN-GATE-BEGIN"
GATE_END = "OTV-RELEASE-ORIGIN-GATE-END"

HARNESS_PRELUDE = """
class GradleException extends RuntimeException {{
    GradleException(String message) {{ super(message) }}
}}

class StubProject {{
    Map props
    Object findProperty(String name) {{ return props.containsKey(name) ? props[name] : null }}
}}

def otvTaskNames = {task_names}
def otvProps = new LinkedHashMap()
{props_assignments}

def gradle = [startParameter: [taskNames: otvTaskNames]]
def project = new StubProject(props: otvProps)
"""

HARNESS_EPILOGUE = """
try {{
    // 门槛代码原样嵌在这里：与发布时同一段逻辑，只是换了执行壳。
{gate}
    binding.setVariable('otvResolvedBaseUrl', apiBaseUrl)
    println 'OTV_RESULT=accepted'
    println 'OTV_API_BASE_URL=' + binding.getVariable('otvResolvedBaseUrl')
}} catch (GradleException otvError) {{
    println 'OTV_RESULT=rejected'
    println 'OTV_ERROR=' + otvError.getMessage()
}}
"""


def _extract_gate(module):
    """从 build.gradle 抽出入口门槛代码块，并要求它唯一、完整。"""
    source = module.joinpath("build.gradle").read_text(encoding="utf-8")
    assert source.count(GATE_BEGIN) == 1, f"{module} 必须恰好有一个 {GATE_BEGIN} 标记"
    assert source.count(GATE_END) == 1, f"{module} 必须恰好有一个 {GATE_END} 标记"
    begin = source.index(GATE_BEGIN)
    end = source.index(GATE_END)
    assert begin < end, f"{module} 的门槛标记顺序颠倒"
    return source[source.index("\n", begin) + 1:end]


def _groovy_lib():
    override = os.environ.get("OTV_GROOVY_LIB")
    if override and glob.glob(os.path.join(override, "groovy-3*.jar")):
        return Path(override)
    candidates = []
    user = Path.home() / ".gradle" / "wrapper" / "dists"
    for pattern in ("gradle-*-bin/*/gradle-*/lib", "gradle-*-all/*/gradle-*/lib"):
        candidates.extend(glob.glob(str(user / pattern)))
    for candidate in candidates:
        if candidate and glob.glob(os.path.join(candidate, "groovy-3*.jar")):
            return Path(candidate)
    return None


def _groovy_classpath(lib):
    """Gradle 把 Groovy 依赖拆到 lib/ 与 lib/plugins/，两边都要进 classpath。

    用 Java 的通配符条目而不是展开成上千个绝对路径：后者会撑爆 Windows 命令行长度。
    """
    entries = [str(lib)]
    for sub in ("plugins", "groovy"):
        if os.path.isdir(os.path.join(str(lib), sub)):
            entries.append(os.path.join(str(lib), sub))
    return os.pathsep.join(f"{entry}{os.sep}*" for entry in entries)


def _java_exe():
    for key in ("OTV_JAVA_HOME", "JAVA_HOME"):
        home = os.environ.get(key)
        if home and Path(home, "bin", "java.exe").exists():
            return str(Path(home, "bin", "java.exe"))
    from shutil import which

    return which("java")


def run_gate(module, url, release=True):
    """用真实 Groovy 引擎跑一遍仓库里那段门槛代码，返回 (是否接受, 说明)。"""
    lib = _groovy_lib()
    java = _java_exe()
    if lib is None or java is None:
        pytest.skip("本机没有可用的 Groovy/JRE，跳过行为腿（静态腿仍然执行）")
    # 逐条 put，避免把 Python dict 直接 repr 进 Groovy（单引号会被当成字符串定界符）。
    props = [("apiBaseUrl", url)] if url is not None else []
    tasks = ["assembleRelease"] if release else ["assembleDebug"]
    gate = _extract_gate(module)
    props_assignments = "\n".join(
        "otvProps.put({0}, {1})".format(
            json.dumps(key), json.dumps(value, ensure_ascii=False) if value is not None else "null")
        for key, value in props
    )
    prelude = HARNESS_PRELUDE.format(
        task_names="[" + ", ".join(json.dumps(task) for task in tasks) + "]",
        props_assignments=props_assignments,
    )
    # 把仓库里那段门槛代码原样塞进 try：被求值的才是发布时真正跑的逻辑，
    # 而不是测试里另写的一份；try 只负责把 GradleException 翻译成可断言的输出。
    script = prelude + "\n" + HARNESS_EPILOGUE.format(gate=gate)
    env = dict(os.environ)
    env.pop("OTV_RELEASE_API_BASE_URL", None)
    # Windows 控制台默认 GBK，Groovy 打印的中文报错会解码失败。
    env["JAVA_TOOL_OPTIONS"] = "-Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8"
    result = subprocess.run(
        [java, "-cp", _groovy_classpath(lib), "groovy.ui.GroovyMain", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180,
    )
    if "OTV_RESULT=accepted" in result.stdout:
        tail = result.stdout.split("OTV_API_BASE_URL=", 1)[-1]
        return True, tail.splitlines()[0].strip()
    assert "OTV_RESULT=rejected" in result.stdout, (
        f"门槛求值没有给出结论\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return False, result.stdout.split("OTV_ERROR=", 1)[-1].splitlines()[0].strip()


# ---------------------------------------------------------------- 行为腿

@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_without_explicit_origin_fails_fast(name, module):
    accepted, reason = run_gate(module, None)
    assert not accepted, f"{name} 缺省 apiBaseUrl 时居然通过了 Release 门槛"
    assert "apiBaseUrl" in reason and "OTV_RELEASE_API_BASE_URL" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_rejects_ip_literal(name, module):
    for bad in ("https://103.236.97.137:35208/api/",
                "https://192.168.1.10/api/",
                "https://[2001:db8::1]/api/"):
        accepted, reason = run_gate(module, bad)
        assert not accepted, f"{name} 竟然接受了 IP 入口 {bad}"
        assert "IP" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_rejects_plain_http(name, module):
    accepted, reason = run_gate(module, "http://tv.example.com")
    assert not accepted, f"{name} 竟然接受了 http 入口"
    assert "https" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_rejects_userinfo(name, module):
    accepted, reason = run_gate(module, "https://user:secret@tv.example.com")
    assert not accepted, f"{name} 竟然接受了带 userinfo 的入口"
    assert "userinfo" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_rejects_query_and_fragment(name, module):
    cases = [("https://tv.example.com?token=1", "query"),
             ("https://tv.example.com" + chr(35) + "top", "fragment")]
    for bad, marker in cases:
        accepted, reason = run_gate(module, bad)
        assert not accepted, f"{name} 竟然接受了带 {marker} 的入口 {bad}"
        assert marker in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_rejects_migration_ca_test_origin(name, module):
    for bad in (MIGRATION_ORIGIN, MIGRATION_ORIGIN + "/"):
        accepted, reason = run_gate(module, bad)
        assert not accepted, f"{name} 竟然接受了迁移 CA 测试入口 {bad}"
        assert "迁移 CA" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_rejects_wrong_api_path(name, module):
    """入口必须是纯 origin：任何路径都要 fail fast（端点由 /activate 等固定路径拼出）。"""
    for bad in ("https://example.com/api/", "https://tv.example.com/v1/activate"):
        accepted, reason = run_gate(module, bad)
        assert not accepted, f"{name} 竟然接受了带路径的入口 {bad}"
        assert "不得包含路径" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_accepts_verified_public_domain(name, module):
    for good in ("https://tv.example.com", "https://tv.example.com/"):
        accepted, resolved = run_gate(module, good)
        assert accepted, f"{name} 拒绝了合法正式域名 {good}：{resolved}"
        assert resolved == "https://tv.example.com", resolved


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_debug_keeps_the_local_migration_test_entry(name, module):
    """Debug 仍能落到本机 IP + 迁移 CA 测试入口，正式包才拦。"""
    accepted, resolved = run_gate(module, None, release=False)
    assert accepted, f"{name} 的 Debug 联调入口被误伤：{resolved}"
    assert resolved == MIGRATION_ORIGIN, resolved


# ---------------------------------------------------------------- 静态腿

def test_tv_and_mobile_share_the_same_release_gate_rules():
    """两套工程必须逐字节使用同一段规则与同一套报错文案。"""
    tv, mobile = (_extract_gate(module) for module in GRADLE_MODULES.values())
    assert tv == mobile, "TV 与移动版的 Release 入口门槛代码块出现分叉"
    markers = ["拒绝迁移 CA / IP 测试入口", "Release 入口不得包含 userinfo",
               "Release 入口不得包含 query", "Release 入口不得使用 IP 字面量",
               "Release 入口不得包含路径"]
    for marker in markers:
        assert marker in tv, f"门槛缺少拒绝条件：{marker}"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_has_no_ip_default_fallback(name, module):
    source = module.joinpath("build.gradle").read_text(encoding="utf-8")
    assert "defaultApiBaseUrl" not in source, \
        f"{name} 仍保留 IP 默认入口，Release 缺省就不会 fail fast"
    gate = _extract_gate(module)
    assert "otvMigrationTestOrigin" in gate
    assert "OTV_RELEASE_API_BASE_URL" in gate, "正式域名必须能从统一环境变量读到"
    release_branch = gate.split("if (otvReleaseRequested)", 1)[1]
    assert MIGRATION_ORIGIN not in release_branch.split("} else {", 1)[0]


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_network_security_config_does_not_trust_migration_ca(name, module):
    """Release 打包的网络安全配置只信任 system CA。"""
    release_config = module / "src" / "main" / "res" / "xml" / "network_security_config.xml"
    text = release_config.read_text(encoding="utf-8")
    # 注释里可以说明迁移 CA 已迁到 debug，但配置本身不得再引用它。
    effective = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    assert "mytv_migration_ca" not in effective, \
        f"{name} 的 Release 网络安全配置仍然信任迁移 CA"
    assert "103.236.97.137" not in effective, \
        f"{name} 的 Release 网络安全配置仍然为 IP 测试入口开口"
    assert '<certificates src="system" />' in text
    root = ElementTree.parse(release_config).getroot()
    assert root.findall("domain-config") == [], \
        f"{name} 的 Release 配置不应再为迁移 CA 保留 domain-config"
    # 本机 HTTP 内嵌后端与直播流仍要放行，不能为了堵 CA 把 cleartext 关掉。
    assert root.find("base-config").attrib.get("cleartextTrafficPermitted") == "true"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_debug_network_security_config_still_trusts_migration_ca(name, module):
    """Debug 覆盖文件保留迁移 CA，Debug 联调不被本次门槛打断。"""
    debug_config = module / "src" / "debug" / "res" / "xml" / "network_security_config.xml"
    assert debug_config.exists(), f"{name} 缺少 Debug 网络安全配置覆盖"
    text = debug_config.read_text(encoding="utf-8")
    assert "@raw/mytv_migration_ca" in text
    assert "103.236.97.137" in text
    manifest = module / "src" / "main" / "AndroidManifest.xml"
    assert 'android:networkSecurityConfig="@xml/network_security_config"' in \
        manifest.read_text(encoding="utf-8"), \
        f"{name} 的 manifest 必须继续指向同名资源，Debug 覆盖才会生效"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_config_keeps_endpoint_derivation(name, module):
    """入口只解析一次，各 BuildConfig endpoint 仍由它派生。"""
    source = module.joinpath("build.gradle").read_text(encoding="utf-8")
    endpoints = [("LICENSE_ENDPOINT", "/activate"),
                 ("HOTUPDATE_ENDPOINT", "/hotupdate"),
                 ("IPTV_LATEST_URL", "/iptv/latest")]
    for field, suffix in endpoints:
        pattern = ("buildConfigField 'String', '%s', "
                   r"'\"' \+ apiBaseUrl \+ '%s\"'" % (field, suffix))
        assert re.search(pattern, source), \
            f"{name} 的 {field} 必须由受门槛约束的 apiBaseUrl 派生"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
