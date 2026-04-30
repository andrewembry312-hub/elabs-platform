// ═══════════════════════════════════════════════════════════════════
// OPENCLAW CLIENT — WebSocket gateway client for OpenClaw visual integration
//
// Reads configuration from:
//   window.OPENCLAW_GATEWAY  — e.g. 'http://127.0.0.1:18789'
//   window.OPENCLAW_TOKEN    — WS auth token
// ═══════════════════════════════════════════════════════════════════

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_ATTEMPTS = 10;
const HEARTBEAT_INTERVAL_MS = 15000;
const RPC_TIMEOUT_MS = 12000;
const PROTOCOL_VERSION = 3;
const DEFAULT_CLIENT_ID = 'openclaw-control-ui';
const DEFAULT_CLIENT_MODE = 'ui';
const DEVICE_IDENTITY_STORAGE_KEY = 'oc_device_identity_v1';
const TOKEN_MISSING_BLOCK_STORAGE_KEY = 'oc_token_missing_block_v1';

const _subscribers = {};   // { eventType: [callback, ...] }
const _wildcard = [];      // callbacks subscribed to all events
const _sessionSubs = {};   // { sessionKey: [callback, ...] } — per-session chat stream
const _pendingCalls = {};  // { id: { resolve, reject, timer } } — in-flight RPC calls
let _callIdSeq = 1;

let _ws = null;
let _state = 'disconnected';  // 'disconnected' | 'connecting' | 'connected' | 'error'
let _reconnectAttempts = 0;
let _reconnectTimer = null;
let _heartbeatTimer = null;
let _explicitClose = false;
let _dotEl = null;          // cached #openclawDot element
let _connectNonce = null;
let _connectReqId = null;
let _connectHandshakeStarted = false;
let _deviceIdentityPromise = null;
let _missingTokenWarned = false;
let _tokenMissingBlockKey = '';

// ── Public API ────────────────────────────────────────────────────

/**
 * Subscribe to a specific OpenClaw event type, or '*' for all events.
 * @param {string} eventType  — e.g. 'task_running', 'resource_update', '*'
 * @param {Function} callback — called with the parsed event payload
 * @returns {Function} unsubscribe function
 */
export function subscribe(eventType, callback) {
  if (eventType === '*') {
    _wildcard.push(callback);
    return () => { const i = _wildcard.indexOf(callback); if (i >= 0) _wildcard.splice(i, 1); };
  }
  if (!_subscribers[eventType]) _subscribers[eventType] = [];
  _subscribers[eventType].push(callback);
  return () => {
    const arr = _subscribers[eventType];
    if (arr) { const i = arr.indexOf(callback); if (i >= 0) arr.splice(i, 1); }
  };
}

/**
 * Subscribe to chat events for a specific OpenClaw session key.
 * Callback receives every `chat` / `chat.side_result` event matching that session.
 */
export function subscribeSession(sessionKey, callback) {
  if (!_sessionSubs[sessionKey]) _sessionSubs[sessionKey] = [];
  _sessionSubs[sessionKey].push(callback);
  return () => {
    const arr = _sessionSubs[sessionKey];
    if (arr) { const i = arr.indexOf(callback); if (i >= 0) arr.splice(i, 1); }
  };
}

/** Remove all session subscribers for a session key. */
export function unsubscribeSession(sessionKey) {
  delete _sessionSubs[sessionKey];
}

/** Get current connection state string. */
export function getConnectionState() { return _state; }

/** Return gateway base URL from window config or default. */
export function getGatewayUrl() {
  return (window.OPENCLAW_GATEWAY || 'http://127.0.0.1:18789').replace(/\/$/, '');
}

/** Return auth token from window config. */
export function getToken() {
  return window.OPENCLAW_TOKEN || '';
}

/**
 * Initiate a connection to the OpenClaw gateway.
 * Safe to call multiple times — will no-op if already connected/connecting.
 */
export function connect() {
  if (_state === 'connected' || _state === 'connecting') return;

  const currentKey = _currentConnectionKey();
  const blockedKey = _getTokenMissingBlockKey();
  if (blockedKey && blockedKey === currentKey) {
    if (!_missingTokenWarned) {
      console.info('[openclaw-client] Gateway token missing; reconnect remains paused until token/gateway config changes.');
      _missingTokenWarned = true;
    }
    _setState('disconnected');
    return;
  }

  if (blockedKey && blockedKey !== currentKey) {
    _setTokenMissingBlockKey('');
  }

  _explicitClose = false;
  _missingTokenWarned = false;
  _openWebSocket();
}

