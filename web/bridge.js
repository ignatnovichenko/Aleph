// Telegram keyboard Mini Apps can return one action directly to their bot.
(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const snapshot = fragment.get('aleph');
  let decoded;
  let submitted = false;

  async function readState() {
    if (!decoded) {
      if (!snapshot || snapshot.length > 4096) throw new Error('Invalid plan link');
      const bytes = Uint8Array.from(atob(snapshot.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
      decoded = await new Response(stream).json();
    }
    return decoded;
  }

  window.AlephBridge = {
    active: () => Boolean(snapshot),
    async request(path, {method = 'GET', body = {}} = {}) {
      const state = await readState();
      if (method === 'GET') {
        if (path === '/api/state' || path === '/api/feeds') return state;
        if (path === '/api/calendar.ics') {
          const {demoCalendar} = await import('./store.js');
          return demoCalendar(state.tasks);
        }
        throw new Error('Unknown view');
      }
      const tg = window.Telegram?.WebApp;
      if (!tg || !fragment.has('tgWebAppVersion') && tg.platform === 'unknown') throw new Error('Open the plan from your Telegram chat');
      const payload = JSON.stringify({v: 1, method, path, body});
      if (new TextEncoder().encode(payload).length > 4096) throw new Error('Telegram allows up to 4096 bytes per action');
      if (submitted) throw new Error('Already submitted');
      tg.sendData(payload);
      submitted = true;
      return body;
    }
  };
})();
