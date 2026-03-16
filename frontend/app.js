/**
 * PitchMirror — Browser client
 *
 * Responsibilities:
 * - Capture webcam + microphone via getUserMedia
 * - Optionally capture screen-share frames via getDisplayMedia
 * - Encode audio as raw 16-bit PCM at 16kHz and send to backend via WebSocket
 * - Capture webcam JPEG frames at 1fps and optional screen JPEG frames at 0.5fps
 * - Play coach audio responses (24kHz PCM) via Web Audio API
 * - Update live metrics, transcript feed, and scorecard
 */

/* ── Config ──────────────────────────────────────── */
const API_TOKEN = window.PITCHMIRROR_API_TOKEN || window.localStorage.getItem('pitchmirror_api_token') || '';
const WS_BASE_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
const AUDIO_SAMPLE_RATE = 16000;
const AUDIO_CHUNK_MS = 100;        // Send 100ms chunks
const VIDEO_FPS = 1;               // 1 frame per second (Live API max)
const VIDEO_QUALITY = 0.7;         // JPEG quality
const VIDEO_WIDTH = 640;
const VIDEO_HEIGHT = 480;
const SCREEN_FPS = 0.5;            // 1 frame every 2s for lower cost
const SCREEN_QUALITY = 0.72;
const COACH_SAMPLE_RATE = 24000;   // Gemini outputs 24kHz PCM
const COACH_AUDIO_SCHEDULE_LEAD_S = 0.02;
const COACH_AUDIO_RESET_GAP_S = 0.35;
const COACH_AUDIO_MAX_BUFFER_AHEAD_S = 1.25;
const SUPPORTS_SCREEN_SHARE = !!navigator.mediaDevices?.getDisplayMedia;

/* ── Binary frame type IDs (must match backend/main.py) ── */
const T_AUDIO_IN  = 0x01;  // client→server PCM
const T_VIDEO_IN  = 0x02;  // client→server JPEG
const T_STOP      = 0x03;  // client→server stop
const T_SCREEN_IN = 0x04;  // client→server screen JPEG
const T_SLIDE_IN  = 0x05;  // client→server uploaded slide JPEG
const T_AUDIO_OUT = 0x10;  // server→client coach PCM
const CLIENT_USER_ID_KEY = 'pitchmirror_user_id';
const CLIENT_USER_ID = (() => {
  const existing = window.localStorage.getItem(CLIENT_USER_ID_KEY);
  if (existing && /^[A-Za-z0-9_.-]{3,64}$/.test(existing)) return existing;
  const generated = `user-${(window.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)).slice(0, 12)}`;
  window.localStorage.setItem(CLIENT_USER_ID_KEY, generated);
  return generated;
})();

/** Prepend a 1-byte type header to a payload ArrayBuffer. */
function makeFrame(typeId, payloadBuffer) {
  const frame = new Uint8Array(1 + payloadBuffer.byteLength);
  frame[0] = typeId;
  frame.set(new Uint8Array(payloadBuffer), 1);
  return frame.buffer;
}

function authHeaders() {
  const headers = { 'x-user-id': CLIENT_USER_ID };
  if (API_TOKEN) headers['Authorization'] = `Bearer ${API_TOKEN}`;
  return headers;
}

function buildWsUrl(sessionCfg = {}) {
  const url = new URL(WS_BASE_URL);
  if (API_TOKEN) url.searchParams.set('token', API_TOKEN);
  if (CLIENT_USER_ID) url.searchParams.set('user', CLIENT_USER_ID);
  if (sessionCfg.persona) url.searchParams.set('persona', sessionCfg.persona);
  if (sessionCfg.mode) url.searchParams.set('mode', sessionCfg.mode);
  if (sessionCfg.deliveryContext) url.searchParams.set('context', sessionCfg.deliveryContext);
  if (sessionCfg.primaryGoal) url.searchParams.set('goal', sessionCfg.primaryGoal);
  if (sessionCfg.screenEnabled) url.searchParams.set('screen', '1');
  if (sessionCfg.demoMode) url.searchParams.set('demo', '1');
  return url.toString();
}

/* ── State ───────────────────────────────────────── */
let ws = null;
let localStream = null;
let audioContext = null;
let audioWorklet = null;
let audioWorkletSink = null;
let videoCanvas = null;
let videoInterval = null;
let screenStream = null;
let screenCanvas = null;
let screenVideo = null;
let screenInterval = null;
let coachAudioContext = null;
let coachNextPlayAt = 0;
let sessionActive = false;
let metrics = { filler: 0, eye: 0, pace: 0, clarity: 0, visuals: 0 };
const practiceSlides = [
  {
    title: 'Slide 1: Problem',
    body: 'State the pain clearly in one sentence and who experiences it.',
  },
  {
    title: 'Slide 2: Why Now',
    body: 'Explain why this matters today with one concrete trigger.',
  },
  {
    title: 'Slide 3: Solution',
    body: 'Describe your approach in plain language and one key advantage.',
  },
  {
    title: 'Slide 4: Evidence',
    body: 'Show one metric, one result, and one proof point.',
  },
  {
    title: 'Slide 5: Ask',
    body: 'Close with a specific ask and the exact next step.',
  },
];
let currentSlideIndex = 0;
let uploadedSlides = [];
let uploadedDeckId = '';
let currentUploadedSlideIndex = 0;

/* Overlay Drawing */
let overlayCtx = null;

/* Timer state */
let timerInterval = null;
let sessionStartTime = null;

/* Active transcript line — accumulates streaming chunks from the same speaker */
let _activeLine = { speaker: null, el: null };
const COACH_DEDUP_WINDOW_MS = 12000;
let _lastCoachLine = { normalized: '', at: 0 };
// Gemini Live often sends a second input_transcription for the same utterance
// after the model responds (deferred final). Dedup within 10s prevents it showing twice.
const USER_DEDUP_WINDOW_MS = 10000;
let _lastUserLine = { normalized: '', at: 0 };

/* Session recovery — used to re-fetch scorecard if WS closes during analysis */
let _currentSessionId = null;
let _waitingForScorecard = false;

/* ── DOM refs ────────────────────────────────────── */
const video           = document.getElementById('local-video');
const overlayCanvas   = document.getElementById('overlay-canvas');
const btnStart        = document.getElementById('btn-start');
const btnStop         = document.getElementById('btn-stop');
const statusBadge     = document.getElementById('status-badge');
const liveMetrics     = document.getElementById('live-metrics');
const videoOverlay    = document.getElementById('video-overlay');
const recIndicator    = document.getElementById('recording-indicator');
const transcriptFeed  = document.getElementById('transcript-feed');
const transcriptPanel = document.getElementById('transcript-panel');
const scorecardPanel  = document.getElementById('scorecard-panel');
const scorecardContent = document.getElementById('scorecard-content');
const coachToast      = document.getElementById('coach-toast');
const toastText       = document.getElementById('toast-text');
const sessionTimerEl  = document.getElementById('session-timer');
const videoWrapper    = document.querySelector('.video-wrapper');
const coachPersonaSelect = document.getElementById('coach-persona');
const coachModeSelect = document.getElementById('coach-mode');
const deliveryContextSelect = document.getElementById('delivery-context');
const primaryGoalSelect = document.getElementById('primary-goal');
const deckIndicator = document.getElementById('deck-indicator');
const deckSlideTitle = document.getElementById('deck-slide-title');
const deckSlideBody = document.getElementById('deck-slide-body');
const deckSlideImage = document.getElementById('deck-slide-image');
const deckSlidePlaceholder = document.getElementById('deck-slide-placeholder');
const deckPrevBtn = document.getElementById('deck-prev');
const deckNextBtn = document.getElementById('deck-next');
const slideUploadInput = document.getElementById('slide-upload');
const slideUploadBtn = document.getElementById('slide-upload-btn');
const slideUploadStatus = document.getElementById('slide-upload-status');
const screenShareToggle = document.getElementById('screen-share-toggle');
const demoModeToggle = document.getElementById('demo-mode-toggle');

if (coachPersonaSelect) {
  const savedPersona = window.localStorage.getItem('pitchmirror_coach_persona');
  if (savedPersona) coachPersonaSelect.value = savedPersona;
  coachPersonaSelect.addEventListener('change', () => {
    window.localStorage.setItem('pitchmirror_coach_persona', coachPersonaSelect.value);
  });
}

