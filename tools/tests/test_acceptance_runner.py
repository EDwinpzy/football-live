from __future__ import annotations

import importlib.util
import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "tools" / "qa" / "run_acceptance.py"
MATRIX_PATH = REPO_ROOT / "tools" / "qa" / "media_matrix.json"


def load_runner():
    assert RUNNER_PATH.is_file(), "acceptance runner is not implemented yet"
    spec = importlib.util.spec_from_file_location("mytv_acceptance_runner", RUNNER_PATH)
    assert spec and spec.loader, "acceptance runner cannot be imported"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_matrix() -> dict:
    assert MATRIX_PATH.is_file(), "acceptance matrix is not implemented yet"
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def write_png(path: Path, width: int, height: int) -> Path:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + (b"\x24\x48\x6c" * width)
    pixels = zlib.compress(row * height)
    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", pixels)
    image += chunk(b"IEND", b"")
    path.write_bytes(image)
    return path


def attach_passing_browser_result(tmp_path: Path, row: dict, *, image_size=(1440, 900)) -> Path:
    image = write_png(tmp_path / "browser.png", *image_size)
    screenshot = {
        "path": image.name,
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "width": image_size[0],
        "height": image_size[1],
    }
    row.update({
        "status": "PASS",
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "device_or_browser": "Chrome desktop",
        "browser_version": "Chrome 140",
        "viewport": "1440x900",
        "network_profile": "stable-wifi",
        "content_category": "catalog",
        "attempts": 1,
        "error_log_result": "NONE",
        "screenshot_paths": [image.name],
        "screenshot_reviewed": True,
    })
    evidence_path = tmp_path / "browser-evidence.json"
    evidence_path.write_text(json.dumps({
        "row_id": row["id"],
        "product_version": row["product_version"],
        "build_version": row["build_version"],
        "viewport": row["viewport"],
        "screenshots": [screenshot],
    }), encoding="utf-8")
    row["evidence_ref"] = evidence_path.name
    return evidence_path


def fill_longrun_measurements(row: dict, *, survival=120, started="2026-09-24T10:00:00Z", ended="2026-09-24T12:00:00Z") -> None:
    row.update({
        "status": "PASS",
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "device_or_browser": "Android TV test device",
        "network_profile": "stable-wifi",
        "content_category": "movie",
        "attempts": 1,
        "first_frame_ms": 430,
        "rebuffer_count": 0,
        "auto_switch_result": "0 switches; uninterrupted source",
        "survival_minutes": survival,
        "measurement_started_at": started,
        "measurement_ended_at": ended,
        "error_log_result": "NONE",
    })


def test_media_inventory_uses_unverified_slots_without_invented_sources():
    ledger = load_matrix()
    samples = ledger["media_samples"]
    targets = ledger["media_coverage_targets"]

    assert targets == {
        "movie": 10,
        "series": 10,
        "1080p": 5,
        "low_resolution_legacy_source": 5,
        "hls": 5,
        "direct_link": 5,
    }
    assert sum(sample["kind"] == "movie" for sample in samples) >= 10
    assert sum(sample["kind"] == "series" for sample in samples) >= 10
    for sample in samples:
        assert sample["status"] == "NOT-VERIFIED"
        assert sample["authorized_test"] is False
        assert sample["content_ref"] is None
        assert sample["source_ref"] is None
        assert sample["resolution"] is None
        assert sample["stream_type"] is None
        assert {run["product"] for run in sample["runs"]} == {"android_tv", "android_mobile", "web"}
        assert {run["duration_minutes"] for run in sample["runs"]} == {5, 30}
        assert all(run["status"] == "NOT-VERIFIED" for run in sample["runs"])
        assert all(run["product_version"] is None and run["build_version"] is None for run in sample["runs"])
        assert all(run["device_or_browser"] is None and run["viewport"] is None for run in sample["runs"])
        assert all(run["network_profile"] is None for run in sample["runs"])
        assert all(run["content_category"] == sample["kind"] for run in sample["runs"])
        assert all("screenshot_paths" in run and run["screenshot_paths"] == [] for run in sample["runs"])