/** Cleanly close the connection and stop reconnect attempts. */
export function disconnect() {
  _explicitClose = true;
  _clearTimers();
  if (_ws) {
    _ws.close(1000, 'explicit close');
    _ws = null;
  }
  _setState('disconnected');
}

/**
 * Send a JSON message to the gateway over the open WebSocket.
 * @param {object} payload
 */
export function sendMessage(payload) {
  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify(payload));
  }
}

/**
 * Call a gateway RPC method and return a Promise for the result.
 * @param {string} method — e.g. 'models.list', 'chat.send', 'health'
 * @param {object} params
 * @param {number} [timeoutMs]
 * @returns {Promise<any>}
 */
export function callMethod(method, params = {}, timeoutMs = RPC_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    if (_state !== 'connected' || !_ws || _ws.readyState !== WebSocket.OPEN) {
      reject(new Error(`[openclaw-client] callMethod: not connected (state=${_state})`));
      return;
    }
    const id = String(_callIdSeq++);
    const timer = setTimeout(() => {
      delete _pendingCalls[id];
      reject(new Error(`[openclaw-client] callMethod timeout: ${method} (${timeoutMs}ms)`));
    }, timeoutMs);
    _pendingCalls[id] = { resolve, reject, timer };
    _sendRequestFrame({ id, method, params });
  });
}

// ── Internal — WebSocket lifecycle ────────────────────────────────

function _openWebSocket() {
  const base = getGatewayUrl();
  const token = getToken();
  // Convert http(s) → ws(s)
  const wsBase = base.replace(/^http/, 'ws');
  const url = token
    ? `${wsBase}/ws?token=${encodeURIComponent(token)}`
    : `${wsBase}/ws`;

  _setState('connecting');
  console.log(`[openclaw-client] Connecting to ${url}`);

  try {
    _ws = new WebSocket(url);
  } catch (err) {
    console.warn('[openclaw-client] WebSocket construction failed:', err);
    _setState('error');
    _scheduleReconnect();
    return;
  }

  _ws.onopen = () => {
    console.log('[openclaw-client] Socket open, waiting for handshake challenge');
    _reconnectAttempts = 0;
    _setState('connecting');
  };

  _ws.onmessage = (evt) => {
    try {
      const payload = JSON.parse(evt.data);
      _dispatch(payload);
    } catch (err) {
      console.warn('[openclaw-client] Failed to parse message:', evt.data, err);
    }
  };

  _ws.onerror = (err) => {
    console.warn('[openclaw-client] WebSocket error:', err);
    _setState('error');
  };

  _ws.onclose = (evt) => {
    console.log(`[openclaw-client] Closed — code=${evt.code} reason=${evt.reason}`);
    _clearTimers();
    _connectReqId = null;
    _connectNonce = null;
    _connectHandshakeStarted = false;
    _ws = null;
    // Reject all pending RPC calls
    Object.keys(_pendingCalls).forEach(id => {
      const pending = _pendingCalls[id];
      clearTimeout(pending.timer);
      pending.reject(new Error('[openclaw-client] connection closed'));
      delete _pendingCalls[id];
    });
    if (!_explicitClose) {
      _setState('disconnected');
      _scheduleReconnect();
    } else {
      _setState('disconnected');
    }
  };
}

function _scheduleReconnect() {
  if (_explicitClose) return;
  if (_reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    console.warn('[openclaw-client] Max reconnect attempts reached — giving up');
    _setState('error');
    return;
  }
  _reconnectAttempts++;
  const delay = RECONNECT_DELAY_MS * Math.min(_reconnectAttempts, 4);
  console.log(`[openclaw-client] Reconnecting in ${delay}ms (attempt ${_reconnectAttempts})`);
  _reconnectTimer = setTimeout(_openWebSocket, delay);
}

function _startHeartbeat() {
  _clearTimers();
  _heartbeatTimer = setInterval(() => {
    callMethod('health', {}).catch(() => {});
  }, HEARTBEAT_INTERVAL_MS);
}

function _clearTimers() {
  if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
  if (_heartbeatTimer) { clearInterval(_heartbeatTimer); _heartbeatTimer = null; }
}

