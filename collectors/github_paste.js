// Run on a github.com "edit file" or "new file" page. Replaces the editor content with the
// gunzipped base64 text and commits. Usage: (paste into localStorage 'jh_up2', then)
//   await new Function(localStorage.getItem('jh_up2'))('<gzip base64>')
return (async (b64, doCommit) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const txt = await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  for (let i = 0; i < 20 && !document.querySelector('.cm-content'); i++) await new Promise(r => setTimeout(r, 500));
  const v = document.querySelector('.cm-content').cmTile.view;
  v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: txt } });
  await new Promise(r => setTimeout(r, 800));
  const ok = v.state.doc.toString() === txt; let committed = false;
  if (ok && doCommit !== false) {
    [...document.querySelectorAll('button')].find(b => /Commit changes/.test(b.textContent)).click();
    await new Promise(r => setTimeout(r, 1500));
    const d = document.querySelector('[role=dialog]');
    [...d.querySelectorAll('button')].find(x => x.textContent.trim() === 'Commit changes').click();
    await new Promise(r => setTimeout(r, 4000)); committed = true;
  }
  return { ok, lines: v.state.doc.lines, committed, url: location.href };
})(arguments[0], arguments[1]);