def test_media_playback_recording_requires_per_product_runtime_evidence():
    ledger = load_matrix()
    required = {
        "product_version",
        "build_version",
        "device_or_browser",
        "viewport",
        "network_profile",
        "content_category",
        "attempts",
        "first_frame_ms",
        "rebuffer_count",
        "auto_switch_result",
        "survival_minutes",
        "error_log_ref",
        "screenshot_paths",
        "screenshot_reviewed",
    }
    for sample in ledger["media_samples"]:
        for run in sample["runs"]:
            assert required <= set(run)


def test_web_and_every_admin_workflow_are_required_at_both_viewports():
    ledger = load_matrix()
    browser_runs = ledger["browser_runs"]
    viewports = {"1440x900", "390x844"}

    assert {row["product"] for row in browser_runs} == {"web", "admin"}
    for product in ("web", "admin"):
        product_rows = [row for row in browser_runs if row["product"] == product]
        assert {row["viewport"] for row in product_rows} == viewports
        for viewport in viewports:
            rows = [row for row in product_rows if row["viewport"] == viewport]
            assert rows and all(row["required"] is True for row in rows)
            assert all(row["status"] == "NOT-VERIFIED" for row in rows)
            assert all(row["screenshot_required"] is True for row in rows)
            assert all(row["product_version"] is None for row in rows)
            assert all(row["build_version"] is None for row in rows)

    web_names = {row["feature"] for row in browser_runs if row["product"] == "web"}
    assert {
        "首页",
        "足球列表/筛选/焦点比赛",
        "片库分类/筛选/加载更多",
        "搜索",
        "详情/剧集",
        "真实媒体播放",
        "直连失败后的受控 relay/重试/换线",
        "本地收藏/历史/进度刷新恢复",
        "IPTV 分组/频道/切台",
        "未授权拦截",
        "隔离测试卡激活/续费/状态刷新",
    } <= web_names

    admin_names = {row["feature"] for row in browser_runs if row["product"] == "admin"}
    assert {
        "登录失败与成功",
        "会话刷新/权限",
        "总览",
        "激活日志筛选",
        "卡密查询/状态/解绑/售出/备注",
        "卡密补货/签发/CSV 下载",
        "热更与 APK 分片上传/版本清单/哈希/下载",
        "公告发布与读取",
        "电视源导入草稿/频道分组/Logo 编辑/探测/发布/版本/回滚/模式切换",
        "影视源发现/草稿/启停/排序/单源测试/全量测速/显式发布",
        "失败发布保留旧版本",
        "隔离 staging 写入后逐项读回",
        "卡密导入脚本最小权限校验",
        "卡密导入签名校验",
        "重复批次幂等和实际插入数与 UI 报告一致",
        "对象下载 GET/HEAD/Range/ETag/Last-Modified/缓存头/内容哈希",
    } <= admin_names


def test_acceptance_ledger_contains_all_current_platform_features_and_evidence_fields():
    ledger = load_matrix()
    rows = ledger["acceptance_runs"]
    assert {row["product"] for row in rows} >= {
        "android_tv",
        "android_mobile",
        "web",
        "admin",
        "selfhosted",
    }

    all_features = {row["feature"] for row in rows}
    assert {
        "首页",
        "片库",
        "筛选",
        "搜索",
        "详情",
        "收藏",
        "历史/进度",
        "episode/线路",
        "直连/relay",
        "自动换线",
        "VLC 回退",
        "续播",
        "IPTV",
        "电视源",
        "公告",
        "激活/license",
        "热更新/版本检查",
        "DLNA/投屏",
        "手势",
        "TV 遥控焦点/返回恢复",
    } <= all_features

    required_fields = {
        "product_version",
        "build_version",
        "device_or_browser",
        "viewport",
        "network_profile",
        "content_category",
        "attempts",
        "first_frame_ms",
        "rebuffer_count",
        "auto_switch_result",
        "survival_minutes",
        "error_log_ref",
        "screenshot_paths",
    }
    assert all(required_fields <= set(row) for row in rows)


def test_admin_write_workflows_are_staging_only_and_require_readback():
    ledger = load_matrix()
    admin_writes = [
        row for row in ledger["browser_runs"]
        if row["product"] == "admin" and row.get("write_readback_required")
    ]
    assert admin_writes
    for row in admin_writes:
        assert "隔离 staging" in row["data_scope"]
        assert row["staging_environment_ref"] is None
        assert row["readback_status"] == "NOT-VERIFIED"
        assert row["readback_evidence_ref"] is None


