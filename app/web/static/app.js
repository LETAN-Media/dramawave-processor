/* DramaWave Studio: fetch-based UI, no build step. */
'use strict';

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { Accept: 'application/json' } }, opts || {}));
  if (res.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); throw new Error('login'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : (d && d.message) || ('HTTP ' + res.status));
  }
  return data;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtDur(s) {
  if (s == null || isNaN(s)) return '';
  s = Math.round(s);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

function cardHTML(it) {
  const pid = esc(it.series_id || it.provider_series_id || '');
  const img = it.cover_url ? '<img src="' + esc(it.cover_url) + '" alt="" loading="lazy">' : '<div class="noposter">DW</div>';
  const sub = it.episode_count != null ? esc(it.episode_count) + ' tập' : '';
  return '<a class="card" href="/series/' + pid + '">' + img +
    '<div class="card-body"><div class="card-title">' + esc(it.title || pid) + '</div>' +
    '<div class="card-sub">' + sub + '</div></div></a>';
}

const StudioSearch = {
  async submit(ev) {
    ev.preventDefault();
    const q = document.getElementById('search-q').value.trim();
    const st = document.getElementById('search-state');
    const grid = document.getElementById('search-grid');
    if (!q) return;
    st.hidden = false; grid.innerHTML = '';
    st.textContent = 'Đang tìm... (API miễn phí có thể mất 30–60s lần đầu: Đang khởi động DramaWave API...)';
    try {
      const data = await api('/web/api/search?q=' + encodeURIComponent(q));
      const items = data.items || [];
      if (!items.length) { st.textContent = 'Không tìm thấy phim nào.'; return; }
      st.hidden = true;
      grid.innerHTML = items.map(cardHTML).join('');
    } catch (e) {
      st.textContent = 'Lỗi: ' + e.message;
    }
  },
};

const StudioSeries = {
  pid: null, episodes: [],
  init(pid) {
    this.pid = pid;
    this.load();
    document.getElementById('process-form').addEventListener('submit', (e) => this.start(e));
    document.querySelectorAll('#process-form [data-range]').forEach((b) =>
      b.addEventListener('click', () => this.preset(b.dataset.range)));
    // apply saved defaults
    try {
      const d = JSON.parse(localStorage.getItem('dw-defaults') || '{}');
      if (d.quality) document.getElementById('opt-quality').value = d.quality;
      if (d.voice) document.getElementById('opt-voice').value = d.voice;
      if (d.style) document.getElementById('opt-style').value = d.style;
      if (d.ytPrivacy) document.getElementById('opt-yt-privacy').value = d.ytPrivacy;
      if (d.ytMeta) document.getElementById('opt-yt-meta').value = d.ytMeta;
      if (d.ytAuto) document.getElementById('opt-yt').checked = true;
      this._ytDefaultChannel = d.ytChannel || '';
    } catch (e) { /* ignore */ }
    this.loadChannels();
  },
  async loadChannels() {
    try {
      const d = await api('/web/api/youtube/channels');
      const sel = document.getElementById('opt-yt-channel');
      sel.innerHTML = '<option value="">—</option>' + (d.items || []).map((c) =>
        '<option value="' + esc(c.id) + '">' + esc(c.youtube_channel_title || c.youtube_channel_id) + '</option>').join('');
      if (this._ytDefaultChannel) sel.value = this._ytDefaultChannel;
    } catch (e) { /* channels optional */ }
  },
  async load() {
    const st = document.getElementById('series-state');
    try {
      const d = await api('/web/api/series/' + encodeURIComponent(this.pid));
      this.episodes = d.episodes || [];
      st.hidden = true;
      document.getElementById('series-head').hidden = false;
      document.getElementById('process-form').hidden = false;
      document.getElementById('series-head').innerHTML =
        '<div class="series-head">' +
        (d.meta.cover_url ? '<img src="' + esc(d.meta.cover_url) + '" alt="">' : '') +
        '<div><h1>' + esc(d.meta.title || this.pid) + '</h1>' +
        '<p class="hint">' + d.unlocked + ' free / ' + d.total + ' tập</p></div></div>';
      const to = document.getElementById('to-ep');
      const firstLocked = this.episodes.find((e) => e.locked);
      to.value = Math.min(5, d.unlocked || 1);
      this.renderList();
      void firstLocked;
    } catch (e) {
      st.textContent = 'Lỗi: ' + e.message;
    }
  },
  renderList() {
    const box = document.getElementById('ep-list');
    box.innerHTML = this.episodes.map((e) =>
      '<label class="ep' + (e.locked ? ' locked' : '') + '">' +
      '<input type="checkbox" data-n="' + e.number + '"' + (e.locked ? ' disabled' : ' checked') + '>' +
      '<span class="t">Tập ' + e.number +
      (e.has_final ? ' ✓' : '') +
      (e.youtube === 'published' ? ' ✅ Published' : e.youtube === 'uploading' ? ' 🔄 Uploading' : e.youtube === 'failed' ? ' ⚠ YT Failed' : '') + '</span>' +
      '<span class="d">' + esc(fmtDur(e.duration)) + '</span>' +
      '<span class="badge ' + (e.locked ? 'locked' : 'free') + '">' + (e.locked ? 'LOCKED' : 'FREE') + '</span>' +
      '</label>').join('');
  },
  checked() {
    return Array.from(document.querySelectorAll('#ep-list input:checked')).map((c) => +c.dataset.n).sort((a, b) => a - b);
  },
  preset(kind) {
    const free = this.episodes.filter((e) => !e.locked).map((e) => e.number);
    if (kind === 'allfree' && free.length) {
      document.getElementById('from-ep').value = free[0];
      document.getElementById('to-ep').value = free[free.length - 1];
    } else {
      const parts = kind.split('-');
      document.getElementById('from-ep').value = parts[0];
      document.getElementById('to-ep').value = parts[1];
    }
  },
  async start(ev) {
    ev.preventDefault();
    const st = document.getElementById('process-state');
    const btn = document.getElementById('start-btn');
    st.hidden = false;
    const ytOn = document.getElementById('opt-yt').checked;
    const payload = {
      from_episode: +document.getElementById('from-ep').value,
      to_episode: +document.getElementById('to-ep').value,
      quality: document.getElementById('opt-quality').value,
      target_language: 'vi',
      voice: document.getElementById('opt-voice').value,
      translation_style: document.getElementById('opt-style').value,
      youtube: {
        enabled: ytOn,
        destination_id: document.getElementById('opt-yt-channel').value || null,
        privacy: document.getElementById('opt-yt-privacy').value,
        metadata_mode: document.getElementById('opt-yt-meta').value,
      },
    };
    if (ytOn && !payload.youtube.destination_id) {
      st.textContent = 'Lỗi: hãy chọn YouTube Channel để tự động đăng.';
      btn.disabled = false;
      return;
    }
    btn.disabled = true; st.textContent = 'Đang tạo jobs...';
    try {
      const d = await api('/web/api/series/' + encodeURIComponent(this.pid) + '/process', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      st.textContent = 'Đã tạo ' + d.jobs.length + ' jobs.' + (d.skipped && d.skipped.length ? ' Bỏ qua: ' + d.skipped.join(', ') : '');
      setTimeout(() => { location.href = '/jobs'; }, 800);
    } catch (e) {
      st.textContent = 'Lỗi: ' + e.message;
      btn.disabled = false;
    }
  },
};

const StudioJobs = {
  filter: 'all', timer: null,
  init() {
    document.querySelectorAll('.tabs button').forEach((b) =>
      b.addEventListener('click', () => {
        document.querySelectorAll('.tabs button').forEach((x) => x.classList.remove('on'));
        b.classList.add('on'); this.filter = b.dataset.f; this.load();
      }));
    this.load();
    this.timer = setInterval(() => this.load(true), 4000);
  },
  async load(quiet) {
    const st = document.getElementById('jobs-state');
    try {
      const d = await api('/web/api/jobs?status=' + this.filter);
      const items = d.items || [];
      st.hidden = items.length > 0;
      if (!items.length) st.textContent = 'Chưa có job nào.';
      document.getElementById('jobs-list').innerHTML = items.map((j) => {
        const name = esc((j.series_title || '') + ' · Tập ' + (j.episode_number == null ? '?' : j.episode_number));
        let badges = '';
        if (j.has_final) badges += ' <span class="badge free">Rendered</span>';
        if (j.youtube_badge === 'published') badges += ' <span class="badge free">Published</span>';
        else if (j.youtube_badge === 'uploading') badges += ' <span class="badge">Uploading</span>';
        else if (j.youtube_badge === 'failed') badges += ' <span class="badge locked">YouTube Failed</span>';
        return '<a class="job" href="/jobs/' + j.job_id + '"><div><b>' + name + '</b>' + badges + '</div>' +
          '<div class="meta"><span>' + esc(j.current_stage || j.status) + '</span><span>' + j.progress + '%</span></div>' +
          '<div class="progress"><div style="width:' + j.progress + '%"></div></div>' +
          (j.error_code ? '<p class="error">' + esc(j.error_code) + '</p>' : '') + '</a>';
      }).join('');
    } catch (e) { if (!quiet) st.textContent = 'Lỗi: ' + e.message; }
  },
};

const StudioJob = {
  timer: null,
  init(id) {
    this.id = id;
    this.load();
    this.timer = setInterval(() => this.load(), 3000);
  },
  async load() {
    const st = document.getElementById('job-state');
    try {
      const j = await api('/web/api/jobs/' + this.id);
      st.hidden = true;
      document.getElementById('job-body').hidden = false;
      document.getElementById('job-title').textContent = (j.series_title || '') + ' · Tập ' + (j.episode_number == null ? '?' : j.episode_number);
      document.getElementById('job-bar').style.width = j.progress + '%';
      document.getElementById('job-status').textContent = j.current_stage + ' · ' + j.progress + '%' +
        (j.asr_provider ? ' · ASR ' + j.asr_provider : '');
      document.getElementById('job-steps').innerHTML = j.steps.map((s) =>
        '<li class="' + s.state + '">' + esc(s.label) +
        '<span class="st">' + (s.seconds != null ? s.seconds.toFixed(1) + 's · ' : '') + s.state + '</span></li>').join('');
      const a = j.artifacts;
      let out = '';
      if (a.final) {
        out += '<video class="preview" controls playsinline preload="metadata" src="/web/media/' + this.id + '/final.mp4"></video><div class="dlrow">';
        out += '<a href="/web/media/' + this.id + '/final.mp4?download=1">Tải MP4</a>';
        if (a.vi_srt) out += '<a href="/web/media/' + this.id + '/source.vi.srt?download=1">Tải SRT Việt</a>';
        if (a.source_srt) out += '<a href="/web/media/' + this.id + '/source.original.srt?download=1">Tải SRT nguồn</a>';
        out += '</div>';
      } else {
        out = '<p class="hint">Chưa có file kết quả.</p>';
      }
      document.getElementById('job-output').innerHTML = out;
      const err = document.getElementById('job-error');
      if (j.status === 'failed' || j.status === 'youtube_upload_failed') {
        err.hidden = false;
        err.textContent = (j.error_code || 'FAILED') + ': ' + (j.error_message || '');
      } else err.hidden = true;
      await this.renderYouTube();
      const ytActive = (this._yt || []).some((p) =>
        ['queued', 'uploading', 'processing'].includes(p.upload_status));
      if ((j.status === 'completed' || j.status === 'failed' ||
           j.status === 'published' || j.status === 'youtube_upload_failed') && !ytActive) {
        clearInterval(this.timer);
      }
    } catch (e) { st.textContent = 'Lỗi: ' + e.message; clearInterval(this.timer); }
  },
  async renderYouTube() {
    const box = document.getElementById('job-youtube');
    try {
      const d = await api('/web/api/jobs/' + this.id + '/youtube');
      this._yt = d.publications || [];
      if (!this._yt.length) {
        box.innerHTML = '<p class="hint">Chưa đăng YouTube.</p>' + this.ytManualHTML();
        this.bindManual();
        return;
      }
      box.innerHTML = this._yt.map((p) => {
        let line = '<div class="panel"><b>' + esc(p.title || 'Video') + '</b>' +
          '<p class="hint">Kênh: ' + esc(p.channel_title || p.channel_id || '') +
          ' · Privacy: ' + esc(p.privacy || '') + '</p>' +
          '<p>Trạng thái: <b>' + esc(p.upload_status) + '</b>' +
          (p.upload_status === 'uploading' ? ' · ' + (p.upload_progress || 0) + '%' : '') + '</p>';
        if (p.youtube_url && (p.upload_status === 'published' || p.upload_status === 'processing')) {
          line += '<div class="dlrow"><a href="' + esc(p.youtube_url) + '" target="_blank" rel="noopener">Mở YouTube</a></div>';
        }
        if (p.error_code) line += '<p class="error">' + esc(p.error_code) + ': ' + esc(p.error_message || '') + '</p>';
        if (p.upload_status === 'failed' || p.upload_status === 'cancelled') {
          line += '<div class="dlrow"><button type="button" data-yt-retry="' + esc(p.destination_id) + '">Retry Upload</button></div>';
        }
        return line + '</div>';
      }).join('') + this.ytManualHTML();
      this.bindManual();
      box.querySelectorAll('[data-yt-retry]').forEach((b) =>
        b.addEventListener('click', () => this.retryUpload()));
    } catch (e) { box.innerHTML = '<p class="error">Lỗi: ' + esc(e.message) + '</p>'; }
  },
  ytManualHTML() {
    return '<div class="panel"><b>Đăng lên YouTube</b>' +
      '<div class="row"><select id="yt-manual-channel"><option value="">— chọn kênh —</option></select>' +
      '<select id="yt-manual-privacy"><option value="public">Public</option><option value="unlisted">Unlisted</option><option value="private">Private</option></select></div>' +
      '<div class="dlrow"><button type="button" id="yt-manual-btn">Đăng lên YouTube</button></div>' +
      '<p id="yt-manual-state" class="hint"></p></div>';
  },
  async bindManual() {
    const sel = document.getElementById('yt-manual-channel');
    if (!sel) return;
    try {
      const d = await api('/web/api/youtube/channels');
      sel.innerHTML = '<option value="">— chọn kênh —</option>' + (d.items || []).map((c) =>
        '<option value="' + esc(c.id) + '">' + esc(c.youtube_channel_title || c.youtube_channel_id) + '</option>').join('');
    } catch (e) { /* ignore */ }
    document.getElementById('yt-manual-btn').addEventListener('click', () => this.manualUpload());
  },
  async manualUpload() {
    const st = document.getElementById('yt-manual-state');
    const dest = document.getElementById('yt-manual-channel').value;
    if (!dest) { st.textContent = 'Hãy chọn kênh.'; return; }
    st.textContent = 'Đang tạo upload...';
    try {
      await api('/web/api/jobs/' + this.id + '/youtube/upload', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ destination_id: dest, privacy: document.getElementById('yt-manual-privacy').value }),
      });
      st.textContent = 'Đã xếp hàng upload.';
      this.load();
    } catch (e) { st.textContent = 'Lỗi: ' + e.message; }
  },
  async retryUpload() {
    try {
      await api('/web/api/jobs/' + this.id + '/youtube/retry', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      this.load();
    } catch (e) {
      document.getElementById('job-youtube').innerHTML = '<p class="error">Lỗi: ' + esc(e.message) + '</p>';
    }
  },
};