// ── Internal — dispatch + dot update ─────────────────────────────

function _dispatch(payload) {
  // Handle gateway challenge event before regular event routing.
  if (payload?.type === 'event' && payload?.event === 'connect.challenge') {
    const nonce = payload?.payload?.nonce;
    if (typeof nonce !== 'string' || !nonce.trim()) {
      console.warn('[openclaw-client] connect.challenge missing nonce, closing socket');
      _ws?.close(4008, 'connect challenge missing nonce');
      return;
    }
    _connectNonce = nonce.trim();
    _sendConnectHandshake();
    return;
  }

  // Resolve handshake response.
  if (payload?.type === 'res' && payload?.id === _connectReqId) {
    _connectReqId = null;
    if (payload?.ok === true) {
      console.log('[openclaw-client] Handshake complete');
      _setState('connected');
      _startHeartbeat();
      _dispatch({ type: '_client_connected', gateway: getGatewayUrl(), hello: payload?.payload || null });
    } else {
      const message = payload?.error?.message || 'connect handshake failed';
      const lower = String(message).toLowerCase();
      const missingToken = lower.includes('token') && lower.includes('missing');
      if (missingToken) {
        _setTokenMissingBlockKey(_currentConnectionKey());
        if (!_missingTokenWarned) {
          console.info('[openclaw-client] Gateway token missing; local executor remains available. Reconnect paused until token/gateway config changes.');
          _missingTokenWarned = true;
        }
        _explicitClose = true;
        _setState('disconnected');
        _ws?.close(4008, 'gateway token missing');
        return;
      }
      console.warn('[openclaw-client] Handshake failed:', message, payload?.error || '');
      _ws?.close(4008, String(message).slice(0, 120));
    }
    return;
  }

  // ── Resolve pending RPC calls (response shape: { type: 'res', id, ok, payload|error })
  if (payload?.type === 'res' && payload?.id && payload.id in _pendingCalls) {
    const pending = _pendingCalls[payload.id];
    clearTimeout(pending.timer);
    delete _pendingCalls[payload.id];
    if (payload.ok !== false && !payload.error) {
      pending.resolve(payload.payload ?? payload.result ?? payload);
    } else {
      pending.reject(new Error(payload.error?.message || payload.error || 'RPC call failed'));
    }
    return;
  }

  const normalized = _normalizeEventPayload(payload);
  const type = normalized?.type;
  if (!type) return;

  // ── Session-scoped chat event routing
  if (type === 'chat' || type === 'chat.side_result') {
    const sk = normalized?.sessionKey || normalized?.data?.sessionKey;
    if (sk && _sessionSubs[sk]) {
      for (const cb of _sessionSubs[sk]) {
        try { cb(normalized); } catch (e) { console.error('[openclaw-client] session subscriber error:', e); }
      }
    }
    if (_sessionSubs['*']) {
      for (const cb of _sessionSubs['*']) {
        try { cb(normalized); } catch (e) { console.error('[openclaw-client] session subscriber error:', e); }
      }
    }
  }

  // Notify wildcard subscribers
  for (const cb of _wildcard) {
    try { cb(normalized); } catch (e) { console.error('[openclaw-client] subscriber error:', e); }
  }

  // Notify type-specific subscribers
  const arr = _subscribers[type];
  if (arr) {
    for (const cb of arr) {
      try { cb(normalized); } catch (e) { console.error('[openclaw-client] subscriber error:', e); }
    }
  }
}

function _normalizeEventPayload(payload) {
  if (payload?.type === 'event' && typeof payload?.event === 'string') {
    const eventType = payload.event;
    const eventData = payload?.payload && typeof payload.payload === 'object' ? payload.payload : {};
    return {
      ...(eventData || {}),
      type: eventType,
      event: eventType,
      data: eventData,
      seq: payload?.seq,
      stateVersion: payload?.stateVersion,
    };
  }
  return payload;
}

function _sendRequestFrame({ id, method, params }) {
  if (!_ws || _ws.readyState !== WebSocket.OPEN) return;
  _ws.send(JSON.stringify({ type: 'req', id, method, params }));
}

function _currentConnectionKey() {
  return `${getGatewayUrl()}|${getToken()}`;
}