if (coachModeSelect) {
  const savedMode = window.localStorage.getItem('pitchmirror_coach_mode');
  if (savedMode) coachModeSelect.value = savedMode;
  coachModeSelect.addEventListener('change', () => {
    window.localStorage.setItem('pitchmirror_coach_mode', coachModeSelect.value);
  });
}

if (deliveryContextSelect) {
  const savedContext = window.localStorage.getItem('pitchmirror_delivery_context');
  if (savedContext) deliveryContextSelect.value = savedContext;
  deliveryContextSelect.addEventListener('change', () => {
    window.localStorage.setItem('pitchmirror_delivery_context', deliveryContextSelect.value);
  });
}

if (primaryGoalSelect) {
  const savedGoal = window.localStorage.getItem('pitchmirror_primary_goal');
  if (savedGoal) primaryGoalSelect.value = savedGoal;
  primaryGoalSelect.addEventListener('change', () => {
    window.localStorage.setItem('pitchmirror_primary_goal', primaryGoalSelect.value);
  });
}

if (demoModeToggle) {
  const savedDemo = window.localStorage.getItem('pitchmirror_demo_mode');
  if (savedDemo === '1') demoModeToggle.checked = true;
  demoModeToggle.addEventListener('change', () => {
    window.localStorage.setItem('pitchmirror_demo_mode', demoModeToggle.checked ? '1' : '0');
  });
}

if (screenShareToggle) {
  if (!SUPPORTS_SCREEN_SHARE) {
    screenShareToggle.disabled = true;
    screenShareToggle.checked = false;
  } else {
    const savedScreen = window.localStorage.getItem('pitchmirror_screen_share');
    if (savedScreen === '1') screenShareToggle.checked = true;
    screenShareToggle.addEventListener('change', () => {
      window.localStorage.setItem('pitchmirror_screen_share', screenShareToggle.checked ? '1' : '0');
    });
  }
}

