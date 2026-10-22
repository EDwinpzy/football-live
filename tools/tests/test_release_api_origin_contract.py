"""My TV Release 入口门槛契约测试（Task 2）。

两条腿走路：
1. 行为腿：把两套 build.gradle 里逐字节相同的 OTV-RELEASE-ORIGIN-GATE 代码块
    抽出来，用真实 Groovy 引擎（Gradle 自带的 groovy jar）配合最小 gradle/project
    桩求值，直接跑真实的接受/拒绝判定。这不是 mock——被求值的就是发布用的那段代码。
2. 静态腿：不依赖 Groovy 环境的对称性与资源断言，保证任何环境下都不会恒真。
"""

import glob
import hashlib
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
# 迁移 CA（CN=My TV Migration CA）PEM 去空白后的 sha256。两端必须一致；
# 换证书就要重新评估 IP 入口的信任面，所以锁死在这里。
MIGRATION_CA_SHA256 = "f94b7724a6183c6d3d6bbca1d10f8d597c1e3977236bd272df4b8ae923df3624"

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


def run_gate(module, url, release=True, allow_ip_release=None):
    """用真实 Groovy 引擎跑一遍仓库里那段门槛代码，返回 (是否接受, 说明)。"""
    lib = _groovy_lib()
    java = _java_exe()
    if lib is None or java is None:
        pytest.skip("本机没有可用的 Groovy/JRE，跳过行为腿（静态腿仍然执行）")
    # 逐条 put，避免把 Python dict 直接 repr 进 Groovy（单引号会被当成字符串定界符）。
    props = [("apiBaseUrl", url)] if url is not None else []
    if allow_ip_release is not None:
        props.append(("allowIpReleaseOrigin", str(allow_ip_release).lower()))
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
    env.pop("OTV_ALLOW_IP_RELEASE_ORIGIN", None)
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
    """不加 opt-in 时，迁移 IP 入口仍然必须被正式包拒绝（默认行为不变）。"""
    for bad in (MIGRATION_ORIGIN, MIGRATION_ORIGIN + "/"):
        accepted, reason = run_gate(module, bad)
        assert not accepted, f"{name} 竟然接受了迁移 CA 测试入口 {bad}"
        assert "迁移 CA" in reason, reason
        assert "allowIpReleaseOrigin" in reason, "拒绝文案要告诉发布者怎么显式 opt-in"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_accepts_migration_origin_only_with_explicit_opt_in(name, module):
    """显式 opt-in + 恰好等于迁移 origin → 正式包允许这个自建 VPS IP 入口。"""
    for good in (MIGRATION_ORIGIN, MIGRATION_ORIGIN + "/"):
        accepted, resolved = run_gate(module, good, allow_ip_release=True)
        assert accepted, f"{name} 带 -PallowIpReleaseOrigin=true 仍拒绝了 {good}：{resolved}"
        assert resolved == MIGRATION_ORIGIN, resolved


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_opt_in_does_not_unlock_other_ip_literals(name, module):
    """opt-in 只解锁那一个迁移 origin，绝不放行任意 IP 字面量。"""
    for bad in ("https://192.168.1.10:35208", "https://203.0.113.7:443"):
        accepted, reason = run_gate(module, bad, allow_ip_release=True)
        assert not accepted, f"{name} 的 opt-in 竟然放行了别的 IP {bad}"
        assert "IP" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_opt_in_flag_alone_does_not_bypass_https_or_path_checks(name, module):
    """opt-in 不能顺手放过 http / 路径 / userinfo / query 等其它门槛。"""
    for bad, marker in (("http://103.236.97.137:35208", "https"),
                        ("https://103.236.97.137:35208/api/", "路径")):
        accepted, reason = run_gate(module, bad, allow_ip_release=True)
        assert not accepted, f"{name} 的 opt-in 放过了 {bad}"
        assert marker in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_opt_in_rejects_migration_origin_with_any_path_suffix(name, module):
    """opt-in 只放行纯 origin；带任意路径后缀的迁移 origin 一律拒绝。

    回归点：早期实现把 opt-in 分支和 startsWith 判断合在一起，
    结果 -PallowIpReleaseOrigin=true 会顺带放行 https://<ip>:<port>/api/ 之类的前缀。
    """
    for bad in (MIGRATION_ORIGIN + "/api/", MIGRATION_ORIGIN + "/activate",
                MIGRATION_ORIGIN + "/whatever/deep/path"):
        accepted, reason = run_gate(module, bad, allow_ip_release=True)
        assert not accepted, f"{name} 的 opt-in 竟然放行了带路径的迁移入口 {bad}"
        assert "不得包含路径" in reason, reason


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_opt_in_rejects_migration_origin_with_credentials_or_query(name, module):
    """带 userinfo / query / fragment 的迁移 origin 同样不得被 opt-in 放行。"""
    cases = ("https://user:secret@103.236.97.137:35208",
             MIGRATION_ORIGIN + "?token=1",
             MIGRATION_ORIGIN + "#top")
    for bad in cases:
        accepted, reason = run_gate(module, bad, allow_ip_release=True)
        assert not accepted, f"{name} 的 opt-in 放过了带修饰的迁移入口 {bad}"

