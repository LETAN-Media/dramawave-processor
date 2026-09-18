import re
with open('app/web/static/app.js', 'r') as f:
    content = f.read()

replacement = """  renderList() {
    const box = document.getElementById('episodes');
    if (!this.episodes.length) {
      box.innerHTML = '<p class="hint">Chưa có tập nào được crawl.</p>';
      return;
    }
    box.innerHTML = this.episodes.map((e) => {
      let badge = '';
      let sourcesHtml = '';
      if (e.sources && e.sources.length > 0) {
        let bestFree = null;
        for (const s of e.sources) {
            sourcesHtml += `<div class="hint">${s.provider}: ${s.locked ? 'LOCKED' : 'FREE'}</div>`;
            if (!s.locked && (!bestFree || s.provider === 'reelshort' || s.provider === 'dramabox')) {
                bestFree = s;
            }
        }
        if (bestFree) {
            badge = `<span class="badge free">FREE &middot; ${bestFree.provider}</span>`;
        } else {
            badge = '<span class="badge locked">LOCKED</span>';
        }
      } else {
          badge = e.locked ? '<span class="badge locked">LOCKED</span>' : '<span class="badge free">FREE</span>';
      }
      return `
      <label class="ep-item ${e.locked ? 'locked' : ''}">
        <input type="checkbox" value="${e.number}" ${e.locked ? 'disabled' : ''}>
        <div class="ep-info">
          <strong>Tập ${e.number}</strong>
          ${badge}
          <div class="ep-status ${e.yt_status || e.status}">${this.formatStatus(e.status, e.progress, e.yt_status)}</div>
        </div>
        ${sourcesHtml ? `<div class="ep-sources" style="font-size: 0.8em; margin-top: 4px; display: none;">${sourcesHtml}</div>` : ''}
      </label>`
    }).join('');
    
    // Add click event for expanding sources
    box.querySelectorAll('.ep-item').forEach(item => {
      item.addEventListener('click', (ev) => {
        if(ev.target.tagName !== 'INPUT') {
          const sources = item.querySelector('.ep-sources');
          if(sources) sources.style.display = sources.style.display === 'none' ? 'block' : 'none';
        }
      });
    });
  },"""

content = re.sub(r'  renderList\(\) \{.*?\}\,\n', replacement + "\n", content, flags=re.DOTALL)
with open('app/web/static/app.js', 'w') as f:
    f.write(content)