const StudioSettings = {
  async init() {
    const st = document.getElementById('settings-state');
    try {
      const d = await api('/web/api/provider-status');
      st.hidden = true;
      document.getElementById('settings-body').hidden = false;
      try {
        const saved = JSON.parse(localStorage.getItem('dw-defaults') || '{}');
        if (saved.quality) document.getElementById('set-quality').value = saved.quality;
        if (saved.voice) document.getElementById('set-voice').value = saved.voice;
        if (saved.style) document.getElementById('set-style').value = saved.style;
      } catch (e) { /* ignore */ }
      document.getElementById('set-save').addEventListener('click', () => {
        localStorage.setItem('dw-defaults', JSON.stringify({
          quality: document.getElementById('set-quality').value,
          voice: document.getElementById('set-voice').value,
          style: document.getElementById('set-style').value,
          ytChannel: document.getElementById('set-yt-channel').value,
          ytAuto: document.getElementById('set-yt-auto').value === 'on',
          ytPrivacy: document.getElementById('set-yt-privacy').value,
          ytMeta: document.getElementById('set-yt-meta').value,
        }));
        st.hidden = false; st.textContent = 'Đã lưu mặc định trên thiết bị này.';
      });
      await this.loadYouTube();
      const rows = [
        ['DramaWave API', d.dramawave_api.online ? 'Online' + (d.dramawave_api.latency_ms != null ? ' · ' + d.dramawave_api.latency_ms + 'ms' : '') : 'Offline' + (d.dramawave_api.error ? ' · ' + d.dramawave_api.error : '')],
        ['Processor', d.processor.online ? 'Online' : 'Offline'],
        ['ASR', 'JianYing ' + (d.asr.jianying_available ? '✓' : '✗') + ' · Whisper ' + (d.asr.whisper_available ? '✓' : '✗')],
        ['TTS', esc(d.tts.provider) + ' · ' + esc(d.tts.voice) + (d.tts.available ? ' ✓' : ' ✗')],
        ['Translation', esc(d.translation.primary_model) + ' / ' + esc(d.translation.fallback_model) + (d.translation.configured ? ' ✓' : ' ✗')],
        ['Concurrency', String(d.defaults.episode_concurrency)],
      ];
      document.getElementById('provider-status').innerHTML =
        rows.map((r) => '<dt>' + esc(r[0]) + '</dt><dd>' + r[1] + '</dd>').join('');
    } catch (e) { st.textContent = 'Lỗi: ' + e.message; }
  },
  async loadYouTube() {
    const st = document.getElementById('yt-state');
    try {
      const cfg = await api('/web/api/youtube/config');
      const ch = await api('/web/api/youtube/channels');
      const items = ch.items || [];
      st.hidden = true;
      document.getElementById('yt-list').innerHTML = items.length ? items.map((c) =>
        '<div class="panel"><b>' + esc(c.youtube_channel_title || c.youtube_channel_id) + '</b>' +
        '<p class="hint">Channel ID: ' + esc(c.youtube_channel_id) + '</p>' +
        '<p>Trạng thái: <b>' + (c.is_active ? 'Connected' : 'Disconnected') + '</b>' +
        (c.reauth_required ? ' · <span class="badge locked">Cần kết nối lại Google</span>' : '') + '</p>' +
        (c.last_error ? '<p class="error">' + esc(c.last_error) + '</p>' : '') +
        (c.is_active ? '<div class="dlrow"><button type="button" data-yt-disconnect="' + esc(c.id) + '">Disconnect</button></div>' : '') +
        '</div>').join('') : '<p class="hint">Chưa kết nối kênh nào.</p>';
      document.querySelectorAll('[data-yt-disconnect]').forEach((b) =>
        b.addEventListener('click', async () => {
          if (!confirm('Ngắt kết nối kênh này?')) return;
          await api('/web/api/youtube/channels/' + encodeURIComponent(b.dataset.ytDisconnect), { method: 'DELETE' });
          location.reload();
        }));
      const cb = document.getElementById('yt-callback');
      if (!cfg.oauth_configured) {
        cb.textContent = 'Chưa cấu hình OAuth (GOOGLE_CLIENT_ID/SECRET, YOUTUBE_REDIRECT_URI). Hãy thêm redirect URI này trong Google Cloud Console: ' +
          (cfg.callback_url || '(chưa đặt YOUTUBE_REDIRECT_URI)') + '. Liên hệ admin VPS.';
      } else {
        cb.textContent = 'Callback URI đã cấu hình: ' + cfg.callback_url;
      }
      const sel = document.getElementById('set-yt-channel');
      sel.innerHTML = '<option value="">—</option>' + items.filter((c) => c.is_active).map((c) =>
        '<option value="' + esc(c.id) + '">' + esc(c.youtube_channel_title || c.youtube_channel_id) + '</option>').join('');
      try {
        const saved = JSON.parse(localStorage.getItem('dw-defaults') || '{}');
        if (saved.ytChannel) sel.value = saved.ytChannel;
        if (saved.ytPrivacy) document.getElementById('set-yt-privacy').value = saved.ytPrivacy;
        if (saved.ytMeta) document.getElementById('set-yt-meta').value = saved.ytMeta;
        if (typeof saved.ytAuto === 'boolean') document.getElementById('set-yt-auto').value = saved.ytAuto ? 'on' : 'off';
      } catch (e) { /* ignore */ }
    } catch (e) { st.textContent = 'Lỗi YouTube: ' + e.message; }
  },
};