function base64ToArrayBuffer(base64) {
  const binary = window.atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

function updateSlideUploadStatus(message, isError = false) {
  if (!slideUploadStatus) return;
  slideUploadStatus.textContent = message;
  slideUploadStatus.style.color = isError ? 'var(--danger)' : 'var(--text-muted)';
}

function currentUploadedSlide() {
  if (!uploadedSlides.length) return null;
  return uploadedSlides[currentUploadedSlideIndex] || null;
}

/** Fetch a single slide by index and cache it. Returns null on failure. */
async function fetchSlide(deckId, idx) {
  if (uploadedSlides[idx]) return uploadedSlides[idx];
  try {
    const resp = await fetch(`/api/slides/${deckId}/${idx}`, { headers: authHeaders() });
    if (!resp.ok) return null;
    const slide = await resp.json();
    uploadedSlides[idx] = slide;
    return slide;
  } catch {
    return null;
  }
}

/**
 * Pre-fetch remaining slides in the background after upload.
 * Yields between each request so the event loop stays responsive.
 */
async function prefetchSlidesBackground(deckId, total) {
  for (let i = 1; i < total; i++) {
    if (uploadedSlides[i]) continue;
    const slide = await fetchSlide(deckId, i);
    // If the user navigated to this slide while we were fetching, refresh the view.
    if (slide && currentUploadedSlideIndex === i) {
      renderPracticeSlide();
      if (sessionActive) sendCurrentUploadedSlideFrame();
    }
    await new Promise(r => setTimeout(r, 30));  // yield to event loop
  }
}

/**
 * If the current slide isn't loaded yet, kick off an async fetch and
 * refresh the view once it arrives.  No-op when slide is already cached.
 */
function maybeLoadCurrentSlide() {
  const idx = currentUploadedSlideIndex;
  if (uploadedSlides[idx] || !uploadedDeckId) return;
  fetchSlide(uploadedDeckId, idx).then(slide => {
    if (slide && currentUploadedSlideIndex === idx) {
      renderPracticeSlide();
      if (sessionActive) sendCurrentUploadedSlideFrame();
    }
  });
}

function sendSlideMeta() {
  if (!sessionActive || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (!uploadedSlides.length) return;
  ws.send(JSON.stringify({
    type: 'slide_index',
    deck_id: uploadedDeckId || '',
    current_slide_index: currentUploadedSlideIndex,
    total_slides: uploadedSlides.length,
  }));
}

function sendCurrentUploadedSlideFrame() {
  if (!sessionActive || !ws || ws.readyState !== WebSocket.OPEN) return;
  const slide = currentUploadedSlide();
  if (!slide || !slide.data_base64) return;
  try {
    sendSlideMeta();
    const buf = base64ToArrayBuffer(slide.data_base64);
    ws.send(makeFrame(T_SLIDE_IN, buf));
  } catch (err) {
    console.warn('failed to send slide frame', err);
  }
}

function renderPracticeSlide() {
  if (uploadedSlides.length) {
    const total = uploadedSlides.length;
    const slide = currentUploadedSlide();
    if (deckIndicator) deckIndicator.textContent = `${currentUploadedSlideIndex + 1}/${total}`;
    if (deckSlideImage) {
      if (slide?.data_base64) {
        deckSlideImage.src = `data:${slide.mime_type || 'image/jpeg'};base64,${slide.data_base64}`;
        deckSlideImage.classList.remove('hidden');
      } else {
        deckSlideImage.src = '';
        deckSlideImage.classList.add('hidden');
      }
    }
    if (deckSlideTitle) deckSlideTitle.classList.add('hidden');
    if (deckSlideBody) deckSlideBody.classList.add('hidden');
    if (deckSlidePlaceholder) {
      deckSlidePlaceholder.textContent = slide?.data_base64 ? 'Uploaded slide deck active.' : 'Loading slide\u2026';
      deckSlidePlaceholder.classList.remove('hidden');
    }
    updateSlideUploadStatus(`Uploaded deck: ${total} slide${total === 1 ? '' : 's'}`);
    return;
  }

  const total = practiceSlides.length;
  const slide = practiceSlides[currentSlideIndex] || practiceSlides[0];
  if (deckIndicator) deckIndicator.textContent = `${currentSlideIndex + 1}/${total}`;
  if (deckSlideTitle) {
    deckSlideTitle.textContent = slide.title;
    deckSlideTitle.classList.remove('hidden');
  }
  if (deckSlideBody) {
    deckSlideBody.textContent = slide.body;
    deckSlideBody.classList.remove('hidden');
  }
  if (deckSlideImage) {
    deckSlideImage.src = '';
    deckSlideImage.classList.add('hidden');
  }
  if (deckSlidePlaceholder) {
    deckSlidePlaceholder.textContent = 'Upload a PDF to enable real slide-aware coaching.';
    deckSlidePlaceholder.classList.remove('hidden');
  }
}

async function uploadSlidesPdf(file) {
  const form = new FormData();
  form.append('file', file);
  updateSlideUploadStatus('Uploading slides...');
  if (slideUploadBtn) slideUploadBtn.disabled = true;
  try {
    const resp = await fetch('/api/slides/upload', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Upload failed (${resp.status})`);
    }
    const payload = await resp.json();
    uploadedDeckId = payload.deck_id || '';
    const total = Number(payload.total_slides) || 0;
    // Initialise with null placeholders; slides are fetched lazily/in background.
    uploadedSlides = new Array(total).fill(null);
    // Seed the first slide from the eager upload response (avoids an extra round-trip).
    if (Array.isArray(payload.slides) && payload.slides[0]) {
      uploadedSlides[0] = payload.slides[0];
    }
    currentUploadedSlideIndex = Math.max(0, Number(payload.current_slide_index || 0));
    renderPracticeSlide();
    addTranscriptLine('coach', `Slides uploaded (${total}). I can now critique slide design.`);
    if (sessionActive) sendCurrentUploadedSlideFrame();
    // Pre-fetch remaining slides in the background (non-blocking).
    if (total > 1) prefetchSlidesBackground(uploadedDeckId, total);
  } catch (err) {
    console.error('slide upload failed', err);
    updateSlideUploadStatus(err.message || 'Slide upload failed.', true);
  } finally {
    if (slideUploadBtn) slideUploadBtn.disabled = false;
  }
}

function navigatePracticeSlides(action) {
  const norm = String(action || '').toLowerCase();
  if (!['next', 'previous', 'first', 'last'].includes(norm)) return;

  if (uploadedSlides.length) {
    const total = uploadedSlides.length;
    if (norm === 'next') currentUploadedSlideIndex = Math.min(total - 1, currentUploadedSlideIndex + 1);
    else if (norm === 'previous') currentUploadedSlideIndex = Math.max(0, currentUploadedSlideIndex - 1);
    else if (norm === 'first') currentUploadedSlideIndex = 0;
    else if (norm === 'last') currentUploadedSlideIndex = total - 1;
    renderPracticeSlide();
    sendCurrentUploadedSlideFrame();
    maybeLoadCurrentSlide();
    return;
  }

  const total = practiceSlides.length;
  if (total === 0) return;
  if (norm === 'next') currentSlideIndex = Math.min(total - 1, currentSlideIndex + 1);
  else if (norm === 'previous') currentSlideIndex = Math.max(0, currentSlideIndex - 1);
  else if (norm === 'first') currentSlideIndex = 0;
  else if (norm === 'last') currentSlideIndex = total - 1;
  renderPracticeSlide();
}

if (slideUploadBtn) {
  slideUploadBtn.addEventListener('click', async () => {
    const file = slideUploadInput?.files?.[0];
    if (!file) {
      updateSlideUploadStatus('Select a PDF before uploading.', true);
      return;
    }
    await uploadSlidesPdf(file);
  });
}

if (slideUploadInput) {
  slideUploadInput.addEventListener('change', () => {
    if (slideUploadInput.files?.[0]) {
      updateSlideUploadStatus(`Selected: ${slideUploadInput.files[0].name}`);
    }
  });
}

if (deckPrevBtn) {
  deckPrevBtn.addEventListener('click', () => navigatePracticeSlides('previous'));
}
if (deckNextBtn) {
  deckNextBtn.addEventListener('click', () => navigatePracticeSlides('next'));
}
renderPracticeSlide();

/* ── Status helper ───────────────────────────────── */
function setStatus(state, label) {
  statusBadge.className = `status-badge ${state}`;
  statusBadge.textContent = label || state.charAt(0).toUpperCase() + state.slice(1);
}

/* ── Session timer ───────────────────────────────── */
function startTimer() {
  sessionStartTime = Date.now();
  if (sessionTimerEl) sessionTimerEl.classList.remove('hidden');
  timerInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - sessionStartTime) / 1000);
    const m = String(Math.floor(elapsed / 60)).padStart(2, '0');
    const s = String(elapsed % 60).padStart(2, '0');
    if (sessionTimerEl) sessionTimerEl.textContent = `${m}:${s}`;
  }, 1000);
}

function stopTimer() {
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
  if (sessionTimerEl) sessionTimerEl.classList.add('hidden');
}

/* ── Session start ───────────────────────────────── */
btnStart.addEventListener('click', async () => {
  try {
    await startSession();
  } catch (err) {
    console.error('Failed to start session:', err);
    setStatus('error', 'Camera/mic error');
    alert(`Could not access camera or microphone: ${err.message}`);
  }
});

btnStop.addEventListener('click', stopSession);

async function startSession() {
  const sessionCfg = {
    persona: coachPersonaSelect?.value || 'coach',
    mode: coachModeSelect?.value || 'general',
    deliveryContext: deliveryContextSelect?.value || 'virtual',
    primaryGoal: primaryGoalSelect?.value || 'balanced',
    screenEnabled: !!screenShareToggle?.checked,
    demoMode: !!demoModeToggle?.checked,
  };

  // 1. Get user media
  localStream = await navigator.mediaDevices.getUserMedia({
    video: { width: VIDEO_WIDTH, height: VIDEO_HEIGHT, facingMode: 'user' },
    audio: { sampleRate: AUDIO_SAMPLE_RATE, channelCount: 1, echoCancellation: true },
  });

  video.srcObject = localStream;
  await video.play();

  if (sessionCfg.screenEnabled) {
    await startScreenStream();
    sessionCfg.screenEnabled = !!screenStream;
  }

  // Activate broadcast corner brackets
  if (videoWrapper) videoWrapper.classList.add('recording');

  // 2. Setup video canvas for frame capture
  videoCanvas = document.createElement('canvas');
  videoCanvas.width = VIDEO_WIDTH;
  videoCanvas.height = VIDEO_HEIGHT;

  // 3. Connect WebSocket
  ws = new WebSocket(buildWsUrl(sessionCfg));
  ws.binaryType = 'arraybuffer';   // receive binary frames as ArrayBuffer (not Blob)

  // 4. Setup overlay canvas for real-time AI drawing
  if (overlayCanvas) {
    overlayCtx = overlayCanvas.getContext('2d');
    overlayCanvas.width = video.videoWidth || 640;
    overlayCanvas.height = video.videoHeight || 480;
    clearOverlay();
  }

  ws.onopen = () => {
    console.log('WebSocket connected');
    sessionActive = true;
    startAudioCapture();
    startVideoCapture();
    if (sessionCfg.screenEnabled) startScreenCapture();
    if (uploadedSlides.length) sendCurrentUploadedSlideFrame();
    startTimer();
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      // Binary frame from server — only coach audio (0x10) currently
      const view = new Uint8Array(event.data);
      if (view[0] === T_AUDIO_OUT) {
        playCoachAudio(event.data.slice(1));
      }
    } else {
      // JSON text frame — all control messages
      handleServerMessage(event);
    }
  };

  ws.onerror = (e) => {
    console.error('WebSocket error', e);
    setStatus('error', 'Connection lost');
  };

  ws.onclose = () => {
    if (sessionActive) {
      // Server closed mid-session (rate limit, error, timeout).
      stopSession();
      setStatus('error', 'Disconnected');
    } else if (_waitingForScorecard && _currentSessionId) {
      // WS closed during the analysis phase (network drop, Cloud Run restart, etc.)
      // The backend still completes analysis and saves to Firestore — poll for it.
      const recoverSessionId = _currentSessionId;
      _waitingForScorecard = false;
      setStatus('analyzing', 'Reconnecting to results\u2026');
      setTimeout(() => tryRecoverScorecard(recoverSessionId), 2500);
    }
  };

  // 4. UI updates
  btnStart.classList.add('hidden');
  btnStop.classList.remove('hidden');
  if (coachPersonaSelect) coachPersonaSelect.disabled = true;
  if (coachModeSelect) coachModeSelect.disabled = true;
  if (deliveryContextSelect) deliveryContextSelect.disabled = true;
  if (primaryGoalSelect) primaryGoalSelect.disabled = true;
  if (screenShareToggle) screenShareToggle.disabled = true;
  if (demoModeToggle) demoModeToggle.disabled = true;
  liveMetrics.classList.remove('hidden');
  videoOverlay.classList.remove('hidden');
  recIndicator.classList.remove('hidden');
  transcriptFeed.innerHTML = '';
  _activeLine = { speaker: null, el: null };
  _lastCoachLine = { normalized: '', at: 0 };
  _lastUserLine  = { normalized: '', at: 0 };
  _currentSessionId = null;
  _waitingForScorecard = false;
  window._analysisReport = null;
  window._researchTips   = null;
  window._generatedAssets = null;
  scorecardPanel.classList.add('hidden');
  document.querySelector('.right-panel')?.classList.remove('results-mode');
  document.getElementById('analysis-panel')?.classList.add('hidden');
  transcriptPanel.classList.remove('hidden');
  setStatus('connected', `Connecting (${sessionCfg.mode.replace('_', ' ')})...`);
}

/* ── Audio capture (PCM 16kHz) ───────────────────── */
async function startAudioCapture() {
  audioContext = new AudioContext({ sampleRate: AUDIO_SAMPLE_RATE });

  // Use AudioWorklet for low-latency PCM capture
  const workletCode = `
    class PCMProcessor extends AudioWorkletProcessor {
      constructor() {
        super();
        this._buffer = [];
        this._bufferSize = ${Math.floor(AUDIO_SAMPLE_RATE * AUDIO_CHUNK_MS / 1000)};
      }
      process(inputs) {
        const input = inputs[0][0];
        if (!input) return true;
        for (let i = 0; i < input.length; i++) {
          this._buffer.push(input[i]);
        }
        while (this._buffer.length >= this._bufferSize) {
          const chunk = this._buffer.splice(0, this._bufferSize);
          this.port.postMessage(chunk);
        }
        return true;
      }
    }
    registerProcessor('pcm-processor', PCMProcessor);
  `;

  const blob = new Blob([workletCode], { type: 'application/javascript' });
  const url = URL.createObjectURL(blob);
  try {
    await audioContext.audioWorklet.addModule(url);
  } finally {
    // Revoke unconditionally — success or failure — to avoid blob URL leak.
    URL.revokeObjectURL(url);
  }

  const source = audioContext.createMediaStreamSource(localStream);
  audioWorklet = new AudioWorkletNode(audioContext, 'pcm-processor');
  audioWorkletSink = audioContext.createGain();
  audioWorkletSink.gain.value = 0;

  audioWorklet.port.onmessage = (e) => {
    if (!sessionActive || !ws || ws.readyState !== WebSocket.OPEN) return;
    // Convert float32 → int16 PCM, send as binary frame — no JSON, no base64
    const pcm16 = float32ToInt16(e.data);
    ws.send(makeFrame(T_AUDIO_IN, pcm16.buffer));
  };

  source.connect(audioWorklet);
  // Keep the worklet in a live graph without echoing mic audio to speakers.
  audioWorklet.connect(audioWorkletSink);
  audioWorkletSink.connect(audioContext.destination);
}

function float32ToInt16(float32Array) {
  const int16 = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return int16;
}

/* ── Video capture (JPEG at 1fps) ────────────────── */
function startVideoCapture() {
  videoInterval = setInterval(() => {
    if (!sessionActive || !ws || ws.readyState !== WebSocket.OPEN) return;
    const ctx = videoCanvas.getContext('2d');
    ctx.save();
    ctx.scale(-1, 1);  // mirror to match the display
    ctx.drawImage(video, -VIDEO_WIDTH, 0, VIDEO_WIDTH, VIDEO_HEIGHT);
    ctx.restore();
    videoCanvas.toBlob((blob) => {
      if (!blob) return;
      blob.arrayBuffer().then((buf) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(makeFrame(T_VIDEO_IN, buf));
        }
      });
    }, 'image/jpeg', VIDEO_QUALITY);
  }, 1000 / VIDEO_FPS);
}

/* ── Screen-share capture (optional) ─────────────── */
async function startScreenStream() {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    console.warn('Screen capture API not available in this browser.');
    return;
  }

  try {
    screenStream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: 5 },
      audio: false,
    });
  } catch (err) {
    console.warn('Screen-share not granted:', err);
    if (screenShareToggle) screenShareToggle.checked = false;
    return;
  }

  screenVideo = document.createElement('video');
  screenVideo.srcObject = screenStream;
  screenVideo.muted = true;
  screenVideo.playsInline = true;
  await screenVideo.play();

  const track = screenStream.getVideoTracks()[0];
  if (track) {
    track.addEventListener('ended', () => {
      stopScreenCapture();
      if (screenShareToggle) screenShareToggle.checked = false;
      if (sessionActive) addTranscriptLine('coach', 'Screen share ended. Continuing webcam-only coaching.');
    });
  }

  screenCanvas = document.createElement('canvas');
}

function startScreenCapture() {
  if (!screenStream || !screenVideo || !screenCanvas) return;

  screenInterval = setInterval(() => {
    if (!sessionActive || !ws || ws.readyState !== WebSocket.OPEN) return;
    if (!screenVideo.videoWidth || !screenVideo.videoHeight) return;

    const maxW = 960;
    const aspect = screenVideo.videoWidth / screenVideo.videoHeight;
    const width = Math.min(maxW, screenVideo.videoWidth);
    const height = Math.round(width / aspect);
    screenCanvas.width = width;
    screenCanvas.height = height;

    const ctx = screenCanvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(screenVideo, 0, 0, width, height);

    screenCanvas.toBlob((blob) => {
      if (!blob) return;
      blob.arrayBuffer().then((buf) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(makeFrame(T_SCREEN_IN, buf));
        }
      });
    }, 'image/jpeg', SCREEN_QUALITY);
  }, 1000 / SCREEN_FPS);
}

function stopScreenCapture() {
  if (screenInterval) {
    clearInterval(screenInterval);
    screenInterval = null;
  }
  if (screenStream) {
    screenStream.getTracks().forEach((t) => t.stop());
    screenStream = null;
  }
  if (screenVideo) {
    screenVideo.srcObject = null;
    screenVideo = null;
  }
  screenCanvas = null;
}

/* ── Post-session scorecard recovery ─────────────── */
/**
 * Poll /api/sessions/{sessionId} until the scorecard is available.
 * Used when the WebSocket closes during analysis (network drop, server restart).
 * The backend always finishes analysis and saves to Firestore even if the WS is gone.
 */
async function tryRecoverScorecard(sessionId) {
  const MAX_ATTEMPTS = 8;
  const BASE_DELAY_MS = 3000;
  for (let i = 0; i < MAX_ATTEMPTS; i++) {
    await new Promise(r => setTimeout(r, BASE_DELAY_MS + i * 2000));
    try {
      const resp = await fetch(`/api/sessions/${sessionId}`, { headers: authHeaders() });
      if (resp.ok) {
        const data = await resp.json();
        // A completed scorecard has at least an overall_score
        if (data && (data.overall_score !== undefined || data.final_report)) {
          renderScorecard(data);
          loadRecentSessions();
          return;
        }
      }
    } catch { /* network still down — keep retrying */ }
  }
  // Gave up — surface an error so the user isn't left with a spinning panel
  document.getElementById('analysis-panel')?.classList.add('hidden');
  setStatus('error', 'Results unavailable — check session history');
}

/* ── Session stop ────────────────────────────────── */
function stopSession() {
  sessionActive = false;

  stopTimer();
  if (videoWrapper) videoWrapper.classList.remove('recording');

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(new Uint8Array([T_STOP]).buffer);
  }

  if (videoInterval) { clearInterval(videoInterval); videoInterval = null; }
  stopScreenCapture();
  if (audioWorklet)  { audioWorklet.disconnect(); audioWorklet = null; }
  if (audioWorkletSink) { audioWorkletSink.disconnect(); audioWorkletSink = null; }
  if (audioContext)  { audioContext.close(); audioContext = null; }
  if (coachAudioContext) {
    coachAudioContext.close().catch(() => {});
    coachAudioContext = null;
  }
  coachNextPlayAt = 0;
  if (localStream)   { localStream.getTracks().forEach(t => t.stop()); localStream = null; }

  video.srcObject = null;
  btnStop.classList.add('hidden');
  btnStart.classList.remove('hidden');
  if (coachPersonaSelect) coachPersonaSelect.disabled = false;
  if (coachModeSelect) coachModeSelect.disabled = false;
  if (deliveryContextSelect) deliveryContextSelect.disabled = false;
  if (primaryGoalSelect) primaryGoalSelect.disabled = false;
  if (screenShareToggle) screenShareToggle.disabled = !SUPPORTS_SCREEN_SHARE;
  if (demoModeToggle) demoModeToggle.disabled = false;
  recIndicator.classList.add('hidden');
  setStatus('idle', 'Session ended');
}

/* ── Coach audio playback (PCM 24kHz) ───────────── */
function playCoachAudio(pcmArrayBuffer) {
  if (!coachAudioContext) {
    coachAudioContext = new AudioContext({ sampleRate: COACH_SAMPLE_RATE });
    coachNextPlayAt = 0;
  } else if (coachAudioContext.state === 'suspended') {
    coachAudioContext.resume().catch(() => {});
  }

  // Raw 16-bit PCM little-endian → float32 — no base64 decode needed
  const view = new DataView(pcmArrayBuffer);
  const samples = new Float32Array(pcmArrayBuffer.byteLength / 2);
  for (let i = 0; i < samples.length; i++) {
    samples[i] = view.getInt16(i * 2, true) / 32768;  // little-endian
  }
  if (!samples.length) return;

  const buffer = coachAudioContext.createBuffer(1, samples.length, COACH_SAMPLE_RATE);
  buffer.getChannelData(0).set(samples);

  const source = coachAudioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(coachAudioContext.destination);

  // Queue audio chunks to avoid overlap/echo artifacts from immediate start().
  const now = coachAudioContext.currentTime;
  if (coachNextPlayAt < now - COACH_AUDIO_RESET_GAP_S) {
    coachNextPlayAt = now;
  }
  // If queue gets too far ahead (network jitter/backpressure), trim latency.
  if (coachNextPlayAt > now + COACH_AUDIO_MAX_BUFFER_AHEAD_S) {
    coachNextPlayAt = now + COACH_AUDIO_SCHEDULE_LEAD_S;
  }
  const startAt = Math.max(now + COACH_AUDIO_SCHEDULE_LEAD_S, coachNextPlayAt);
  source.start(startAt);
  coachNextPlayAt = startAt + buffer.duration;
  source.onended = () => {
    try { source.disconnect(); } catch (_) {}
  };
}

/* ── Server message handler ──────────────────────── */
function handleServerMessage(event) {
  const msg = JSON.parse(event.data);

  switch (msg.type) {

    case 'status':
      handleStatus(msg.state, msg.message);
      if (msg.state === 'connected' && msg.session_id) {
        _currentSessionId = msg.session_id;
      }
      if (msg.state === 'connected') {
        if (msg.coach_mode && coachModeSelect) {
          coachModeSelect.value = msg.coach_mode;
        }
        if (msg.delivery_context && deliveryContextSelect) {
          deliveryContextSelect.value = msg.delivery_context;
        }
        if (msg.primary_goal && primaryGoalSelect) {
          primaryGoalSelect.value = msg.primary_goal;
        }
        if (msg.screen_enabled === false && screenShareToggle?.checked) {
          addTranscriptLine('coach', 'Server disabled screen-aware mode for this deployment.');
        }
        if (Number(msg.total_slides) > 0 && uploadedSlides.length === 0) {
          updateSlideUploadStatus(`Server reports ${msg.total_slides} cached slides. Upload PDF in this tab to display them.`);
        }
      }
      break;

    case 'transcript':
      addTranscriptLine(msg.speaker, msg.text);
      break;

    case 'metric':
      updateMetric(msg.key, msg.value);
      break;

    case 'tool_call':
      // ADK tool invocation — show as a visible agent action in the transcript
      if (msg.tool === 'draw_overlay') {
        drawOverlayHighlight(msg.args.x, msg.args.y, msg.args.label);
      } else if (msg.tool === 'navigate_practice_slides') {
        // Uploaded decks are controlled by backend slide_change events to avoid
        // double-advancing and duplicate frame sends.
        if (!uploadedSlides.length) {
          navigatePracticeSlides(msg.args?.action || 'next');
        }
      }
      showToolCallEvent(msg.tool, msg.args);
      break;

    case 'slide_mark':
      showSlideMarkEvent(msg);
      break;

    case 'live_visual_hint':
      handleLiveVisualHint(msg);
      break;

    case 'slide_change':
      if (uploadedSlides.length) {
        const idx = Number(msg.current_slide_index);
        if (Number.isFinite(idx)) {
          currentUploadedSlideIndex = Math.min(
            Math.max(0, idx),
            Math.max(0, uploadedSlides.length - 1),
          );
          renderPracticeSlide();
          sendCurrentUploadedSlideFrame();
          maybeLoadCurrentSlide();
        }
      } else {
        navigatePracticeSlides(msg.action || 'next');
      }
      break;

    case 'pipeline_step': {
      // Real step completion event from backend — no fake timers needed
      const stepMap = {
        delivery_done:  'step-delivery',
        content_done:   'step-content',
        research_done:  'step-research',
        synthesis_done: 'step-synthesis',
      };
      const stepId = stepMap[msg.step];
      if (stepId) markPipelineStepDone(stepId);
      if (msg.step === 'visuals_start') {
        addTranscriptLine('coach', 'Generating visual improvement cards...');
      }
      break;
    }

    case 'analysis_complete': {
      // Post-session pipeline finished — store report + research for scorecard
      window._analysisReport = msg.report;
      window._researchTips   = msg.research_tips || '';
      window._generatedAssets = msg.generated_assets || [];
      // Ensure all steps are visually done (fallback for any missed pipeline_step events)
      ['step-delivery', 'step-content', 'step-research', 'step-synthesis'].forEach(markPipelineStepDone);
      setTimeout(() => document.getElementById('analysis-panel').classList.add('hidden'), 900);
      break;
    }

    case 'scorecard':
      _waitingForScorecard = false;
      renderScorecard(msg.data);
      loadRecentSessions();
      break;
  }
}

function handleStatus(state, message) {
  const labels = {
    connected:    'Connecting...',
    listening:    'Listening',
    coaching:     'Coach Speaking',
    reconnecting: 'Reconnecting...',
    analyzing:    'Analyzing Session...',
    error:        message || 'Error',
  };
  setStatus(state, labels[state] || state);

  if (state === 'analyzing') {
    _waitingForScorecard = true;
    liveMetrics.classList.add('hidden');
    recIndicator.classList.add('hidden');
    document.getElementById('transcript-panel').classList.add('hidden');
    document.getElementById('analysis-panel').classList.remove('hidden');

    // Reset all steps to spinning — real completion events drive the done state
    ['step-delivery', 'step-content', 'step-research', 'step-synthesis'].forEach(id => {
      const step = document.getElementById(id);
      if (!step) return;
      step.classList.remove('done');
      const dot = step.querySelector('.step-dot');
      if (dot) { dot.classList.remove('done'); dot.classList.add('spinning'); }
    });
  }
}

/* ── Transcript ──────────────────────────────────── */
function normalizeTranscriptForDedup(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function sanitizeTranscriptText(value) {
  return String(value || '')
    .replace(/<ctrl\d+>/gi, ' ')
    .replace(/<(spoken_[^>]*|noise|inaudible|unk|unknown)>/gi, ' ')
    .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function isRecentCoachDuplicate(text) {
  const normalized = normalizeTranscriptForDedup(text);
  if (!normalized) return true;
  if (!_lastCoachLine.normalized) return false;
  const elapsed = Date.now() - _lastCoachLine.at;
  if (elapsed > COACH_DEDUP_WINDOW_MS) return false;
  const last = _lastCoachLine.normalized;
  // Exact match: same full text re-delivered (e.g. output_transcription re-sent).
  // Prefix match: first streaming chunk of a duplicate turn won't match the full
  // stored line exactly — check if the incoming text is the start of what we
  // already showed (guards against "Monitor your pace" not matching the full
  // remembered "monitor your pace you re speaking quite quickly").
  return last === normalized || last.startsWith(normalized);
}

function rememberCoachLine(text) {
  const normalized = normalizeTranscriptForDedup(text);
  if (!normalized) return;
  _lastCoachLine = { normalized, at: Date.now() };
}

function isRecentUserDuplicate(text) {
  const normalized = normalizeTranscriptForDedup(text);
  if (!normalized) return true;
  if (!_lastUserLine.normalized) return false;
  const elapsed = Date.now() - _lastUserLine.at;
  return elapsed <= USER_DEDUP_WINDOW_MS && normalized === _lastUserLine.normalized;
}

function rememberUserLine(text) {
  const normalized = normalizeTranscriptForDedup(text);
  if (!normalized) return;
  _lastUserLine = { normalized, at: Date.now() };
}

function coachChunkAlreadyRendered(existingText, incomingText) {
  const existingNorm = normalizeTranscriptForDedup(existingText);
  const incomingNorm = normalizeTranscriptForDedup(incomingText);
  if (!incomingNorm) return true;
  if (!existingNorm) return false;
  if (existingNorm === incomingNorm) return true;
  if (incomingNorm.length >= 12 && existingNorm.includes(incomingNorm)) return true;
  return false;
}

function addTranscriptLine(speaker, text) {
  const normalizedSpeaker = String(speaker || '').toLowerCase();
  const cleanText = sanitizeTranscriptText(text);
  if (!cleanText) return;

  // Drop repeated coach lines (streaming retries).
  if (normalizedSpeaker === 'coach' && isRecentCoachDuplicate(cleanText)) {
    return;
  }
  // Drop repeated user lines — Gemini Live often re-sends the same input_transcription
  // as a deferred final after the model responds (after tool calls reset _activeLine).
  if (normalizedSpeaker === 'user' && isRecentUserDuplicate(cleanText)) {
    return;
  }

  const welcomePanel = transcriptFeed.querySelector('#welcome-panel');
  if (welcomePanel) welcomePanel.remove();

  // Accumulate consecutive chunks from the same speaker into one line.
  // The Live API streams transcription incrementally, so "Look at the camera
  // when presenting." arrives as several separate events — merge them.
  if (_activeLine.speaker === normalizedSpeaker && _activeLine.el) {
    const textEl = _activeLine.el.querySelector('.transcript-text');
    if (textEl) {
      if (normalizedSpeaker === 'coach' && coachChunkAlreadyRendered(textEl.textContent, cleanText)) {
        return;
      }
      // Add a space unless the existing text ends with one or the new chunk starts with one
      const needsSpace = textEl.textContent.length > 0
        && !textEl.textContent.endsWith(' ')
        && !cleanText.startsWith(' ');
      textEl.textContent += (needsSpace ? ' ' : '') + cleanText;
      transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
      if (normalizedSpeaker === 'coach') {
        rememberCoachLine(textEl.textContent);
        showCoachToast(textEl.textContent);
      } else if (normalizedSpeaker === 'user') {
        rememberUserLine(textEl.textContent);
      }
      return;
    }
  }

  // Different speaker (or first line) — start a new transcript entry
  const line = document.createElement('div');
  line.className = `transcript-line ${normalizedSpeaker}`;
  line.innerHTML = `
    <span class="transcript-speaker">${normalizedSpeaker === 'coach' ? 'Coach' : 'You'}</span>
    <span class="transcript-text">${escapeHtml(cleanText)}</span>
  `;
  transcriptFeed.appendChild(line);
  transcriptFeed.scrollTop = transcriptFeed.scrollHeight;

  _activeLine = { speaker: normalizedSpeaker, el: line };

  if (normalizedSpeaker === 'coach') {
    rememberCoachLine(cleanText);
    showCoachToast(cleanText);
  } else if (normalizedSpeaker === 'user') {
    rememberUserLine(cleanText);
  }
}

/* ── ADK tool call visibility ────────────────────── */
function showToolCallEvent(tool, args) {
  const placeholder = transcriptFeed.querySelector('#welcome-panel');
  if (placeholder) placeholder.remove();

  // A tool call happens between turns — reset active line accumulator
  _activeLine = { speaker: null, el: null };

  const line = document.createElement('div');
  line.className = 'transcript-line tool-call';

  if (tool === 'flag_issue') {
    const typeLabels = {
      filler:        'Filler words',
      pace:          'Pace',
      eye_contact:   'Eye contact',
      contradiction: 'Contradiction',
      clarity:       'Clarity',
      slide_clarity: 'Slide clarity',
      slide_mismatch:'Slide mismatch',
    };
    const label = typeLabels[args?.issue_type || ''] || args?.issue_type || '';
    // Build a human-readable evidence chip so judges can see exactly what threshold triggered
    const ev = args?.evidence || {};
    let evidenceHtml = '';
    if (ev.metric && ev.threshold != null) {
      const measured = ev.session_total != null ? ` · total: ${ev.session_total}`
                     : ev.threshold_s    != null ? ` · threshold: ${ev.threshold_s}s`
                     : '';
      evidenceHtml = `<span class="tool-evidence">${escapeHtml(ev.metric)} ${escapeHtml(String(ev.threshold))}${escapeHtml(measured)}</span>`;
    }
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">flag_issue</span>
        <span class="tool-arg">${escapeHtml(label)}</span>
        ${evidenceHtml}
        <span class="tool-desc">${escapeHtml(args?.description || '')}</span>
      </span>`;

  } else if (tool === 'get_speech_metrics') {
    const wpm   = args?.wpm_20s   != null ? `${args.wpm_20s} WPM` : '';
    const fills = args?.filler_count_30s != null ? `${args.filler_count_30s} fillers/30s` : '';
    const summary = [wpm, fills].filter(Boolean).join(' · ') || 'metrics';
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">get_speech_metrics</span>
        <span class="tool-arg metrics-result">${escapeHtml(summary)}</span>
      </span>`;

  } else if (tool === 'get_recent_transcript') {
    const n = args?.returned_words ?? args?.n_words ?? '';
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">get_recent_transcript</span>
        <span class="tool-arg">${n ? `last ${n} words` : 'transcript'}</span>
      </span>`;

  } else if (tool === 'check_eye_contact') {
    const confirmed = args?.confirmed;
    const secs = args?.seconds_away != null ? `${args.seconds_away}s away` : '';
    const status = confirmed ? '✓ confirmed' : 'tracking…';
    const cls = confirmed ? 'metrics-result' : '';
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">check_eye_contact</span>
        <span class="tool-arg ${cls}">${escapeHtml(secs ? `${secs} · ${status}` : status)}</span>
      </span>`;

  } else if (tool === 'check_slide_clarity') {
    const signalMap = {
      clutter: 'clutter',
      unreadable_text: 'unreadable text',
      weak_hierarchy: 'weak hierarchy',
      speech_mismatch: 'speech mismatch',
    };
    const signal = signalMap[args?.signal] || args?.signal || 'slide issue';
    const detail = args?.suggested_callout || args?.reason || '';
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">check_slide_clarity</span>
        <span class="tool-arg">${escapeHtml(signal)}</span>
        <span class="tool-desc">${escapeHtml(detail)}</span>
      </span>`;

  } else if (tool === 'navigate_practice_slides') {
    const action = args?.action || 'next';
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">navigate_practice_slides</span>
        <span class="tool-arg">${escapeHtml(action)}</span>
      </span>`;

  } else if (tool === 'jump_to_slide') {
    const idx = args?.index ?? '?';
    const total = args?.total_slides ?? '?';
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">jump_to_slide</span>
        <span class="tool-arg">slide ${Number(idx) + 1} of ${total}</span>
      </span>`;

  } else if (tool === 'mark_slide_issue') {
    const issueLabels = {
      clutter: 'Clutter', font_too_small: 'Font too small',
      low_contrast: 'Low contrast', off_topic: 'Off-topic', missing_data: 'Missing data',
    };
    const issueLabel = issueLabels[args?.issue_type] || args?.issue_type || 'Issue';
    const lbl = args?.label ? ` — ${args.label}` : '';
    const slideNum = args?.slide_index != null ? ` (slide ${Number(args.slide_index) + 1})` : '';
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">mark_slide_issue</span>
        <span class="tool-arg">${escapeHtml(issueLabel + lbl + slideNum)}</span>
      </span>`;

  } else if (tool === 'generate_live_visual_hint') {
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">generate_live_visual_hint</span>
        <span class="tool-arg">${escapeHtml(args?.hint_type || 'visual')}</span>
        <span class="tool-desc">Generating ${escapeHtml(args?.title || 'visual')}…</span>
      </span>`;

  } else {
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">${escapeHtml(tool)}</span>
      </span>`;
  }

  transcriptFeed.appendChild(line);
  transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
}

/* ── Slide issue annotation ──────────────────────── */
function showSlideMarkEvent(msg) {
  const issueLabels = {
    clutter: 'Clutter', font_too_small: 'Font too small',
    low_contrast: 'Low contrast', off_topic: 'Off-topic', missing_data: 'Missing data',
  };
  const label = issueLabels[msg.issue_type] || msg.issue_type || 'Slide issue';
  const extra = msg.label ? ` — ${msg.label}` : '';
  const slideNum = msg.slide_index != null ? ` (slide ${Number(msg.slide_index) + 1})` : '';
  const line = document.createElement('div');
  line.className = 'transcript-line tool-call slide-mark-event';
  line.innerHTML = `
    <span class="transcript-speaker tool-speaker">ADK</span>
    <span class="transcript-text tool-text">
      <span class="tool-name">slide annotation</span>
      <span class="tool-arg">${escapeHtml(label + extra + slideNum)}</span>
    </span>`;
  const placeholder = transcriptFeed.querySelector('#welcome-panel');
  if (placeholder) placeholder.remove();
  transcriptFeed.appendChild(line);
  transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
}

/* ── Live visual hint (Imagen in-session) ─────────── */
// Map from hint_type → pending DOM element (so we can replace spinner with image)
const _liveVisualPending = {};

function handleLiveVisualHint(msg) {
  const placeholder = transcriptFeed.querySelector('#welcome-panel');
  if (placeholder) placeholder.remove();

  if (msg.status === 'generating') {
    const line = document.createElement('div');
    line.className = 'transcript-line tool-call live-visual-line';
    line.id = `lv-pending-${msg.hint_type}`;
    line.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text">
        <span class="tool-name">live visual</span>
        <span class="tool-arg">${escapeHtml(msg.title || msg.hint_type)}</span>
        <span class="tool-desc lv-spinner">Generating image…</span>
      </span>`;
    _liveVisualPending[msg.hint_type] = line;
    transcriptFeed.appendChild(line);
    transcriptFeed.scrollTop = transcriptFeed.scrollHeight;

  } else if (msg.status === 'ready' && msg.data_base64) {
    // Replace spinner line with the actual image
    const pending = _liveVisualPending[msg.hint_type];
    const container = pending || document.createElement('div');
    container.className = 'transcript-line live-visual-line';
    container.removeAttribute('id');

    const sourceLabel = msg.source === 'fallback' ? ' (fallback)' : '';
    container.innerHTML = `
      <span class="transcript-speaker tool-speaker">ADK</span>
      <span class="transcript-text tool-text lv-result">
        <span class="tool-name">live visual</span>
        <span class="tool-arg">${escapeHtml(msg.title || msg.hint_type)}${escapeHtml(sourceLabel)}</span>
        <div class="lv-image-wrap">
          <img class="lv-image" src="data:${msg.mime_type || 'image/jpeg'};base64,${msg.data_base64}"
               alt="${escapeHtml(msg.title || 'Live visual hint')}"
               onclick="openLiveVisualModal(this.src, ${JSON.stringify(msg.title || '')})" />
        </div>
      </span>`;
    if (!pending) transcriptFeed.appendChild(container);
    transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
    delete _liveVisualPending[msg.hint_type];
  }
}

function openLiveVisualModal(src, title) {
  const modal = document.getElementById('live-visual-modal');
  if (!modal) return;
  document.getElementById('lv-modal-img').src = src;
  document.getElementById('lv-modal-title').textContent = title || 'Live Visual';
  modal.classList.remove('hidden');
}

function closeLiveVisualModal() {
  const modal = document.getElementById('live-visual-modal');
  if (modal) modal.classList.add('hidden');
}

/* ── Coach toast ─────────────────────────────────── */
let toastTimeout = null;
function showCoachToast(text) {
  toastText.textContent = text;
  coachToast.classList.remove('hidden');
  requestAnimationFrame(() => {
    coachToast.classList.add('visible');
  });

  if (toastTimeout) clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => {
    coachToast.classList.remove('visible');
    setTimeout(() => coachToast.classList.add('hidden'), 350);
  }, 5000);
}

/* ── Live metrics ────────────────────────────────── */
function updateMetric(key, value) {
  const metricMap = {
    filler: 'filler',
    eye_contact: 'eye',
    pace: 'pace',
    clarity: 'clarity',
    contradiction: 'clarity',
    slide_clarity: 'visuals',
    slide_mismatch: 'visuals',
  };
  const id = metricMap[key];
  if (!id) return;

  metrics[id] = value;
  const el = document.getElementById(`val-${id}`);
  const chip = document.getElementById(`chip-${id}`);

  if (el) {
    el.textContent = value;
    // Flash animation on change
    el.classList.remove('flash');
    void el.offsetWidth; // force reflow to restart animation
    el.classList.add('flash');
    el.addEventListener('animationend', () => el.classList.remove('flash'), { once: true });
  }
  if (chip) chip.classList.toggle('alert', value >= 3);
}

/* ── Overlay Drawing Logic ──────────────────────── */
function clearOverlay() {
  if (!overlayCtx || !overlayCanvas) return;
  overlayCtx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
}

let activeOverlayTimeout = null;
function drawOverlayHighlight(x, y, label) {
  if (!overlayCtx || !overlayCanvas) return;

  // x, y are 0..1 normalized. Map to canvas pixels.
  const px = x * overlayCanvas.width;
  const py = y * overlayCanvas.height;

  clearOverlay();
  if (activeOverlayTimeout) clearTimeout(activeOverlayTimeout);

  // Draw glowing ring
  overlayCtx.beginPath();
  overlayCtx.arc(px, py, 40, 0, 2 * Math.PI);
  overlayCtx.strokeStyle = '#38d9a9';
  overlayCtx.lineWidth = 4;
  overlayCtx.shadowBlur = 15;
  overlayCtx.shadowColor = '#38d9a9';
  overlayCtx.stroke();

  // Draw smaller inner ring
  overlayCtx.beginPath();
  overlayCtx.arc(px, py, 10, 0, 2 * Math.PI);
  overlayCtx.fillStyle = '#38d9a9';
  overlayCtx.fill();

  if (label) {
    overlayCtx.font = 'bold 16px Manrope, sans-serif';
    overlayCtx.fillStyle = '#ffffff';
    overlayCtx.shadowBlur = 4;
    overlayCtx.shadowColor = 'rgba(0,0,0,0.5)';
    overlayCtx.fillText(label, px + 50, py + 5);
  }

  // Clear after 4 seconds
  activeOverlayTimeout = setTimeout(clearOverlay, 4000);
}

/* ── Star rating helpers ─────────────────────────── */

/** Convert a 0-100 score to a 0-5 rating rounded to the nearest 0.5. */
function scoreToStars(score) {
  return Math.round(score / 10) / 2;
}

/** Render 5 star spans with full / half / empty CSS classes. */
function renderStarRow(stars) {
  let html = '';
  for (let i = 1; i <= 5; i++) {
    if (i <= Math.floor(stars)) {
      html += '<span class="star full">★</span>';
    } else if (i === Math.ceil(stars) && stars % 1 >= 0.5) {
      html += '<span class="star half">★</span>';
    } else {
      html += '<span class="star empty">★</span>';
    }
  }
  return html;
}

/* ── Scorecard ───────────────────────────────────── */
function renderScorecard(data) {
  const score = data.overall_score || 0;
  const gradeClass = score >= 80 ? 'excellent' : score >= 65 ? 'good' : score >= 45 ? 'needs-work' : 'poor';
  const gradeLabel = score >= 80 ? 'Excellent' : score >= 65 ? 'Good' : score >= 45 ? 'Needs Work' : 'Poor';
  const modeLabel = (data.coach_mode || 'general').replace('_', ' ');
  const contextLabel = (data.delivery_context || 'virtual').replace('_', ' ');
  const goalLabel = (data.primary_goal || 'balanced').replace('_', ' ');

  const overallStars = scoreToStars(score);

  const categories = data.categories || {};
  const events = data.coaching_events || [];

  // SVG score ring (arc reflects internal 0-100 percentage)
  const r = 52, cx = 60, cy = 60;
  const circ = +(2 * Math.PI * r).toFixed(2);
  const dashTarget = +(circ * (1 - score / 100)).toFixed(2);
  const ringColorMap = {
    excellent:    '#38d9a9',
    good:         '#4dd4c4',
    'needs-work': '#ffd04d',
    poor:         '#ff4757',
  };
  const ringColor = ringColorMap[gradeClass] || '#f0a035';

  const catHtml = Object.entries(categories).map(([key, cat]) => {
    const catName = {
      filler_words: 'Filler Words',
      eye_contact: 'Eye Contact',
      pace: 'Pace',
      clarity: 'Clarity & Logic',
      visual_delivery: 'Slide Clarity',
    }[key] || key;
    const catStars = scoreToStars(cat.score);
    const detail = cat.count !== undefined ? `${cat.count} detected` :
                   cat.drops !== undefined ? `${cat.drops} drops` :
                   cat.violations !== undefined ? `${cat.violations} flags` :
                   cat.slide_clarity_flags !== undefined ? `${cat.slide_clarity_flags + (cat.slide_mismatch_flags || 0)} visual issues` :
                   `${(cat.contradictions || 0) + (cat.flags || 0)} issues`;
    return `
      <div class="scorecard-category">
        <div class="sc-cat-name">${escapeHtml(catName)}</div>
        <div class="sc-cat-stars">
          <span class="sc-cat-star-row">${renderStarRow(catStars)}</span>
          <span class="sc-cat-rating">${catStars.toFixed(1)}/5</span>
        </div>
        <div class="sc-cat-detail">${escapeHtml(cat.label)} &middot; ${detail}</div>
      </div>
    `;
  }).join('');

  const eventsHtml = events.length === 0
    ? '<div style="color:var(--text-muted); font-size:12px; padding: 8px 0;">No coaching interruptions — great session!</div>'
    : events.map(e => {
        const ev = e.evidence || {};
        let evidenceChip = '';
        if (ev.metric && ev.threshold != null) {
          const parts = [ev.metric, ev.threshold];
          if (ev.session_total != null) parts.push(`session total: ${ev.session_total}`);
          if (ev.threshold_s    != null) parts.push(`≥${ev.threshold_s}s`);
          evidenceChip = `<span class="sc-event-evidence">${parts.map(escapeHtml).join(' · ')}</span>`;
        }
        return `
        <div class="sc-event">
          <span class="sc-event-time">${formatTime(e.timestamp)}</span>
          <span class="sc-event-text">${escapeHtml(e.text)}</span>
          ${evidenceChip}
        </div>`;
      }).join('');

  const durationMin = Math.floor((data.duration_seconds || 0) / 60);
  const durationSec = Math.round((data.duration_seconds || 0) % 60);

  // Final report from post-session multi-agent pipeline
  const report = data.final_report || window._analysisReport || '';
  const generatedAssets = data.generated_assets || window._generatedAssets || [];
  const citationHtml = renderCitationCards(data.research_tips || window._researchTips || '');
  const assetsHtml = renderGeneratedAssets(generatedAssets);
  // Always render the "AI Coaching Report" section; fall back to a placeholder
  // when the report is empty (e.g. very short session, analysis timeout, WS error).
  const reportBody = report
    ? escapeHtml(report).replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    : '<span style="color:var(--text-muted);font-style:italic">AI analysis is pending or unavailable for this session.</span>';
  const reportHtml = `<div class="scorecard-report">
      <div class="sc-events-title">AI Coaching Report <span class="report-badge">${escapeHtml(modeLabel)} mode</span> <span class="report-badge">gemini · multi-agent</span></div>
      <div class="report-body">${reportBody}</div>
    </div>
    ${assetsHtml}
    ${citationHtml}`;

  scorecardContent.innerHTML = `
    <div class="scorecard-overall">
      <div class="score-ring-wrap">
        <svg class="score-svg" viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle class="ring-track" cx="${cx}" cy="${cy}" r="${r}" />
          <circle class="ring-fill" cx="${cx}" cy="${cy}" r="${r}"
            stroke="${ringColor}"
            stroke-dasharray="${circ}"
            stroke-dashoffset="${circ}"
            data-target="${dashTarget}"
            transform="rotate(-90 ${cx} ${cy})" />
        </svg>
        <div class="score-number ${gradeClass}">${overallStars.toFixed(1)}</div>
      </div>
      <div class="overall-stars">${renderStarRow(overallStars)}</div>
      <div class="scorecard-grade-label">${gradeLabel} &middot; ${overallStars.toFixed(1)}&thinsp;/&thinsp;5 &middot; ${durationMin}:${String(durationSec).padStart(2,'0')} session &middot; ${escapeHtml(modeLabel)} &middot; ${escapeHtml(contextLabel)} &middot; goal: ${escapeHtml(goalLabel)}</div>
    </div>
    <div class="scorecard-categories">${catHtml}</div>
    <div class="scorecard-events">
      <div class="sc-events-title">Agent Coaching Events (${events.length})</div>
      ${eventsHtml}
    </div>
    ${reportHtml}
    <div class="recent-section">
      <div class="sc-events-title">Recent Sessions</div>
      <div id="recent-sessions" class="recent-sessions">
        <div class="recent-empty">Loading recent sessions...</div>
      </div>
    </div>
  `;

  scorecardPanel.classList.remove('hidden');
  liveMetrics.classList.add('hidden');
  videoOverlay.classList.add('hidden');

  // Enable scrolling on right panel for tall scorecard and scroll to top
  const rightPanel = document.querySelector('.right-panel');
  if (rightPanel) {
    rightPanel.classList.add('results-mode');
    rightPanel.scrollTop = 0;
  }

  // Animate score ring arc
  requestAnimationFrame(() => {
    const arc = scorecardContent.querySelector('.ring-fill');
    if (arc) {
      const t = arc.dataset.target;
      requestAnimationFrame(() => { arc.style.strokeDashoffset = t; });
    }
  });
}

async function loadRecentSessions() {
  const container = document.getElementById('recent-sessions');
  if (!container) return;

  try {
    const resp = await fetch('/api/sessions?limit=5', {
      headers: authHeaders(),
    });
    if (!resp.ok) {
      container.innerHTML = '<div class="recent-empty">Recent session history unavailable.</div>';
      return;
    }

    const payload = await resp.json();
    const sessions = (payload.sessions || []).filter(Boolean);
    if (!sessions.length) {
      container.innerHTML = '<div class="recent-empty">No previous sessions found.</div>';
      return;
    }

    container.innerHTML = sessions.map((s) => {
      const score = Number.isFinite(s.overall_score) ? s.overall_score : 0;
      const when = s.created_at ? new Date(s.created_at * 1000).toLocaleString() : 'unknown';
      const dur = Number.isFinite(s.duration_seconds) ? `${Math.round(s.duration_seconds)}s` : '0s';
      const mode = (s.coach_mode || 'general').replace('_', ' ');
      const context = (s.delivery_context || 'virtual').replace('_', ' ');
      return `
        <div class="recent-row">
          <span class="recent-score">${score}</span>
          <span class="recent-meta">${escapeHtml(when)} · ${escapeHtml(dur)} · ${escapeHtml(mode)} · ${escapeHtml(context)}</span>
          <span class="recent-id">${escapeHtml(String(s.session_id || '').slice(0, 8))}</span>
        </div>
      `;
    }).join('');
  } catch (err) {
    console.error('recent sessions fetch failed', err);
    container.innerHTML = '<div class="recent-empty">Recent session history unavailable.</div>';
  }
}

/* ── Helpers ─────────────────────────────────────── */
function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function markPipelineStepDone(id) {
  const step = document.getElementById(id);
  if (!step) return;
  step.classList.add('done');
  const dot = step.querySelector('.step-dot');
  if (dot) { dot.classList.remove('spinning'); dot.classList.add('done'); }
}

function renderGeneratedAssets(assets) {
  if (!Array.isArray(assets) || assets.length === 0) return '';

  const cards = assets.slice(0, 2).map((asset) => {
    const mime = asset?.mime_type || 'image/jpeg';
    const data = asset?.data_base64 || '';
    if (!data) return '';
    const dataUrl = `data:${mime};base64,${data}`;
    const filename = `${asset?.id || 'visual'}.jpg`;

    return `
      <div class="asset-card">
        <img class="asset-preview" src="${dataUrl}" alt="${escapeHtml(asset?.title || 'Generated visual')}" />
        <div class="asset-meta">
          <div class="asset-title">${escapeHtml(asset?.title || 'Generated visual')}</div>
          <div class="asset-desc">${escapeHtml(asset?.description || '')}</div>
          <div class="asset-actions">
            <span class="asset-source">${escapeHtml((asset?.source || 'generated').toUpperCase())}</span>
            <a class="asset-download" href="${dataUrl}" download="${filename}">Download</a>
          </div>
        </div>
      </div>
    `;
  }).join('');

  if (!cards.trim()) return '';
  return `
    <div class="asset-section">
      <div class="sc-events-title">Multimodal Visuals <span class="report-badge">2-image cap</span></div>
      <div class="asset-grid">${cards}</div>
    </div>
  `;
}

function renderCitationCards(researchTips) {
  if (!researchTips || !researchTips.trim()) return '';
  // Extract bullet lines from the research agent output
  const bullets = researchTips.split('\n')
    .map(l => l.replace(/^[-•*]\s*/, '').trim())
    .filter(l => l.length > 20);  // skip empty / header lines
  if (!bullets.length) return '';

  const cards = bullets.map(b => {
    // Extract **bold name** if present
    const nameMatch = b.match(/\*\*(.*?)\*\*/);
    const name = nameMatch ? nameMatch[1] : null;
    const rest = nameMatch ? b.replace(/\*\*.*?\*\*:?\s*/, '') : b;
    // Extract trailing (citation) in parens
    const citeMatch = rest.match(/\(([^)]{5,})\)\s*\.?\s*$/);
    const citation = citeMatch ? citeMatch[1] : null;
    const desc = (citeMatch ? rest.slice(0, -citeMatch[0].length) : rest).trim().replace(/\.$/, '');
    return `
      <div class="citation-card">
        ${name ? `<div class="citation-name">${escapeHtml(name)}</div>` : ''}
        <div class="citation-desc">${escapeHtml(desc)}.</div>
        ${citation ? `<div class="citation-source">— ${escapeHtml(citation)}</div>` : ''}
      </div>`;
  }).join('');

  return `
    <div class="citation-section">
      <div class="sc-events-title">Evidence-Based Techniques <span class="report-badge">google search · cited</span></div>
      <div class="citation-cards">${cards}</div>
    </div>`;
}
