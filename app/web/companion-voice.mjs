// A voice turn only creates an editable draft. It never submits an Agent task.
const ACTIVE = new Set(['consent','authorizing','listening','processing']);
const reasonText = {
  native_unavailable:'这个版本暂不支持设备端语音输入，可使用 macOS 系统听写或打字。',
  on_device_unavailable:'当前语言或设备暂不支持设备端识别，可使用系统听写；不会切换到云端识别。',
  unavailable:'语音识别暂不可用，可使用系统听写或打字。',
  microphone_denied:'麦克风权限未开启。可以在系统设置中调整，或继续打字。',
  speech_denied:'语音识别权限未开启。可以在系统设置中调整，或继续打字。',
  speech_restricted:'系统限制了语音识别，可以继续打字。',
  recognizer_unavailable:'设备端识别暂不可用，可使用系统听写或打字。',
  audio_unavailable:'没有可用的麦克风，可以继续打字。',
  recognition_failed:'设备端识别没有完成，草稿仍然保留。',
  final_timeout:'整理语音超时了，草稿仍然保留。',
  permission_denied:'语音权限未开启，可以继续打字。',
  'not-allowed':'浏览器未获得语音权限，可以继续打字。',
  'service-not-allowed':'浏览器语音服务不可用，可以继续打字。',
  'audio-capture':'没有可用的麦克风，可以继续打字。',
  'no-speech':'没有识别到清楚的话，草稿仍然保留。',
  no_speech:'没有识别到清楚的话，草稿仍然保留。',
  timeout:'这次语音输入超时了，草稿仍然保留。',
  draft_full:'这段话还没加入草稿。请先缩短草稿，再点“加入草稿”。',
  transcript_too_long:'这次识别的文字太长，请分成较短的几句再说。草稿仍然保留。',
  failed:'没能完成这次语音输入，草稿仍然保留，可以继续打字。',
};
let fallbackId = 0;

export class CompanionVoice {
  constructor({desktop = false, transport, capabilities = () => ({}), createRecognition,
    readDraft = () => '', writeDraft = () => {}, canStart = () => true,
    stopSpeech = () => {}, onChange = () => {},
    turnId = () => globalThis.crypto?.randomUUID?.() || `voice-${Date.now()}-${++fallbackId}`,
    schedule = (fn, ms) => setTimeout(fn, ms), unschedule = handle => clearTimeout(handle),
    authorizationMs = 0, listeningMs = 37_000, processingMs = 8_000, maxDraftChars = 4000,
  } = {}) {
    Object.assign(this,{desktop,transport,capabilities,createRecognition,readDraft,writeDraft,canStart,
      stopSpeech,onChange,turnId,schedule,unschedule,authorizationMs,listeningMs,processingMs,maxDraftChars});
    this.state = {phase:'idle',active:false,turnId:null,source:null,text:'',inserted:false,reason:null,message:''};
    this.round = null; this.timer = null; this.browserConsent = false;
  }

  toggle() {
    if (this.state.phase === 'listening') return this.finish();
    if (['consent','authorizing'].includes(this.state.phase)) {this.cancel(); return false;}
    if (this.state.phase === 'processing' || !this.canStart()) return false;
    if (this.state.phase === 'review' && !this.state.inserted) return false;
    if (this.desktop) {
      const support = this.capabilities();
      if (!support?.recognition || !support?.onDeviceRecognition || typeof this.transport !== 'function') {
        this._set({phase:'error',reason:'native_unavailable',message:reasonText.native_unavailable}); return false;
      }
    } else if (typeof this.createRecognition !== 'function') {
      this._set({phase:'error',reason:'unavailable',message:reasonText.unavailable}); return false;
    }
    this.stopSpeech();
    const round = {id:this.turnId(),source:this.desktop?'native':'browser',recognition:null};
    this.round = round;
    this._set({phase:!this.desktop && !this.browserConsent?'consent':'authorizing',
      turnId:round.id,source:round.source,text:'',inserted:false,reason:null,message:''});
    if (this.state.phase !== 'consent') this._start(round);
    return true;
  }

  allowBrowser(turnId) {
    if (this.state.phase !== 'consent' || !this.round || turnId !== this.round.id || !this.canStart()) return false;
    this.browserConsent = true;
    this._set({phase:'authorizing'}); this._start(this.round); return true;
  }

  finish() {
    if (!this.round || this.state.phase !== 'listening') return false;
    const round = this.round;
    this._set({phase:'processing'});
    try {
      if (round.source === 'native') this.transport('finishListening',{turn_id:round.id});
      else round.recognition.stop();
    } catch {this._fail('failed');}
    return true;
  }

