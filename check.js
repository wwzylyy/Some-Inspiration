
// ── bookmarks ──
const STORAGE_KEY = 'anti-rec-bookmarks';

function loadBookmarks() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); }
  catch { return []; }
}
function saveBookmarks(list) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
}
function isBookmarked(url) {
  return loadBookmarks().some(b => b.url === url);
}
function toggleBookmark(item) {
  let list = loadBookmarks();
  const idx = list.findIndex(b => b.url === item.url);
  if (idx >= 0) { list.splice(idx, 1); }
  else { list.unshift(item); }
  saveBookmarks(list);
  updateBookmarkUI();
  renderDrawer();
  return idx < 0; // true = just added
}
function updateBookmarkUI() {
  const list = loadBookmarks();
  const el = document.getElementById('bookmarkCount');
  el.textContent = list.length;
  el.classList.toggle('has-items', list.length > 0);
}

function renderDrawer() {
  const list = loadBookmarks();
  const body = document.getElementById('drawerBody');
  if (!list.length) {
    body.innerHTML = '<div class="drawer-empty">还没有收藏<br><br>点击卡片上的 ⭐ 保存</div>';
    return;
  }
  body.innerHTML = list.map(item => `
    <div class="saved-item">
      <div class="saved-item-domain">${escHtml(item.domain)}</div>
      <div class="saved-item-title">${escHtml(item.title)}</div>
      <div class="saved-item-footer">
        <a class="saved-item-link" href="${escHtml(item.url)}" target="_blank" rel="noopener">阅读 →</a>
        <button class="saved-item-remove" onclick="removeSaved('${escHtml(item.url)}')">移除</button>
      </div>
    </div>
  `).join('') + '<button class="drawer-clear" onclick="clearAll()">清空收藏夹</button>';
}
function removeSaved(url) {
  let list = loadBookmarks();
  saveBookmarks(list.filter(b => b.url !== url));
  updateBookmarkUI();
  renderDrawer();
  // update card button if visible
  document.querySelectorAll('.save-btn').forEach(btn => {
    if (btn.dataset.url === url) {
      btn.classList.remove('saved');
      btn.textContent = '⭐ 收藏';
    }
  });
}
function clearAll() {
  saveBookmarks([]);
  updateBookmarkUI();
  renderDrawer();
  document.querySelectorAll('.save-btn.saved').forEach(btn => {
    btn.classList.remove('saved');
    btn.textContent = '⭐ 收藏';
  });
}

let drawerOpen = false;
function toggleDrawer() {
  drawerOpen = !drawerOpen;
  document.getElementById('drawer').classList.toggle('open', drawerOpen);
  document.getElementById('drawerOverlay').classList.toggle('open', drawerOpen);
  if (drawerOpen) renderDrawer();
}

// ── streaming recommend ──
let currentSource = null;
const pillState = {};

function startRecommend() {
  const bubble = document.getElementById('bubbleInput').value.trim();
  if (!bubble) return;

  if (currentSource) { currentSource.close(); currentSource = null; }

  const btn = document.getElementById('goBtn');
  const feed = document.getElementById('domainFeed');
  const pills = document.getElementById('domainPills');
  const header = document.getElementById('resultsHeader');
  const container = document.getElementById('cardContainer');
  const errorMsg = document.getElementById('errorMsg');

  btn.disabled = true;
  feed.classList.remove('visible');
  pills.innerHTML = '';
  header.classList.remove('visible');
  container.innerHTML = '';
  errorMsg.classList.remove('visible');

  const es = new EventSource(`/stream?bubble=${encodeURIComponent(bubble)}`);
  currentSource = es;

  es.addEventListener('status', e => {
    const d = JSON.parse(e.data);
    feed.classList.add('visible');
    pills.innerHTML = `<span style="color:#6b7280;font-size:0.82rem">${escHtml(d.msg)}</span>`;
  });

  es.addEventListener('domains', e => {
    const d = JSON.parse(e.data);
    pills.innerHTML = d.domains.map(name =>
      `<span class="domain-pill" id="pill-${cssId(name)}" data-name="${escHtml(name)}">
        <span class="pill-dot"></span>${escHtml(name)}
      </span>`
    ).join('');
  });

  es.addEventListener('searching', e => {
    const d = JSON.parse(e.data);
    // mark previously-searching ones as done
    document.querySelectorAll('.domain-pill.searching').forEach(p => {
      p.classList.remove('searching');
      p.classList.add('done');
    });
    const pill = document.getElementById('pill-' + cssId(d.domain));
    if (pill) pill.classList.add('searching');
  });

  es.addEventListener('card', e => {
    const item = JSON.parse(e.data);
    header.classList.add('visible');
    const div = document.createElement('div');
    div.innerHTML = buildCard(item);
    const card = div.firstElementChild;
    container.appendChild(card);
  });

  function cleanup() {
    btn.disabled = false;
    es.close();
    currentSource = null;
    document.querySelectorAll('.domain-pill.searching').forEach(p => {
      p.classList.remove('searching');
      p.classList.add('done');
    });
  }

  es.addEventListener('done', e => {
    cleanup();
  });

  es.addEventListener('error', e => {
    try {
      const d = JSON.parse(e.data);
      showError(d.msg);
    } catch {}
    cleanup();
  });

  es.onerror = () => {
    cleanup();
  };
}

function buildCard(item) {
  const saved = isBookmarked(item.url);
  const itemJson = escAttr(JSON.stringify(item));
  return `
    <div class="card">
      <div class="card-domain">${escHtml(item.domain)}</div>
      <div class="card-title">${escHtml(item.title)}</div>
      <div class="card-hook">"${escHtml(item.hook)}"</div>
      <div class="card-meta">
        <div class="meta-block">
          <div class="meta-label">为什么你不会自己找到</div>
          <div class="meta-text">${escHtml(item.why_wont_find)}</div>
        </div>
        <div class="meta-block">
          <div class="meta-label">隐藏的桥梁</div>
          <div class="meta-text">${escHtml(item.bridge)}</div>
        </div>
      </div>
      <div class="card-footer">
        <div class="score-badge">
          意外程度
          <span class="score-dots">
            ${Array.from({length:10},(_,i)=>`<span class="dot${i<item.score?' active':''}"></span>`).join('')}
          </span>
        </div>
        <div class="card-actions">
          <button
            class="save-btn${saved?' saved':''}"
            data-url="${escHtml(item.url)}"
            data-item="${itemJson}"
            onclick="handleSave(this)"
          >${saved?'⭐ 已收藏':'⭐ 收藏'}</button>
          <a class="read-link" href="${escHtml(item.url)}" target="_blank" rel="noopener">阅读 →</a>
        </div>
      </div>
    </div>`;
}

function handleSave(btn) {
  const item = JSON.parse(btn.dataset.item);
  const added = toggleBookmark(item);
  btn.classList.toggle('saved', added);
  btn.textContent = added ? '⭐ 已收藏' : '⭐ 收藏';
}

function showError(msg) {
  const el = document.getElementById('errorMsg');
  el.textContent = '出错了：' + msg;
  el.classList.add('visible');
}

function cssId(s) { return s.replace(/[^a-zA-Z0-9一-龥]/g,'_'); }
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s) {
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

document.getElementById('bubbleInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') startRecommend();
});

// init
updateBookmarkUI();
