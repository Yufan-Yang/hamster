// Report code positions only: no error message, stack, page URL, media title or account data.
(() => {
  let sent = 0;
  const seen = new Set();
  function report(filename,line,error) {
    if (sent >= 3 || !filename || !line) return;
    try {
      const url = new URL(filename, location.origin);
      if (url.origin !== location.origin || !(url.pathname === '/' || /^\/static\/[a-z_]+\.js$/.test(url.pathname))) return;
      const name = ['TypeError','ReferenceError','SyntaxError','RangeError'].includes(error?.name) ? error.name : 'Error';
      const body = {file:url.pathname, line, name}, key = JSON.stringify(body);
      if (seen.has(key)) return;
      seen.add(key); ++sent;
      fetch('/api/health/client', {method:'POST', headers:{'Content-Type':'application/json'}, body:key, keepalive:true}).catch(() => {});
    } catch (_) {}
  }
  window.addEventListener('error', event => report(event.filename,event.lineno,event.error));
  window.addEventListener('unhandledrejection', event => {
    try {
      for (const frame of String(event.reason?.stack || '').matchAll(/(https?:\/\/[^\s)]+):(\d+):(\d+)/g)) {
        report(frame[1],Number(frame[2]),event.reason);
      }
    } catch (_) {}
  });
})();