function _getTokenMissingBlockKey() {
  if (_tokenMissingBlockKey) return _tokenMissingBlockKey;
  try {
    _tokenMissingBlockKey = localStorage.getItem(TOKEN_MISSING_BLOCK_STORAGE_KEY) || '';
  } catch {
    _tokenMissingBlockKey = '';
  }
  return _tokenMissingBlockKey;
}

function _setTokenMissingBlockKey(key) {
  _tokenMissingBlockKey = key || '';
  try {
    if (_tokenMissingBlockKey) {
      localStorage.setItem(TOKEN_MISSING_BLOCK_STORAGE_KEY, _tokenMissingBlockKey);
    } else {
      localStorage.removeItem(TOKEN_MISSING_BLOCK_STORAGE_KEY);
    }
  } catch {
    // Ignore storage errors.
  }
}

function _sendConnectHandshake() {
  if (!_connectNonce || !_ws || _ws.readyState !== WebSocket.OPEN || _connectReqId || _connectHandshakeStarted) return;
  _connectHandshakeStarted = true;

  void _sendConnectHandshakeAsync();
}

async function _sendConnectHandshakeAsync() {
  const token = getToken();
  const platform = (navigator?.platform || 'web').toString().trim() || 'web';
  const version = (window.OPENCLAW_CLIENT_VERSION || 'webui-1.0.0').toString().trim() || 'webui-1.0.0';
  const scopes = ['operator.read', 'operator.write', 'operator.admin'];
  const nonce = _connectNonce;

  try {
    const id = String(_callIdSeq++);
    const params = {
      minProtocol: PROTOCOL_VERSION,
      maxProtocol: PROTOCOL_VERSION,
      client: {
        id: DEFAULT_CLIENT_ID,
        version,
        platform,
        mode: DEFAULT_CLIENT_MODE,
      },
      caps: [],
      role: 'operator',
      scopes,
      auth: token ? { token } : undefined,
      userAgent: typeof navigator?.userAgent === 'string' ? navigator.userAgent : undefined,
      locale: typeof navigator?.language === 'string' ? navigator.language : undefined,
    };

    const deviceIdentity = await _getOrCreateDeviceIdentity();
    const signedAt = Date.now();
    const payload = _buildDeviceAuthPayloadV3({
      deviceId: deviceIdentity.deviceId,
      clientId: DEFAULT_CLIENT_ID,
      clientMode: DEFAULT_CLIENT_MODE,
      role: 'operator',
      scopes,
      signedAtMs: signedAt,
      token,
      nonce,
      platform,
      deviceFamily: '',
    });
    const signature = await _signDevicePayload(deviceIdentity.privateKey, payload);

    params.device = {
      id: deviceIdentity.deviceId,
      publicKey: deviceIdentity.publicKeyRaw,
      signature,
      signedAt,
      nonce,
    };

    _connectReqId = id;
    _sendRequestFrame({ id, method: 'connect', params });
  } catch (err) {
    console.warn('[openclaw-client] device identity handshake setup failed, retrying without device identity:', err?.message || err);
    const id = String(_callIdSeq++);
    _connectReqId = id;
    _sendRequestFrame({
      id,
      method: 'connect',
      params: {
        minProtocol: PROTOCOL_VERSION,
        maxProtocol: PROTOCOL_VERSION,
        client: {
          id: DEFAULT_CLIENT_ID,
          version,
          platform,
          mode: DEFAULT_CLIENT_MODE,
        },
        caps: [],
        role: 'operator',
        auth: token ? { token } : undefined,
        userAgent: typeof navigator?.userAgent === 'string' ? navigator.userAgent : undefined,
        locale: typeof navigator?.language === 'string' ? navigator.language : undefined,
      },
    });
  }
}

