// Run on a Naukri search results page. Adds matching roles to localStorage "jh_naukri".
(() => {
  const KW = /program|growth|strateg|consult|analyst|manager|sales|b2b|\bai\b|genai|founder|generalist|chief of staff|business operations|bizops|partnership|allianc|corporate development|\bgtm\b|go.to.market|revenue|customer success|transformation|associate|product|business development|category|key account|marketing|brand/i;
  const EX = /engineer|developer|scientist|devops|architect|designer|\bqa\b|test|security|finance|accountant|accounting|audit|tax|treasury|legal|compliance|\bhr\b|recruit|talent acquisition|payroll|intern|trainee|fresher|telecall|tele ?sales|field sales|store manager|warehouse|director|\bvp\b|vice president|\bhead\b|leadership|general manager|president|\bchief (?!of staff)|collection|underwrit|insurance advisor|relationship manager|branch manager/i;
  const ago = s => { s = (s || '').toLowerCase(); let n = 0; const m = s.match(/(\d+)\+?\s*day/); if (m) n = +m[1]; else if (/today|hour|just/.test(s)) n = 0; else if (/yesterday/.test(s)) n = 1; else n = 30; const d = new Date(Date.now() - n * 864e5); return d.toISOString().slice(0, 10); };
  const store = JSON.parse(localStorage.getItem('jh_naukri') || '{}');
  let added = 0, seen = 0;
  document.querySelectorAll('.srp-jobtuple-wrapper').forEach(w => {
    seen++;
    const a = w.querySelector('a.title'); if (!a) return;
    const t = a.getAttribute('title') || a.textContent.trim();
    if (!KW.test(t) || EX.test(t)) return;
    const q = sel => (w.querySelector(sel)?.getAttribute('title') || w.querySelector(sel)?.textContent || '').trim();
    const sal = q('.sal-wrap span[title], .sal span[title]');
    const m = sal.match(/([\d.]+)\s*-\s*([\d.]+)\s*Lacs/i);
    const id = w.getAttribute('data-job-id');
    store[id] = { c: q('a.comp-name'), t, l: q('.locWdth'), u: a.href.split('?')[0], d: ago(q('.job-post-day')), e: q('.expwdth'),
      j: q('.job-desc'), k: [...w.querySelectorAll('.tag-li')].map(x => x.textContent.trim()).join(', '),
      smin: m ? +m[1] * 1e5 : null, smax: m ? +m[2] * 1e5 : null };
    added++;
  });
  localStorage.setItem('jh_naukri', JSON.stringify(store));
  return { page: location.pathname + location.search, cards: seen, matched: added, total: Object.keys(store).length };
})()

// ---- EXPORT (run once after all searches, on any naukri.com page) ----
// Returns gzip+base64 of {collected, jobs:[...]} filtered to 1-5 yrs overlap and pay >= 20 LPA when stated.
// const store=JSON.parse(localStorage.getItem('jh_naukri')||'{}'); ... (see README in this folder)
