# Browser Harness Tab Recorder

This optional unpacked Chrome extension records the active tab as a real WebM stream using
`chrome.tabCapture` and `MediaRecorder`.

1. Open `chrome://extensions`, enable Developer mode, and choose **Load unpacked**.
2. Select this `harness/assets/tab_recorder` directory.
3. Focus the tab to record and click the extension action. Option-Shift-R works when
   Chrome assigned the suggested extension shortcut.
4. Perform the browser-harness run. Click the action again to stop.

Chrome requires the first action to be a user gesture. While recording, the badge reads
`REC`. Finished files land in `Downloads/browser-harness-recordings/` as WebM at up to
1080p/30 fps and 8 Mbps. No page content, cookies, or credentials are sent anywhere.