@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_opt_in_keeps_working_for_normal_domain(name, module):
    """opt-in 打开时，正式域名依旧照常通过（开关是加法不是替换）。"""
    accepted, resolved = run_gate(module, RELEASE_URL, allow_ip_release=True)
    assert accepted, f"{name} 带 opt-in 反而拒绝了合法正式域名：{resolved}"
    assert resolved == RELEASE_URL, resolved


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
               "Release 入口不得包含路径",
               # opt-in 通道：开关名与解锁条件两端必须一致，否则发布口径会分叉。
               "allowIpReleaseOrigin", "OTV_ALLOW_IP_RELEASE_ORIGIN", "otvIpReleaseOptIn"]
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
    # opt-in 只能是「放行已传入的那一个值」，不能变成 Release 的兜底默认值。
    opt_in = gate.split("def otvIpReleaseOptIn", 1)[1].split("def ", 1)[0]
    assert "true" in opt_in.lower() and "false" not in opt_in.lower(), \
        f"{name} 的 opt-in 判定必须显式等于字符串 'true'，缺省即关闭"
    fallback = gate.split("} else {", 1)[1]
    assert "otvIpReleaseOptIn" not in fallback, \
        f"{name} 的 Debug 兜底分支不应被 opt-in 影响"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_main_network_security_config_does_not_trust_migration_ca(name, module):
    """main 兜底配置只信任 system CA —— 不加 opt-in 时所有构建类型都用它。"""
    release_config = module / "src" / "main" / "res" / "xml" / "network_security_config.xml"
    text = release_config.read_text(encoding="utf-8")
    # 注释里可以说明迁移 CA 只在 debug / opt-in release 覆盖里，但配置本身不得再引用它。
    effective = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    assert "mytv_migration_ca" not in effective, \
        f"{name} 的 main 网络安全配置仍然信任迁移 CA"
    assert "103.236.97.137" not in effective, \
        f"{name} 的 main 网络安全配置仍然为 IP 入口开口"
    assert '<certificates src="system" />' in text
    root = ElementTree.parse(release_config).getroot()
    assert root.findall("domain-config") == [], \
        f"{name} 的 main 配置不应为迁移 CA 保留 domain-config"
    # 本机 HTTP 内嵌后端与直播流仍要放行，不能为了堵 CA 把 cleartext 关掉。
    assert root.find("base-config").attrib.get("cleartextTrafficPermitted") == "true"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_override_trusts_migration_ca_only_for_the_vps_ip(name, module):
    """opt-in 挂载的 IP 覆盖只为自建 VPS 那个 IP 信任迁移 CA，不做全量信任。"""
    config = module / "src" / "ipRelease" / "res" / "xml" / "network_security_config.xml"
    assert config.is_file(), f"{name} 缺少 IP 入口的网络安全配置覆盖（IP 入口会 TLS 失败）"
    root = ElementTree.parse(config).getroot()
    base = root.find("base-config")
    assert base is not None
    # 其余域名仍走系统 CA：base-config 不得把迁移 CA 加进全局信任锚。
    base_anchors = [item.attrib.get("src") for item in base.findall("trust-anchors/certificates")]
    assert base_anchors == ["system"], f"{name} 的 Release base-config 信任锚应只有 system：{base_anchors}"
    # 本机 HTTP 内嵌后端与直播流仍要放行。
    assert base.attrib.get("cleartextTrafficPermitted") == "true"

    # 只有迁移 origin 那一个 host 走迁移 CA，且不允许子域。
    host_configs = [block for block in root.findall("domain-config")
                    if block.findtext("domain") == "103.236.97.137"]
    assert len(host_configs) == 1, f"{name} 的 Release 覆盖必须恰好为 VPS IP 保留一段 domain-config"
    host_config = host_configs[0]
    assert host_config.attrib.get("cleartextTrafficPermitted") == "false"
    domain = host_config.find("domain")
    assert domain.attrib.get("includeSubdomains") == "false", \
        f"{name} 的 IP 信任范围不得扩散到子域"
    anchors = [item.attrib.get("src") for item in host_config.findall("trust-anchors/certificates")]
    assert anchors == ["@raw/mytv_migration_ca"], \
        f"{name} 的 IP 段信任锚应是迁移 CA：{anchors}"
    # 覆盖里除这一个 IP 外不得再出现别的 domain-config（避免悄悄扩面）。
    others = [block.findtext("domain") for block in root.findall("domain-config")
              if block.findtext("domain") != "103.236.97.137"]
    assert not others, f"{name} 的 Release 覆盖出现了额外域名：{others}"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_both_modules_ship_the_same_migration_ca_pem(name, module):
    """两端打包的迁移 CA 必须逐字节一致 —— IP 入口信任的是同一张证书。"""
    pem = module / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"
    assert pem.is_file(), f"{name} 缺少迁移 CA 资源，IP 入口无法完成 TLS 校验"
    text = pem.read_text(encoding="utf-8")
    assert "BEGIN CERTIFICATE" in text and "END CERTIFICATE" in text, f"{name} 的 CA 文件不是 PEM"
    # 主体/有效期固定：CN=My TV Migration CA，签发 2026-09-23，到期 2036-09-20。
    digest = hashlib.sha256(re.sub(r"\s+", "", text).encode("ascii")).hexdigest()
    assert digest == MIGRATION_CA_SHA256, \
        f"{name} 的迁移 CA 内容变了（{digest}），两端信任的证书不再一致，需重新评估 IP 入口"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_override_is_referenced_by_the_manifest_indirection(name, module):
    """IP 覆盖与 main/debug 同名，靠 manifest 的 @xml 间接引用 + 变体优先级才生效。"""
    manifest = module / "src" / "main" / "AndroidManifest.xml"
    assert 'android:networkSecurityConfig="@xml/network_security_config"' in \
        manifest.read_text(encoding="utf-8"), \
        f"{name} 的 manifest 必须指向 @xml/network_security_config，source set 覆盖才会生效"
    # main/debug/ipRelease 三个目录下的文件名必须一致，否则覆盖会退化成新增资源而非替换。
    for variant in ("main", "debug", "ipRelease"):
        path = module / "src" / variant / "res" / "xml" / "network_security_config.xml"
        assert path.is_file(), f"{name} 缺少 {variant} 网络安全配置"
        assert ElementTree.parse(path).getroot().tag == "network-security-config", \
            f"{name} 的 {variant} 配置根节点不对"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_ip_override_dir_is_only_wired_when_opted_in(name, module):
    """关键回归点：IP 覆盖目录必须由 opt-in 条件挂载，不得常驻 release 变体。

    如果这里改成静态 src/release 覆盖，那么「用正式域名发布、不传开关」的包里
    也会打进私有 CA 信任锚 —— 门槛形同虚设。
    """
    source = module.joinpath("build.gradle").read_text(encoding="utf-8")
    assert "sourceSets.release.res.srcDir 'src/ipRelease/res'" in source, \
        f"{name} 必须用条件 sourceSet 挂载 IP 覆盖"
    wiring = source.split("sourceSets.release.res.srcDir", 1)[0]
    assert "if (otvIpReleaseOptIn)" in wiring.rsplit("android {", 1)[-1], \
        f"{name} 的 IP 覆盖挂载必须位于 if (otvIpReleaseOptIn) 之内"
    # 不允许再有常驻的 src/release 覆盖（那会绕过 opt-in 直接把私有 CA 打进所有正式包）。
    stray = module / "src" / "release" / "res" / "xml" / "network_security_config.xml"
    assert not stray.exists(), \
        f"{name} 存在常驻 src/release 覆盖，会让未 opt-in 的正式包也信任迁移 CA"


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_debug_network_security_config_still_trusts_migration_ca(name, module):
    """Debug 覆盖文件保留迁移 CA，Debug 联调不被本次门槛打断。"""
    debug_config = module / "src" / "debug" / "res" / "xml" / "network_security_config.xml"
    assert debug_config.exists(), f"{name} 缺少 Debug 网络安全配置覆盖"
    text = debug_config.read_text(encoding="utf-8")
    assert "@raw/mytv_migration_ca" in text
    assert "103.236.97.137" in text


@pytest.mark.parametrize("name,module", GRADLE_MODULES.items(), ids=list(GRADLE_MODULES))
def test_release_config_keeps_endpoint_derivation(name, module):
    """入口只解析一次，各 BuildConfig endpoint 仍由它派生。"""
    source = module.joinpath("build.gradle").read_text(encoding="utf-8")
    endpoints = [("HOTUPDATE_ENDPOINT", "/hotupdate")]
    for field, suffix in endpoints:
        pattern = ("buildConfigField 'String', '%s', "
                   r"'\"' \+ apiBaseUrl \+ '%s\"'" % (field, suffix))
        assert re.search(pattern, source), \
            f"{name} 的 {field} 必须由受门槛约束的 apiBaseUrl 派生"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