def test_representative_two_hour_playback_is_a_required_acceptance_row():
    ledger = load_matrix()
    rows = ledger["longrun_runs"]
    assert {row["product"] for row in rows} >= {"android_tv", "android_mobile", "web"}
    assert all(row["duration_minutes"] == 120 for row in rows)
    assert all(row["required"] is True and row["status"] == "NOT-VERIFIED" for row in rows)


def test_two_hour_runs_must_reference_a_completed_real_media_sample(tmp_path):
    runner = load_runner()
    fixture = runner._prepare_fixture_ledger(tmp_path)
    fixture_result = runner.evaluate_gate(fixture, artifact_root=tmp_path)
    assert not fixture_result.allowed
    assert any("fixture or synthetic" in blocker.lower() for blocker in fixture_result.blockers)

    fixture["longrun_runs"][0]["sample_id"] = "unknown-sample"
    result = runner.evaluate_gate(fixture, artifact_root=tmp_path)
    assert not result.allowed
    assert any("sample_id" in blocker and "unknown-sample" in blocker for blocker in result.blockers)


def test_fixture_media_runs_record_visual_and_playback_evidence(tmp_path):
    runner = load_runner()
    fixture = runner._prepare_fixture_ledger(tmp_path)
    for sample in fixture["media_samples"]:
        for run in sample["runs"]:
            assert run["status"] == "PASS"
            assert run["first_frame_ms"] >= 0
            assert run["rebuffer_count"] >= 0
            assert run["survival_minutes"] >= run["duration_minutes"]
            assert run["auto_switch_result"]
            assert run["screenshot_reviewed"] is True
            assert run["screenshot_paths"]
            assert run["evidence_ref"].startswith("fixture://")


def test_direct_relay_distribution_requires_both_real_counts():
    runner = load_runner()
    ledger = load_matrix()
    metric = next(item for item in ledger["performance_metrics"] if item["id"] == "direct-relay-distribution")
    assert metric["threshold"]["operator"] == "recorded_counts"
    metric.update({
        "status": "PASS",
        "actual": {"direct_count": 7, "relay_count": 3},
        "sample_count": 10,
        "product_version": "fixture-version",
        "build_version": "fixture-build",
        "device_or_browser": "fixture-device",
        "evidence_ref": "fixture://direct-relay",
    })
    result = runner.evaluate_gate(ledger)
    assert not any("direct/relay" in blocker.lower() for blocker in result.blockers)

    metric["actual"] = {"direct_count": 7}
    result = runner.evaluate_gate(ledger)
    assert any("direct/relay" in blocker.lower() and "count" in blocker.lower() for blocker in result.blockers)


