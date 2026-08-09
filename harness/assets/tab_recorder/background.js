const OFFSCREEN_URL = "offscreen.html";
let recordingTabId = null;
let lastError = null;

async function ensureOffscreenDocument() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: [chrome.offscreen.Reason.USER_MEDIA],
    justification: "Encode the explicitly selected tab with MediaRecorder"
  });
}

async function setBadge(tabId, active) {
  await chrome.action.setBadgeBackgroundColor({tabId, color: active ? "#c62828" : "#666"});
  await chrome.action.setBadgeText({tabId, text: active ? "REC" : ""});
  await chrome.action.setTitle({tabId, title: active ? "Browser Harness: recording"
                                                      : "Start or stop Browser Harness recording"});
}

async function capturedTabs() {
  return chrome.tabCapture.getCapturedTabs();
}

async function waitUntilReleased(tabId, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const captures = await capturedTabs();
    if (!captures.some(item => item.tabId === tabId &&
        (item.status === "active" || item.status === "pending"))) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
}

async function stopRecording(tabId = recordingTabId) {
  if (tabId == null) return;
  if (await chrome.offscreen.hasDocument()) {
    await chrome.runtime.sendMessage({target: "offscreen", type: "stop"});
  }
  recordingTabId = null;
  await setBadge(tabId, false);
  await waitUntilReleased(tabId);
}

async function toggleRecording(tab) {
  lastError = null;
  if (!tab?.id) return;
  const captures = await capturedTabs();
  const active = captures.filter(item => item.status === "active" ||
                                               item.status === "pending");
  if (recordingTabId === tab.id || active.some(item => item.tabId === tab.id)) {
    await stopRecording(tab.id);
    return;
  }
  if (recordingTabId != null) await stopRecording(recordingTabId);
  for (const capture of active) await stopRecording(capture.tabId);
  await ensureOffscreenDocument();
  const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: tab.id});
  const response = await chrome.runtime.sendMessage({
    target: "offscreen", type: "start", streamId, tabId: tab.id,
    title: tab.title || "tab-recording"
  });
  if (!response?.ok) throw new Error(response?.error || "recorder did not start");
  recordingTabId = tab.id;
  await setBadge(tab.id, true);
}

chrome.action.onClicked.addListener(tab => {
  toggleRecording(tab).catch(async error => {
    lastError = error?.message || String(error);
    if (tab?.id) {
      await chrome.action.setBadgeBackgroundColor({tabId: tab.id, color: "#ef6c00"});
      await chrome.action.setBadgeText({tabId: tab.id, text: "ERR"});
      await chrome.action.setTitle({tabId: tab.id,
                                    title: `Browser Harness error: ${lastError}`});
    }
    console.error("Browser Harness recorder:", error);
  });
});

chrome.tabs.onRemoved.addListener(tabId => {
  if (recordingTabId === tabId) stopRecording().catch(console.error);
});

// DevTools-driven validation can invoke this with Runtime.evaluate(userGesture=true).
// Chrome still enforces tabCapture's gesture permission; this merely exposes a stable
// extension-side function instead of depending on profile-specific keyboard shortcuts.
globalThis.browserHarnessRecorder = {
  toggle: (tabId, title = "tab-recording") => toggleRecording({id: tabId, title}),
  toggleActive: async () => {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    return toggleRecording(tab);
  },
  stop: stopRecording,
  status: async () => ({recordingTabId, lastError,
                        offscreen: await chrome.offscreen.hasDocument()})
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "save-recording") {
    chrome.downloads.download({
      url: message.url,
      filename: message.filename,
      saveAs: false,
      conflictAction: "uniquify"
    }).then(downloadId => sendResponse({ok: true, downloadId}))
      .catch(error => sendResponse({ok: false, error: error.message}));
    return true;
  }
  if (message?.type === "recording-error") {
    lastError = message.error || "recording could not be saved";
    recordingTabId = null;
    if (message.tabId) {
      chrome.action.setBadgeBackgroundColor({tabId: message.tabId, color: "#ef6c00"});
      chrome.action.setBadgeText({tabId: message.tabId, text: "ERR"});
      chrome.action.setTitle({tabId: message.tabId,
                              title: `Browser Harness error: ${lastError}`});
    }
    return false;
  }
  if (message?.type === "toggle-from-page" && sender.tab) {
    toggleRecording(sender.tab)
      .then(() => sendResponse({ok: true}))
      .catch(error => {
        lastError = error?.message || String(error);
        sendResponse({ok: false, error: lastError});
      });
    return true;
  }
  if (message?.type === "recording-complete" && message.tabId === recordingTabId) {
    const tabId = recordingTabId;
    recordingTabId = null;
    setBadge(tabId, false).catch(console.error);
  }
  return false;
});
