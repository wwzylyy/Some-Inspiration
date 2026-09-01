const APP_URL = "http://localhost:8768";

let currentBubble = "";

async function init() {
  const { historySnapshot, snapshotTime, savedBubble } =
    await chrome.storage.local.get(["historySnapshot", "snapshotTime", "savedBubble"]);

  const count = historySnapshot ? historySnapshot.length : 0;
  document.getElementById("histCount").textContent = count;

  if (savedBubble) {
    showBubble(savedBubble);
  }
}

function showBubble(bubble) {
  const box = document.getElementById("bubbleBox");
  box.textContent = bubble;
  box.classList.remove("empty");
  currentBubble = bubble;
  document.getElementById("sendBtn").disabled = false;
}

function setStatus(msg, cls = "") {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = "status " + cls;
}

async function analyze() {
  const btn = document.getElementById("analyzeBtn");
  btn.disabled = true;
  setStatus("正在分析浏览历史...", "loading");

  const { historySnapshot } = await chrome.storage.local.get("historySnapshot");
  if (!historySnapshot || historySnapshot.length === 0) {
    // Collect now
    setStatus("正在收集浏览记录...", "loading");
    await chrome.runtime.sendMessage({ action: "collect" });
    await new Promise(r => setTimeout(r, 2000));
  }

  const snap = (await chrome.storage.local.get("historySnapshot")).historySnapshot || [];
  if (!snap.length) {
    setStatus("没有找到浏览记录", "err");
    btn.disabled = false;
    return;
  }

  try {
    const resp = await fetch(`${APP_URL}/import-history`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history: snap }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showBubble(data.bubble);
      await chrome.storage.local.set({ savedBubble: data.bubble });
      setStatus(`已从 ${data.count} 条记录分析完成`, "ok");
    } else {
      setStatus(data.detail || "分析失败", "err");
    }
  } catch (e) {
    setStatus("连接失败，请确认反推荐引擎已启动", "err");
  }
  btn.disabled = false;
}

async function sendToApp() {
  if (!currentBubble) return;
  try {
    // Open the app and fill in the bubble via URL param
    await chrome.tabs.create({
      url: `${APP_URL}?bubble=${encodeURIComponent(currentBubble)}`,
    });
    setStatus("已在新标签页打开 →", "ok");
  } catch (e) {
    setStatus("打开失败：" + e.message, "err");
  }
}

// Listen for collect trigger from popup
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.action === "collect") {
    // background handles it
  }
});

init();