async function _getOrCreateDeviceIdentity() {
  if (_deviceIdentityPromise) return _deviceIdentityPromise;

  _deviceIdentityPromise = (async () => {
    if (!globalThis.crypto?.subtle) {
      throw new Error('WebCrypto SubtleCrypto unavailable');
    }

    const cachedRaw = localStorage.getItem(DEVICE_IDENTITY_STORAGE_KEY);
    if (cachedRaw) {
      try {
        const cached = JSON.parse(cachedRaw);
        if (cached?.privateJwk && cached?.publicKeyRaw && cached?.deviceId) {
          const privateKey = await crypto.subtle.importKey(
            'jwk',
            cached.privateJwk,
            { name: 'Ed25519' },
            false,
            ['sign'],
          );
          return {
            deviceId: String(cached.deviceId),
            publicKeyRaw: String(cached.publicKeyRaw),
            privateKey,
          };
        }
      } catch (e) {
        console.warn('[openclaw-client] cached device identity invalid, regenerating:', e?.message || e);
      }
    }

    const keyPair = await crypto.subtle.generateKey(
      { name: 'Ed25519' },
      true,
      ['sign', 'verify'],
    );
    const publicRaw = new Uint8Array(await crypto.subtle.exportKey('raw', keyPair.publicKey));
    const publicJwk = await crypto.subtle.exportKey('jwk', keyPair.publicKey);
    const privateJwk = await crypto.subtle.exportKey('jwk', keyPair.privateKey);
    const publicKeyRaw = _base64UrlEncode(publicRaw);
    const deviceId = await _sha256Hex(publicRaw);

    localStorage.setItem(
      DEVICE_IDENTITY_STORAGE_KEY,
      JSON.stringify({ version: 1, deviceId, publicKeyRaw, publicJwk, privateJwk }),
    );

    return {
      deviceId,
      publicKeyRaw,
      privateKey: keyPair.privateKey,
    };
  })();

  return _deviceIdentityPromise;
}

async function _signDevicePayload(privateKey, payload) {
  const bytes = new TextEncoder().encode(payload);
  const signature = new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, privateKey, bytes));
  return _base64UrlEncode(signature);
}

function _buildDeviceAuthPayloadV3(params) {
  const scopes = Array.isArray(params.scopes) ? params.scopes.join(',') : '';
  const token = params.token || '';
  const platform = _normalizeDeviceMetadataForAuth(params.platform);
  const deviceFamily = _normalizeDeviceMetadataForAuth(params.deviceFamily);
  return [
    'v3',
    params.deviceId,
    params.clientId,
    params.clientMode,
    params.role,
    scopes,
    String(params.signedAtMs),
    token,
    params.nonce,
    platform,
    deviceFamily,
  ].join('|');
}

function _normalizeDeviceMetadataForAuth(value) {
  if (typeof value !== 'string') return '';
  const trimmed = value.trim();
  return trimmed ? trimmed.replace(/[A-Z]/g, (char) => String.fromCharCode(char.charCodeAt(0) + 32)) : '';
}

function _base64UrlEncode(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

async function _sha256Hex(bytes) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return Array.from(digest).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function _setState(newState) {
  _state = newState;
  _updateDot();
}

function _updateDot() {
  if (!_dotEl) _dotEl = document.getElementById('openclawDot');
  if (!_dotEl) return;

  const configs = {
    connected:    { color: '#4ade80', shadow: '#4ade80', label: 'OpenClaw ✓', border: 'rgba(74,222,128,0.35)' },
    connecting:   { color: '#fbbf24', shadow: '#fbbf24', label: 'OpenClaw…',  border: 'rgba(251,191,36,0.35)'  },
    disconnected: { color: '#6b7280', shadow: 'transparent', label: 'OpenClaw', border: 'rgba(107,114,128,0.25)' },
    error:        { color: '#ef4444', shadow: '#ef4444', label: 'OpenClaw ✗',  border: 'rgba(239,68,68,0.35)'    },
  };

  const cfg = configs[_state] || configs.disconnected;
  const dot  = _dotEl.querySelector('.oc-dot');
  const lbl  = _dotEl.querySelector('.oc-label');
  const wrap = _dotEl;

  if (dot)  { dot.style.background  = cfg.color;  dot.style.boxShadow = `0 0 6px ${cfg.shadow}`; }
  if (lbl)  { lbl.style.color = cfg.color; lbl.textContent = cfg.label; }
  if (wrap) { wrap.style.borderColor = cfg.border; }

  // Notify the DAG button context helper if available
  if (typeof window._ocUpdateDagButton === 'function') window._ocUpdateDagButton();
}

// ── Auto-connect when the DOM is ready ───────────────────────────

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', connect);
} else {
  // Small delay so other modules finish imports before first event dispatch
  setTimeout(connect, 50);
}

export default { connect, disconnect, subscribe, subscribeSession, unsubscribeSession, callMethod, getConnectionState, getGatewayUrl, getToken, sendMessage };