def test_gate_rejects_missing_browser_workflow_or_lowered_media_targets(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    removed_feature = "对象上传权限/签名/分片完整性/重复对象处理"
    ledger["browser_runs"] = [
        row for row in ledger["browser_runs"]
        if not (row["product"] == "admin" and row["feature"] == removed_feature)
    ]
    ledger["media_coverage_targets"]["movie"] = 0
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert any("required browser workflow" in blocker.lower() and removed_feature in blocker for blocker in result.blockers)
    assert any("media coverage targets" in blocker.lower() for blocker in result.blockers)


def test_web_and_admin_rows_include_manual_e2e_steps_and_recordable_result_fields():
    ledger = load_matrix()
    for row in ledger["browser_runs"]:
        assert row["manual_steps"]
        assert row["expected_results"]
        assert row["execution_method"] == "manual_browser_e2e"
        assert row["status"] == "NOT-VERIFIED"

    importer_steps = next(
        row["manual_steps"] for row in ledger["browser_runs"]
        if row["product"] == "admin" and row["feature"] == "重复批次幂等和实际插入数与 UI 报告一致"
    )
    instructions = " ".join(importer_steps)
    assert "重复" in instructions
    assert "UI" in instructions
    assert "插入" in instructions


def test_apply_browser_result_records_evidence_and_rejects_wrong_viewport(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(row for row in ledger["browser_runs"] if row["id"] == "web-01-desktop")
    screenshot = tmp_path / "web-home.png"
    write_png(screenshot, 1440, 900)
    (tmp_path / "web-home-evidence.json").write_text(json.dumps({
        "row_id": row["id"],
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "viewport": "1440x900",
        "screenshots": [{
            "path": "web-home.png",
            "sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
            "width": 1440,
            "height": 900,
        }],
    }), encoding="utf-8")
    result = {
        "status": "PASS",
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "device_or_browser": "Chrome desktop",
        "browser_version": "Chrome 140",
        "viewport": "1440x900",
        "network_profile": "stable-wifi",
        "content_category": "catalog",
        "attempts": 1,
        "first_frame_ms": None,
        "rebuffer_count": None,
        "auto_switch_result": None,
        "survival_minutes": None,
        "error_log_result": "NONE",
        "screenshot_paths": [str(screenshot)],
        "screenshot_reviewed": True,
        "evidence_ref": "web-home-evidence.json",
    }
    runner.apply_run_result(ledger, row["id"], result, artifact_root=tmp_path)
    assert row["status"] == "PASS"
    assert row["result_history"][-1]["viewport"] == "1440x900"
    assert row["screenshot_paths"] == ["web-home.png"]

    with pytest.raises(ValueError, match="viewport"):
        runner.apply_run_result(ledger, row["id"], {**result, "viewport": "1280x720"}, artifact_root=tmp_path)


def test_show_steps_displays_manual_web_instructions_without_claiming_acceptance():
    completed = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--show-steps", "web-01-desktop"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    output = completed.stdout + completed.stderr
    assert "manual_browser_e2e" in output
    assert "NOT-VERIFIED" in output
    assert "does not execute or pass" in output.lower()


def test_record_result_command_persists_one_browser_row_and_keeps_global_gate_blocked(tmp_path):
    ledger_path = tmp_path / "media_matrix.json"
    ledger_path.write_text(MATRIX_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    screenshot = write_png(tmp_path / "web-home.png", 1440, 900)
    (tmp_path / "web-home-evidence.json").write_text(json.dumps({
        "row_id": "web-01-desktop",
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "viewport": "1440x900",
        "screenshots": [{
            "path": "web-home.png",
            "sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
            "width": 1440,
            "height": 900,
        }],
    }), encoding="utf-8")
    result_path = tmp_path / "web-home-result.json"
    result_path.write_text(json.dumps({
        "row_id": "web-01-desktop",
        "status": "PASS",
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "device_or_browser": "Chrome desktop",
        "browser_version": "Chrome 140",
        "viewport": "1440x900",
        "network_profile": "stable-wifi",
        "content_category": "catalog",
        "attempts": 1,
        "error_log_result": "NONE",
        "screenshot_paths": ["web-home.png"],
        "screenshot_reviewed": True,
        "evidence_ref": "web-home-evidence.json",
    }, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "--ledger", str(ledger_path),
            "--artifact-root", str(tmp_path),
            "--record-result", str(result_path),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "overall acceptance gate remains NOT-VERIFIED" in completed.stdout
    saved = json.loads(ledger_path.read_text(encoding="utf-8"))
    row = next(row for row in saved["browser_runs"] if row["id"] == "web-01-desktop")
    assert row["status"] == "PASS"
    assert row["screenshot_paths"] == ["web-home.png"]
    assert len(row["result_history"]) == 1


def test_gate_blocks_completed_admin_write_without_staging_readback(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(
        row for row in ledger["browser_runs"]
        if row["product"] == "admin" and row.get("write_readback_required")
    )
    screenshot = tmp_path / "staging-write.png"
    screenshot.write_bytes(b"fixture-screen")
    row.update({
        "status": "PASS",
        "product_version": "fixture-version",
        "build_version": "fixture-build",
        "browser_version": "fixture-browser",
        "device_or_browser": "fixture-browser",
        "network_profile": "fixture-network",
        "content_category": "staging-operation",
        "attempts": 1,
        "error_log_result": "NONE",
        "evidence_ref": "fixture://admin-operation",
        "screenshot_paths": [str(screenshot)],
        "screenshot_reviewed": True,
        "staging_environment_ref": "fixture-staging",
        "readback_status": "NOT-VERIFIED",
    })
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert any("readback" in blocker.lower() for blocker in result.blockers)


def test_gate_blocks_not_verified_rows_missing_versions_and_missing_screenshots(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert result.allowed is False
    assert any("NOT-VERIFIED" in blocker for blocker in result.blockers)

    row = next(row for row in ledger["acceptance_runs"] if row["required"])
    row["status"] = "PASS"
    row["screenshot_paths"] = [str(tmp_path / "not-created.png")]
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert any("product_version" in blocker or "build_version" in blocker for blocker in result.blockers)
    assert any("screenshot" in blocker.lower() for blocker in result.blockers)


def test_gate_rejects_performance_failures_network_gaps_and_nonzero_cloudbase_requests(tmp_path):
    runner = load_runner()
    ledger = load_matrix()

    failing_metric = ledger["performance_metrics"][0]
    failing_metric["status"] = "PASS"
    failing_metric["actual"] = failing_metric["threshold"]["value"] + 1
    failing_metric["sample_count"] = 10
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert any("threshold" in blocker.lower() for blocker in result.blockers)
    assert any("network" in blocker.lower() for blocker in result.blockers)
    assert any("VPS" in blocker or "vps" in blocker.lower() for blocker in result.blockers)

    ledger["deployment_evidence"]["cloudbase_runtime_requests"]["observed_requests"] = 1
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert any("CloudBase" in blocker for blocker in result.blockers)


def test_na_rows_require_a_product_capability_note_and_required_rows_cannot_be_na(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    optional_na = next(row for row in ledger["acceptance_runs"] if row["status"] == "N/A")
    optional_na["capability_note"] = ""
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert any("N/A" in blocker and "capability" in blocker.lower() for blocker in result.blockers)

    optional_na["capability_note"] = "This product does not provide this capability."
    optional_na["required"] = True
    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)
    assert any("required" in blocker.lower() and "N/A" in blocker for blocker in result.blockers)


def test_privacy_filter_redacts_credentials_signed_urls_and_emails_from_reports():
    runner = load_runner()
    fixture = {
        "status": "NOT-VERIFIED",
        "access_token": "fixture-secret-token",
        "admin_password": "fixture-password",
        "source_ref": "https://media.example.test/watch.m3u8?sign=fixture-signature",
        "error_log_ref": "Cookie: sessionid=fixture-cookie; Authorization: Basic fixture-basic",
        "operator": "tester@example.invalid",
    }
    rendered = runner.render_report(fixture)
    for sensitive_value in (
        "fixture-secret-token",
        "fixture-password",
        "fixture-signature",
        "fixture-cookie",
        "fixture-basic",
        "tester@example.invalid",
    ):
        assert sensitive_value not in rendered
    assert "NOT-VERIFIED" in rendered


def test_self_test_uses_fixtures_and_never_claims_real_product_acceptance():
    completed = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--self-test"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    output = completed.stdout + completed.stderr
    assert "fixture-only" in output.lower()
    assert "real product acceptance: pass" not in output.lower()
    assert "real Web/admin browser" not in output


@pytest.mark.parametrize(("collection", "row_id"), [
    ("acceptance_runs", "tv-01"),
    ("browser_runs", "web-01-desktop"),
    ("longrun_runs", "web-representative-120m"),
    ("media_samples", "sample-movie-01"),
    ("network_profiles", "stable-wifi"),
    ("performance_metrics", "metadata-api-p95"),
    ("service_runs", "importer-least-privilege"),
])
def test_gate_rejects_deleted_canonical_required_row(collection, row_id, tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    ledger[collection] = [row for row in ledger[collection] if row.get("id") != row_id]

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert not result.allowed
    assert any("canonical" in blocker.lower() and row_id in blocker for blocker in result.blockers)


def test_gate_rejects_duplicate_row_ids_and_missing_required_domains(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    ledger["browser_runs"].append(dict(ledger["browser_runs"][0]))
    ledger["acceptance_runs"] = [row for row in ledger["acceptance_runs"] if row["product"] != "selfhosted"]

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any("duplicate" in blocker.lower() and "web-01-desktop" in blocker for blocker in result.blockers)
    assert any("domain" in blocker.lower() and "selfhosted" in blocker for blocker in result.blockers)


def test_canonical_required_row_cannot_be_disabled_with_required_false(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    required_row = next(row for row in ledger["browser_runs"] if row["id"] == "web-01-desktop")
    required_row["required"] = False
    ledger["deployment_evidence"]["isolated_staging"]["required"] = False

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any("web-01-desktop cannot be marked optional" in blocker for blocker in result.blockers)
    assert any("isolated_staging cannot be marked optional" in blocker for blocker in result.blockers)


def test_two_hour_run_requires_full_survival_and_measured_timestamps(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = ledger["longrun_runs"][0]
    fill_longrun_measurements(row, survival=119, ended="2026-09-24T11:59:00Z")

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any("survival_minutes" in blocker and "120" in blocker for blocker in result.blockers)
    assert any("measured" in blocker.lower() and "120" in blocker for blocker in result.blockers)


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("first_frame_ms", None, "first_frame_ms"),
    ("first_frame_ms", 0, "first_frame_ms"),
    ("attempts", 0, "attempts"),
    ("auto_switch_result", "", "auto_switch_result"),
    ("product_version", "fixture-version", "synthetic"),
])
def test_two_hour_run_rejects_missing_zero_or_synthetic_measurements(field, value, expected, tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = ledger["longrun_runs"][0]
    fill_longrun_measurements(row)
    row[field] = value

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any(expected.lower() in blocker.lower() for blocker in result.blockers)


@pytest.mark.parametrize(("image_size", "image_bytes", "expected"), [
    ((1440, 900), b"not-an-image", "valid png image"),
    ((1280, 720), None, "1440x900"),
])
def test_browser_screenshot_must_be_valid_image_with_target_viewport(image_size, image_bytes, expected, tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(row for row in ledger["browser_runs"] if row["id"] == "web-01-desktop")
    attach_passing_browser_result(tmp_path, row, image_size=image_size)
    if image_bytes is not None:
        (tmp_path / "browser.png").write_bytes(image_bytes)

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any(expected.lower() in blocker.lower() for blocker in result.blockers)


@pytest.mark.parametrize("binding", ["wrong-row", "wrong-build", "missing-file"])
def test_pass_evidence_must_exist_and_bind_to_row_and_build(binding, tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(row for row in ledger["browser_runs"] if row["id"] == "web-01-desktop")
    evidence_path = attach_passing_browser_result(tmp_path, row)
    if binding == "missing-file":
        row["evidence_ref"] = "missing-evidence.json"
    else:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["row_id" if binding == "wrong-row" else "build_version"] = "other-row" if binding == "wrong-row" else "other-build"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any("evidence" in blocker.lower() and ("bind" in blocker.lower() or "exist" in blocker.lower()) for blocker in result.blockers)


def test_admin_write_requires_registered_isolated_staging_identity_not_production(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(row for row in ledger["browser_runs"] if row.get("write_readback_required"))
    row.update({
        "status": "PASS",
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "device_or_browser": "Chrome desktop",
        "browser_version": "Chrome 140",
        "network_profile": "stable-wifi",
        "content_category": "import",
        "attempts": 1,
        "error_log_result": "NONE",
        "evidence_ref": "missing.json",
        "screenshot_paths": [],
        "screenshot_reviewed": True,
        "staging_environment_ref": "production://primary",
        "readback_status": "PASS",
        "readback_evidence_ref": "missing-readback.json",
    })
    ledger["staging_environment_registry"] = {
        "id": "production-primary",
        "registered": True,
        "environment_type": "production",
        "environment_ref": "production://primary",
    }

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any("staging" in blocker.lower() and ("registered" in blocker.lower() or "production" in blocker.lower()) for blocker in result.blockers)


@pytest.mark.parametrize("matching", [False, True])
def test_staging_registry_requires_bound_registration_evidence(matching, tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    ledger["staging_environment_registry"].update({
        "registered": True,
        "status": "PASS",
        "environment_ref": "staging:acceptance-2026-09-27",
        "registration_evidence_ref": "staging-registration.json",
    })
    (tmp_path / "staging-registration.json").write_text(json.dumps({
        "identity_id": "acceptance-staging",
        "environment_type": "isolated_staging",
        "environment_ref": "staging:acceptance-2026-09-27" if matching else "staging:other",
        "registered": True,
    }), encoding="utf-8")

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    staging_blockers = [blocker for blocker in result.blockers if "staging identity" in blocker.lower()]
    assert bool(staging_blockers) is not matching


def test_fixture_ledger_cannot_pass_import_or_report_outside_self_test(tmp_path):
    runner = load_runner()
    fixture = runner._prepare_fixture_ledger(tmp_path)

    result = runner.evaluate_gate(fixture, artifact_root=tmp_path)
    assert not result.allowed
    with pytest.raises(ValueError, match="fixture|synthetic|self-test"):
        runner.render_report(fixture)
    with pytest.raises(ValueError, match="fixture|synthetic|self-test"):
        runner.write_ledger(tmp_path / "must-not-write.json", fixture)
    fixture_path = tmp_path / "fixture-ledger.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(ValueError, match="fixture|synthetic|self-test"):
        runner.load_ledger(fixture_path)
    with pytest.raises(ValueError, match="fixture|synthetic|self-test"):
        runner.apply_run_result(fixture, "web-01-desktop", {"status": "PASS", "product_version": "fixture-version"}, artifact_root=tmp_path)


def test_synthetic_evidence_file_is_rejected_from_normal_gate_and_report(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(row for row in ledger["browser_runs"] if row["id"] == "web-01-desktop")
    row["evidence_ref"] = "evidence.json"
    (tmp_path / "evidence.json").write_text(json.dumps({
        "row_id": row["id"],
        "screenshots": [{"path": "fixture-screen.png"}],
    }), encoding="utf-8")

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any("fixture or synthetic evidence" in blocker.lower() for blocker in result.blockers)
    with pytest.raises(ValueError, match="fixture|synthetic"):
        runner.render_report(ledger, artifact_root=tmp_path)


def test_blank_required_result_fields_cannot_pass(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(row for row in ledger["browser_runs"] if row["id"] == "web-01-desktop")
    row.update({"status": "PASS", "product_version": "", "build_version": "", "attempts": 0, "evidence_ref": ""})

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert not result.allowed
    assert any("missing product_version" in blocker for blocker in result.blockers)
    assert any("missing build_version" in blocker for blocker in result.blockers)
    assert any("attempts must be at least 1" in blocker for blocker in result.blockers)
    assert any("evidence_ref" in blocker for blocker in result.blockers)


def test_importer_service_rows_require_positive_security_assertions(tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    row = next(row for row in ledger["service_runs"] if row["id"] == "importer-least-privilege")
    row.update({
        "status": "PASS",
        "product_version": "2.6.0",
        "build_version": "build-20260924.1",
        "attempts": 1,
        "staging_environment_ref": "staging:acceptance-2026-09-24",
        "evidence_ref": "service-evidence.json",
        "checks": {
            "required_insert_allowed": True,
            "privileged_read_denied": False,
            "unauthorized_write_denied": True,
            "credential_scope_reviewed": True,
        },
    })
    ledger["staging_environment_registry"].update({
        "registered": True,
        "status": "PASS",
        "environment_ref": "staging:acceptance-2026-09-24",
    })

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert any("privileged_read_denied must be verified true" in blocker for blocker in result.blockers)
    assert any("evidence file does not exist" in blocker for blocker in result.blockers)


@pytest.mark.parametrize("failure_location", ["deployment", "nested_playback"])
def test_aggregate_fail_includes_deployment_and_nested_playback_rows(failure_location, tmp_path):
    runner = load_runner()
    ledger = load_matrix()
    if failure_location == "deployment":
        ledger["deployment_evidence"]["object_storage_contract"]["status"] = "FAIL"
    else:
        ledger["media_samples"][0]["runs"][0]["status"] = "FAIL"

    result = runner.evaluate_gate(ledger, artifact_root=tmp_path)

    assert result.status == "FAIL"


def test_importer_security_has_service_rows_and_ui_count_workflows_remain(tmp_path):
    ledger = load_matrix()
    service_rows = {row["id"]: row for row in ledger["service_runs"]}
    assert {"importer-least-privilege", "importer-signature-validation"} <= service_rows.keys()
    assert all(row["required"] is True and row["status"] == "NOT-VERIFIED" for row in service_rows.values())
    browser_features = {row["feature"] for row in ledger["browser_runs"] if row["product"] == "admin"}
    assert "重复批次幂等和实际插入数与 UI 报告一致" in browser_features
    assert "热更与 APK 分片上传/版本清单/哈希/下载" in browser_features
    assert all(
        "390x844" in row["expected_screenshot"]
        for row in ledger["browser_runs"]
        if row["viewport"] == "390x844"
    )


def test_mobile_rotation_requires_both_portrait_and_landscape_viewports():
    ledger = load_matrix()
    row = next(row for row in ledger["acceptance_runs"] if row["id"] == "mobile-rotation")
    assert "1080x2400" in row["viewport"]
    assert "2400x1080" in row["viewport"]
