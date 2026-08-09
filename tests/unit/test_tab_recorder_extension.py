import json
from pathlib import Path

ROOT = Path(__file__).parents[2] / "harness" / "assets" / "tab_recorder"


def test_manifest_declares_only_the_capture_capabilities():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"activeTab", "tabCapture", "offscreen", "downloads"}
    assert manifest["background"]["service_worker"] == "background.js"
    assert "content_scripts" not in manifest


def test_extension_uses_a_real_media_stream_and_no_remote_endpoint():
    source = (ROOT / "offscreen.js").read_text()
    assert "MediaRecorder" in source and "chromeMediaSourceId" in source
    assert "videoBitsPerSecond: 8_000_000" in source
    assert "fetch(" not in source and "XMLHttpRequest" not in source
    assert "chrome.downloads" not in source


def test_service_worker_owns_the_download_api_unavailable_to_offscreen_documents():
    background = (ROOT / "background.js").read_text()
    offscreen = (ROOT / "offscreen.js").read_text()
    assert 'message?.type === "save-recording"' in background
    assert "chrome.downloads.download" in background
    assert 'type: "save-recording"' in offscreen


def test_extension_exposes_a_cdp_test_adapter_without_confusing_target_and_tab_ids():
    source = (ROOT / "background.js").read_text()
    assert "toggleActive" in source
    assert "chrome.tabs.query({active: true, currentWindow: true})" in source
    assert "stop: stopRecording" in source
    assert "status: async" in source


def test_extension_recovers_when_the_service_worker_forgot_an_active_capture():
    background = (ROOT / "background.js").read_text()
    offscreen = (ROOT / "offscreen.js").read_text()
    assert "chrome.tabCapture.getCapturedTabs()" in background
    assert "active.some(item => item.tabId === tab.id)" in background
    assert "waitUntilReleased" in background
    assert "mediaStream?.getTracks().forEach(track => track.stop())" in offscreen
