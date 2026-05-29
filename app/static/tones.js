// Generic professional-radio-style call alert tones.
// All audio is synthesised at runtime — nothing proprietary is bundled.
(function () {
  const STORAGE_KEY = 'dmrstream.tones';
  let ctx = null;

  function tonesEnabled() {
    try {
      return localStorage.getItem(STORAGE_KEY) !== 'off';
    } catch {
      return true;
    }
  }

  function setTonesEnabled(on) {
    try {
      localStorage.setItem(STORAGE_KEY, on ? 'on' : 'off');
    } catch {}
  }

  function ensureCtx() {
    if (ctx) return ctx;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try { ctx = new Ctx(); } catch { ctx = null; }
    return ctx;
  }

  function resumeCtx() {
    const c = ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(()=>{});
  }

  // Two slightly-detuned oscillators through a low-pass filter give the
  // beep some body — closer to a small radio speaker than a pure sine.
  function playVoice(opts) {
    const c = ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(()=>{});
    const start = c.currentTime + (opts.delay || 0);
    const dur = opts.durationMs / 1000;
    const peak = opts.gain != null ? opts.gain : 0.15;

    const filter = c.createBiquadFilter();
    filter.type = 'lowpass';
    filter.frequency.value = opts.cutoff || 2800;
    filter.Q.value = 0.7;

    const gain = c.createGain();
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(peak, start + 0.005);
    gain.gain.setValueAtTime(peak, start + Math.max(0.01, dur - 0.025));
    gain.gain.exponentialRampToValueAtTime(0.0001, start + dur);

    const wave = opts.wave || 'square';
    const detune = opts.detune != null ? opts.detune : 5;
    for (const sign of [-1, 1]) {
      const osc = c.createOscillator();
      osc.type = wave;
      osc.frequency.setValueAtTime(opts.freq, start);
      if (opts.glideTo != null) {
        osc.frequency.exponentialRampToValueAtTime(opts.glideTo, start + dur);
      }
      osc.detune.value = sign * detune;
      osc.connect(filter);
      osc.start(start);
      osc.stop(start + dur + 0.02);
    }
    filter.connect(gain).connect(c.destination);
  }

  function playStartTone() {
    if (!tonesEnabled()) return;
    // Tight two-element alert: short rising lead, then a higher held note.
    playVoice({ freq: 1080, glideTo: 1180, durationMs: 40, delay: 0,    wave: 'square', cutoff: 2800, gain: 0.16 });
    playVoice({ freq: 1380,                 durationMs: 55, delay: 0.050, wave: 'square', cutoff: 2800, gain: 0.16 });
  }

  function playEndTone() {
    if (!tonesEnabled()) return;
    // Short low descending blip.
    playVoice({ freq: 720, glideTo: 560, durationMs: 110, delay: 0, wave: 'square', cutoff: 2200, gain: 0.13 });
  }

  window.dmrTones = {
    tonesEnabled,
    setTonesEnabled,
    ensureCtx,
    resumeCtx,
    playStartTone,
    playEndTone,
  };
})();
