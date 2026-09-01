// Passive history collector — runs in background
// Periodically saves recent browsing to chrome.storage.local

const COLLECT_INTERVAL_MS = 30 * 60 * 1000; // every 30 min
const MAX_HISTORY = 500;
const SKIP_PREFIXES = ["chrome://", "chrome-extension://", "file://",
                       "about:", "data:", "localhost", "127.0.0.1"];

async function collectHistory() {
  const microsecondsPerWeek = 7 * 24 * 60 * 60 * 1000 * 1000;
  const startTime = (Date.now() - 14 * 24 * 60 * 60 * 1000); // last 14 days

  const items = await chrome.history.search({
    text: "",
    startTime,
    maxResults: MAX_HISTORY,
  });

  const filtered = items
    .filter(h => h.title && !SKIP_PREFIXES.some(p => h.url.startsWith(p)))
    .map(h => ({
      url: h.url,
      title: h.title,
      visitCount: h.visitCount || 1,
      lastVisitTime: h.lastVisitTime,
    }));

  await chrome.storage.local.set({
    historySnapshot: filtered,
    snapshotTime: Date.now(),
  });

  console.log(`[anti-rec] collected ${filtered.length} history entries`);
}

// Collect on install and periodically
chrome.runtime.onInstalled.addListener(collectHistory);
setInterval(collectHistory, COLLECT_INTERVAL_MS);