  cancel(reason = 'cancelled') {
    const round = this.round;
    this.round = null; this.browserConsent = false; this._clearTimer();
    if (round) {
      // Invalidate before calling transport: cancellation may synchronously emit
      // an old recognition callback and it must not write to the current draft.
      try {
        if (round.source === 'native') this.transport('cancelListening',{turn_id:round.id});
        else round.recognition?.abort();
      } catch { /* A disappearing device/window still invalidates the turn. */ }
    }
    this._set({phase:'idle',turnId:null,source:null,text:'',inserted:false,reason,message:''});
  }

  updateAvailability() {
    if (this.state.active && !this.canStart()) this.cancel('task_busy');
  }

  handleNative(detail = {}) {
    const round = this.round;
    if (!round || round.source !== 'native' || detail.turn_id !== round.id) return false;
    if (!this.canStart()) {this.cancel('task_busy'); return false;}
    if (detail.state === 'authorizing' && this.state.phase === 'authorizing') return true;
    if (detail.state === 'listening' && this.state.phase === 'authorizing') {this._set({phase:'listening'}); return true;}
    if (detail.state === 'processing' && ['authorizing','listening','processing'].includes(this.state.phase)) {
      this._set({phase:'processing'}); return true;
    }
    if (detail.state === 'result') {this._result(detail.text); return true;}
    if (detail.state === 'error') {this._fail(detail.reason || 'failed'); return true;}
    if (detail.state === 'cancelled') {this.cancel(detail.reason || 'cancelled'); return true;}
    return false;
  }

  insertTranscript() {
    if (this.state.phase !== 'review' || this.state.inserted || !this.state.text || !this.canStart()) return false;
    const draft = String(this.readDraft() || '');
    const next = `${draft}${draft && !draft.endsWith('\n')?'\n':''}${this.state.text}`;
    if (next.length > this.maxDraftChars) {
      this._set({reason:'draft_full',message:reasonText.draft_full}); return false;
    }
    this.writeDraft(next);
    this._set({inserted:true,reason:null,message:'已加入草稿，确认文字后再发送。'});
    return true;
  }

  _start(round) {
    if (round !== this.round) return;
    if (!this.canStart()) {this.cancel('task_busy'); return;}
    try {
      if (round.source === 'native') {this.transport('startListening',{turn_id:round.id,lang:'zh-CN'}); return;}
      const recognition = this.createRecognition(); round.recognition = recognition;
      recognition.lang = 'zh-CN'; recognition.interimResults = false; recognition.continuous = false;
      const current = () => {
        if (this.round !== round) return false;
        if (!this.canStart()) {this.cancel('task_busy'); return false;}
        return true;
      };
      recognition.onstart = () => {if (current() && this.state.phase === 'authorizing') this._set({phase:'listening'});};
      recognition.onresult = event => {
        if (!current()) return;
        const results = Array.from(event.results || []).slice(event.resultIndex || 0);
        if (results.length && results.every(result => result.isFinal === false)) return;
        const text = results.filter(result => result.isFinal !== false).map(result => result[0]?.transcript || '').join('');
        this._result(text);
      };
      recognition.onerror = event => {if (current()) this._fail(event.error || 'failed');};
      recognition.onend = () => {if (current()) this._fail('no_speech');};
      recognition.start();
    } catch {this._fail('failed');}
  }

  _result(raw) {
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (!text) {this._fail('no_speech'); return;}
    if (text.length > this.maxDraftChars) {this._fail('transcript_too_long'); return;}
    const round = this.round; this.round = null; this._clearTimer();
    // Stop browser capture even if its final result arrived before onend.
    try {round?.recognition?.abort();} catch { /* Final text already retained. */ }
    this._set({phase:'review',text,inserted:false,reason:null,message:''});
    this.insertTranscript();
  }

  _fail(reason) {
    const code = typeof reason === 'string' ? reason : 'failed';
    this.cancel(code);
    this._set({phase:'error',reason:code,message:Object.hasOwn(reasonText,code) ? reasonText[code] : reasonText.failed});
  }

  _clearTimer() {if (this.timer !== null) this.unschedule(this.timer); this.timer = null;}
  _set(next) {
    this._clearTimer();
    this.state = {...this.state,...next}; this.state.active = ACTIVE.has(this.state.phase);
    const round = this.round;
    if (round && this.state.active && this.state.phase !== 'consent') {
      const delay = this.state.phase === 'listening' ? this.listeningMs
        : this.state.phase === 'processing' ? this.processingMs : this.authorizationMs;
      if (delay > 0) this.timer = this.schedule(() => {
        if (this.round === round) this._fail('timeout');
      },delay);
    }
    this.onChange({...this.state});
  }
}
