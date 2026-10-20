/*
  auth.js — 모든 화면이 함께 쓰는 로그인 상태 처리 (index.html, a.html, mypage.html, log.html, signin.html)

  ★ 진짜 로그인 여부는 서버 세션 쿠키(hm_session, HttpOnly)가 정한다. 자바스크립트는 그 쿠키를 읽을 수 없어서
    /auth/me 로 물어본다. localStorage 의 isLoggedIn / userName 은 기존 화면 코드가 상단바를 그릴 때
    쓰던 값이라, 서버 답에 맞춰 같이 갱신만 해 준다(표시용. 인증에 쓰지 않는다).

  화면에서 쓰는 것
    HM_AUTH.ready            → Promise<{logged_in, name, email, created_at}>  (페이지 로드 때 한 번 물어본 결과)
    HM_AUTH.login(email, pw) / HM_AUTH.signup({email, password, name, agree})
    HM_AUTH.logout(next)     → 서버 세션을 끊고 next 로 이동
    HM_AUTH.loginUrl(next)   → 'log.html?next=...'
    HM_AUTH.safeNext(v, def) → next 값이 우리 사이트의 .html 일 때만 그대로, 아니면 def (외부 주소로 튕기는 것 방지)

  파일로 직접 열거나(file://) 주소에 ?mock=1 이 있으면 서버 없이 흉내만 낸다.
*/
(function () {
  const MOCK = location.protocol === 'file:' || new URLSearchParams(location.search).get('mock') === '1';
  const BASE = (window.HM_CONFIG && window.HM_CONFIG.BACKEND_URL) || '';

  async function call(path, opt = {}) {
    const init = { credentials: 'include', method: opt.method || 'GET', headers: {} };
    if (opt.json !== undefined) {
      init.method = opt.method || 'POST';
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opt.json);
    }
    const res = await fetch(BASE + path, init);
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const e = new Error(data?.error?.code || String(res.status));
      e.code = data?.error?.code || String(res.status);
      throw e;
    }
    return data;
  }

  /* 기존 화면 코드와 같은 키로 표시값을 맞춘다 */
  function remember(me) {
    try {
      if (me && me.logged_in) {
        localStorage.setItem('isLoggedIn', 'true');
        localStorage.setItem('userName', me.name);
      } else {
        localStorage.removeItem('isLoggedIn');
        localStorage.removeItem('userName');
      }
    } catch (_) {}
  }

  /* 상단바 — 세 화면 모두 같은 id 를 쓴다 (guestBtn / userStatusBtn / userNameSlot) */
  function paint(me) {
    const guest = document.getElementById('guestBtn');
    const user  = document.getElementById('userStatusBtn');
    const slot  = document.getElementById('userNameSlot');
    if (!guest || !user) return;
    const on = !!(me && me.logged_in);
    guest.style.display = on ? 'none' : '';
    user.style.display  = on ? 'inline-flex' : 'none';
    if (slot && on) slot.innerText = me.name + ' 님';
    // 로그인 아이콘을 누르면 지금 화면으로 돌아오게
    if (!on && guest.tagName === 'A') guest.href = loginUrl(location.pathname.split('/').pop() + location.hash);
  }

  function safeNext(v, def) {
    return /^[A-Za-z0-9_\-]+\.html(\?mock=1)?(#[A-Za-z0-9_\-]+)?$/.test(v || '') ? v : def;
  }
  function loginUrl(next) {
    const n = safeNext(next, '');
    return 'log.html' + (n ? '?next=' + encodeURIComponent(n) : '');
  }

  /* ── MOCK: 서버 없이 화면만 ── */
  const mock = {
    me()  { const n = localStorage.getItem('userName'); return n ? { logged_in: true, name: n, email: 'demo@hairmatch.kr', created_at: new Date().toISOString() } : { logged_in: false }; },
    login(email, pw) { if (pw.length < 8) { const e = new Error('BAD_CREDENTIALS'); e.code = e.message; throw e; } return { logged_in: true, name: email.split('@')[0], email }; },
    signup(b) { return { logged_in: true, name: b.name, email: b.email }; },
  };

  async function fetchMe() {
    if (MOCK) return mock.me();
    try { return await call('/auth/me'); }
    catch (_) {                                          // 서버가 꺼져 있으면 기존 표시값을 믿는다
      const n = localStorage.getItem('isLoggedIn') === 'true' && localStorage.getItem('userName');
      return n ? { logged_in: true, name: n, offline: true } : { logged_in: false, offline: true };
    }
  }

  const ready = fetchMe().then(me => {
    if (!me.offline) remember(me);
    const go = () => paint(me);
    document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', go) : go();
    return me;
  });

  window.HM_AUTH = {
    MOCK, ready, safeNext, loginUrl, paint,
    async login(email, password) {
      const me = MOCK ? mock.login(email, password) : await call('/auth/login', { json: { email, password } });
      remember(me); return me;
    },
    async signup(body) {
      const me = MOCK ? mock.signup(body) : await call('/auth/signup', { json: body });
      remember(me); return me;
    },
    async logout(next = 'index.html') {
      if (!MOCK) { try { await call('/auth/logout', { method: 'POST' }); } catch (_) {} }
      remember(null);
      location.href = next;
    },
    call,
  };
})();
