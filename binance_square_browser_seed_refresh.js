const fs = require('fs');
const path = require('path');

const limit = Number(process.argv[2] || 50);
const startPage = Number(process.argv[3] || 1);
const maxPages = Number(process.argv[4] || 5);
const outPath = process.argv[5] || '/root/.openclaw/workspace/results/binance_square_seed_ids.json';

const POST_RE = /\/square\/post\/(\d+)/g;

async function fetchText(url) {
  const res = await fetch(url, { credentials: 'include' });
  if (!res.ok) {
    throw new Error(`fetch failed ${res.status} for ${url}`);
  }
  return await res.text();
}

async function main() {
  const ids = [];
  for (let page = startPage; page < startPage + maxPages && ids.length < limit; page++) {
    const url = `https://www.binance.com/en/square/sitemap/post/${page}`;
    const html = await fetchText(url);
    let match;
    while ((match = POST_RE.exec(html)) !== null) {
      const id = match[1];
      if (!ids.includes(id)) {
        ids.push(id);
        if (ids.length >= limit) break;
      }
    }
  }

  const payload = {
    updated_at: new Date().toISOString(),
    source: 'binance_square_sitemap_browser',
    start_page: startPage,
    max_pages: maxPages,
    limit,
    count: ids.length,
    ids,
  };

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(payload, null, 2), 'utf8');
  console.log(JSON.stringify(payload, null, 2));
}

main().catch((err) => {
  console.error(err.stack || String(err));
  process.exit(1);
});
