// Run on any naukri.com page after the searches. Returns gzip+base64 of {collected, jobs:[...]}
// with roles first collected in the last 26 hours (older ones are already on the dashboard),
// posted within 7 days, experience starting at <= 5 yrs, and pay >= 20 LPA when stated.
// Also prunes the browser store of roles older than 10 days.
(async () => {
  const store = JSON.parse(localStorage.getItem('jh_naukri') || '{}');
  const now = Date.now(), iso = n => new Date(now - n * 864e5).toISOString().slice(0, 10);
  for (const [id, j] of Object.entries(store)) if ((j.d || '') < iso(10)) delete store[id];
  localStorage.setItem('jh_naukri', JSON.stringify(store));
  const jobs = Object.values(store)
    .filter(j => (j.ts || 0) >= now - 26 * 36e5 && (j.d || '9') >= iso(7))
    .filter(j => !(j.smax && j.smax < 2000000))
    .filter(j => { const m = (j.e || '').match(/(\d+)/); return !m || +m[1] <= 5; })
    .map(j => { const x = { c: j.c, t: j.t, l: (j.l || '').split(',').slice(0, 2).join(',').slice(0, 40),
      u: j.u.replace('https://www.naukri.com/job-listings-', '~'), d: j.d, e: j.e, k: (j.k || '').split(', ').slice(0, 4).join(', ') };
      if (j.smin) x.smin = j.smin; if (j.smax) x.smax = j.smax; return x; });
  const txt = JSON.stringify({ collected: new Date().toISOString(), jobs });
  const gz = await new Response(new Blob([txt]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer();
  let s = ''; new Uint8Array(gz).forEach(b => s += String.fromCharCode(b));
  return { n: jobs.length, stored: Object.keys(store).length, b64: btoa(s) };
})()
