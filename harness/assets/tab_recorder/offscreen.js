let mediaRecorder = null;
let mediaStream = null;
let chunks = [];
let recording = null;

function slug(value) {
  return String(value || "tab-recording").normalize("NFKD")
    .replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").slice(0, 80)
    .toLowerCase() || "tab-recording";
}

function preferredMimeType() {
  return ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"]
    .find(type => MediaRecorder.isTypeSupported(type)) || "";
}

async function start(message) {
  if (mediaRecorder?.state === "recording") throw new Error("a tab is already recording");
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: {mandatory: {
      chromeMediaSource: "tab",
      chromeMediaSourceId: message.streamId,
      maxWidth: 1920,
      maxHeight: 1080,
      maxFrameRate: 30
    }}
  });
  chunks = [];
  recording = {tabId: message.tabId, title: message.title, started: Date.now()};
  const mimeType = preferredMimeType();
  const options = {videoBitsPerSecond: 8_000_000};
  if (mimeType) options.mimeType = mimeType;
  mediaRecorder = new MediaRecorder(mediaStream, options);
  mediaRecorder.ondataavailable = event => {
    if (event.data?.size) chunks.push(event.data);
  };
  mediaRecorder.onerror = event => console.error("MediaRecorder error", event.error);
  const tabId = recording.tabId;
  mediaRecorder.onstop = () => save().catch(async error => {
    console.error("Could not save recording", error);
    await chrome.runtime.sendMessage({type: "recording-error", tabId,
                                      error: error.message});
  });
  mediaRecorder.start(1000);
  return {ok: true, mimeType: mediaRecorder.mimeType};
}

async function save() {
  const current = recording;
  const blob = new Blob(chunks, {type: mediaRecorder?.mimeType || "video/webm"});
  mediaStream?.getTracks().forEach(track => track.stop());
  mediaStream = null;
  mediaRecorder = null;
  chunks = [];
  recording = null;
  if (!blob.size || !current) return;
  const url = URL.createObjectURL(blob);
  const stamp = new Date(current.started).toISOString().replace(/[:.]/g, "-");
  try {
    const response = await chrome.runtime.sendMessage({
      type: "save-recording",
      url,
      filename: `browser-harness-recordings/${stamp}-${slug(current.title)}.webm`,
    });
    if (!response?.ok) throw new Error(response?.error || "download did not start");
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
  await chrome.runtime.sendMessage({type: "recording-complete", tabId: current.tabId,
                                    bytes: blob.size});
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "offscreen") return false;
  if (message.type === "start") {
    start(message).then(sendResponse).catch(error => sendResponse({ok: false, error: error.message}));
    return true;
  }
  if (message.type === "stop") {
    if (mediaRecorder?.state === "recording") {
      mediaRecorder.stop();
      mediaStream?.getTracks().forEach(track => track.stop());
    }
    sendResponse({ok: true});
  }
  return false;
});
