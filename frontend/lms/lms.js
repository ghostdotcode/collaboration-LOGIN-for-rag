/* ══════════════════════════════════════════════════════════════════
   Leave Management System — shared client.

   Handles the SSO hand-off from the chatbot, API access and the small
   amount of DOM plumbing both pages need. No build step and no
   framework, to match the existing chatbot frontend.
   ══════════════════════════════════════════════════════════════════ */

(function (global) {
  'use strict';

  // The LMS API is a separate service (default :8001). Overridable at
  // runtime so a deployment behind one ingress can point at /api/v1.
  const DEFAULT_API_BASE = 'http://localhost:8001/api/v1';
  const API_BASE =
    global.LMS_API_BASE ||
    localStorage.getItem('lms_api_base') ||
    DEFAULT_API_BASE;

  // Chatbot session (localStorage, set by frontend/login.js).
  const CHATBOT_TOKEN_KEY = 'access_token';
  // LMS session. sessionStorage, not localStorage: the token dies with the
  // tab, which limits the blast radius if anything ever injects script here.
  const LMS_TOKEN_KEY = 'lms_access_token';

  class ApiError extends Error {
    constructor(message, status, code, details) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.details = details || {};
    }
  }

  function chatbotToken() {
    return localStorage.getItem(CHATBOT_TOKEN_KEY);
  }

  function lmsToken() {
    return sessionStorage.getItem(LMS_TOKEN_KEY);
  }

  async function request(path, options) {
    const opts = options || {};
    const headers = { Accept: 'application/json' };
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';

    const token = opts.token || lmsToken();
    if (token) headers.Authorization = 'Bearer ' + token;

    let response;
    try {
      response = await fetch(API_BASE + path, {
        method: opts.method || 'GET',
        headers: headers,
        // Cookies carry the refresh token for same-site deployments.
        credentials: 'include',
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      });
    } catch (networkError) {
      throw new ApiError(
        'Could not reach the leave service. Is the LMS API running?',
        0,
        'network_error'
      );
    }

    if (response.status === 204) return null;

    let payload = null;
    try {
      payload = await response.json();
    } catch (parseError) {
      payload = null;
    }

    if (!response.ok) {
      const error = (payload && payload.error) || {};
      throw new ApiError(
        error.message || 'Request failed (' + response.status + ').',
        response.status,
        error.code || 'http_error',
        error.details
      );
    }
    return payload;
  }

  /**
   * Establish an LMS session.
   *
   * Prefers an existing LMS token, otherwise exchanges the chatbot's token
   * (both are signed with the same secret) so the user never logs in twice.
   */
  async function ensureSession() {
    if (lmsToken()) return lmsToken();

    const bridgeToken = chatbotToken();
    if (!bridgeToken) {
      throw new ApiError(
        'Please sign in to the HR assistant first.',
        401,
        'no_session'
      );
    }

    const result = await request('/auth/sso/exchange', {
      method: 'POST',
      body: { chatbot_token: bridgeToken },
      token: null,
    });
    sessionStorage.setItem(LMS_TOKEN_KEY, result.access_token);
    if (result.full_name) sessionStorage.setItem('lms_user_name', result.full_name);
    if (result.role) sessionStorage.setItem('lms_user_role', result.role);
    return result.access_token;
  }

  /** API call that transparently establishes/refreshes the session once. */
  async function api(path, options) {
    await ensureSession();
    try {
      return await request(path, options);
    } catch (error) {
      if (error.status === 401) {
        // Access tokens last 15 minutes; a stale one just needs replacing.
        sessionStorage.removeItem(LMS_TOKEN_KEY);
        await ensureSession();
        return await request(path, options);
      }
      throw error;
    }
  }

  /* ── Small DOM helpers ──────────────────────────────────────────── */

  function el(id) {
    return document.getElementById(id);
  }

  /** Always assign user data via textContent — never innerHTML. */
  function text(node, value) {
    if (node) node.textContent = value == null ? '' : String(value);
    return node;
  }

  function show(node, visible) {
    if (node) node.hidden = !visible;
  }

  function notice(node, message, kind) {
    if (!node) return;
    node.className = 'notice' + (kind ? ' ' + kind : '');
    node.textContent = message;
    node.hidden = !message;
  }

  let toastTimer = null;
  function toast(message) {
    let node = el('lms-toast');
    if (!node) {
      node = document.createElement('div');
      node.id = 'lms-toast';
      node.className = 'toast';
      document.body.appendChild(node);
    }
    node.textContent = message;
    node.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      node.classList.remove('show');
    }, 3200);
  }

  function formatDate(value) {
    if (!value) return '—';
    const parsed = new Date(value + 'T00:00:00');
    if (isNaN(parsed.getTime())) return value;
    return parsed.toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
  }

  function formatRange(start, end) {
    if (start === end) return formatDate(start);
    return formatDate(start) + ' → ' + formatDate(end);
  }

  function days(value) {
    const number = Number(value || 0);
    // "1 day" / "2.5 days" — trailing .00 reads like a machine.
    const rounded = Number.isInteger(number) ? number : number.toFixed(2).replace(/0$/, '');
    return rounded + (Math.abs(number) === 1 ? ' day' : ' days');
  }

  function todayISO() {
    const now = new Date();
    const offsetMs = now.getTimezoneOffset() * 60000;
    return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
  }

  /** Debounce, so the live estimate doesn't fire on every keystroke. */
  function debounce(fn, wait) {
    let timer = null;
    return function () {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(null, args);
      }, wait);
    };
  }

  function signOut() {
    sessionStorage.removeItem(LMS_TOKEN_KEY);
    sessionStorage.removeItem('lms_user_name');
    sessionStorage.removeItem('lms_user_role');
  }

  global.LMS = {
    API_BASE: API_BASE,
    ApiError: ApiError,
    api: api,
    ensureSession: ensureSession,
    request: request,
    signOut: signOut,
    el: el,
    text: text,
    show: show,
    notice: notice,
    toast: toast,
    formatDate: formatDate,
    formatRange: formatRange,
    days: days,
    todayISO: todayISO,
    debounce: debounce,
  };
})(window);
