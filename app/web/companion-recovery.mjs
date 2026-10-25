import {createSSEParser} from './companion-state.mjs';
import {TaskPlanTracker} from './companion-plan.mjs';

const ACTIVE = new Set(['queued', 'running']);
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'partial_success', 'interrupted']);
export const runStatusLabel = status => ({queued:'排队中', running:'进行中', requires_confirmation:'等待确认', completed:'已返回', failed:'失败', cancelled:'已停止', partial_success:'需核对', interrupted:'已中断'}[status] || '状态待核对');
const empty = () => ({run:null, task:'', text:'', cursor:0, timeline:[], phase:'idle', recovered:false, error:null, blocked:false});
const copy = state => ({...state, run:state.run ? {...state.run} : null, timeline:state.timeline.map(item => ({...item}))});

// Only the admitted current task can open an executing stream. History uses GET
// run + GET events exclusively. On reload we replay from zero: storing a cursor
// without the corresponding text would silently discard the partial response.
export class CompanionRecovery {
  constructor({request, fetchStream = (...args) => fetch(...args), storage, userId, conversationId, onChange = () => {}, onHistory = () => {}, onTerminal = () => {}, streamIdleMs = 35000, retryDelays = [600, 1500, 3500, 7000], wait = (ms, signal) => new Promise((resolve, reject) => {
    const timer = setTimeout(done, ms);
    function done() {signal?.removeEventListener('abort', abort); resolve();}
    function abort() {clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError'));}
    signal?.addEventListener('abort', abort, {once:true});
    if (signal?.aborted) abort();
  }), requestId = () => crypto.randomUUID()}) {
    Object.assign(this, {request, fetchStream, storage, userId, conversationId, onChange, onHistory, onTerminal, streamIdleMs, retryDelays, wait, requestId});
    this.key = `caibao.companion.run.${userId}.${conversationId}`;
    this.state = empty(); this.history = []; this.generation = 0; this.abort = null; this.intent = this.readIntent();
    this.planTrackers = new WeakMap();
  }
  readIntent() {try {return JSON.parse(this.storage?.getItem(this.key) || 'null');} catch {return null;}}
  persist(intent) {
    this.intent = intent;
    try {if (intent) this.storage?.setItem(this.key, JSON.stringify(intent)); else this.storage?.removeItem(this.key);} catch { /* Recovery also discovers the server-side run list when storage is unavailable. */ }
  }
  publish(patch = {}) {Object.assign(this.state, patch); this.onChange(copy(this.state));}
  dispose() {this.generation++; this.abort?.abort(); this.abort = null;}
  async refreshHistory() {
    const generation = this.generation;
    const result = await this.request(`/agent/runs?conversation_id=${encodeURIComponent(this.conversationId)}&trigger_channel=companion&limit=20`);
    if (generation !== this.generation) return [];
    this.history = result.items || []; this.onHistory(this.history); return this.history;
  }
  async restore() {
    this.dispose(); const generation = this.generation;
    this.publish({phase:'loading', blocked:true, recovered:true});
    try {
      const history = await this.refreshHistory(); if (generation !== this.generation) return;
      const intent = this.intent;
      const current = (intent?.run_id && history.find(run => run.run_id === intent.run_id)) ||
        (intent?.submission && history.find(run => run.client_request_id === intent.submission.client_request_id)) ||
        history.find(run => ACTIVE.has(run.status) || run.status === 'requires_confirmation');
      if (current) return this.follow(current, {recovered:true, admitted:current.status !== 'queued' || Boolean(intent?.run_id === current.run_id || intent?.submission?.client_request_id && intent.submission.client_request_id === current.client_request_id)});
      if (intent?.run_id) return this.follow({run_id:intent.run_id, task:intent.task}, {recovered:true, admitted:true});
      // Retrying an uncertain admission carries the same immutable body and key.
      // The server atomically returns the existing run, never repeats its work.
      if (intent?.submission) return this.admit(intent.submission, true);
      this.publish({...empty(), recovered:true});
    } catch (error) {if (generation === this.generation) this.unknown(error);}
  }
  async submit(payload) {
    if (this.state.blocked) throw new Error('上个任务的状态还没核对，请先恢复连接。');
    const submission = {...payload, conversation_id:this.conversationId, client_request_id:this.requestId()};
    this.persist({submission, task:payload.task});
    this.state = {...empty(), task:payload.task, phase:'loading', blocked:true}; this.publish();
    return this.admit(submission, false);
  }
  async admit(submission, recovered) {
    const generation = this.generation;
    try {
      const start = await this.request('/companion/runs', {method:'POST', body:JSON.stringify(submission)});
      if (generation !== this.generation) return;
      this.persist({run_id:start.run_id, task:submission.task});
      return this.follow({...start, task:submission.task}, {recovered, admitted:true});
    } catch (error) {
      if (generation !== this.generation) return;
      // A definitive validation rejection did not admit work. Transport/server
      // errors keep the intent and block a second task until it is reconciled.
      if (error.status >= 400 && error.status < 500 && ![408, 409, 429].includes(error.status)) {
        this.persist(null); this.publish({phase:'finished', blocked:false, error:error.message});
      } else this.unknown(error);
    }
  }
  unknown(error) {this.publish({phase:'unknown', blocked:true, recovered:true, error:error?.message || '暂时无法核对任务状态', errorStatus:error?.status});}
  async retry() {
    if (this.state.run?.run_id) return this.follow(this.state.run, {recovered:true, admitted:this.admitted});
    return this.restore();
  }
  async follow(run, {recovered = true, admitted = false} = {}) {
    this.dispose(); const generation = this.generation;
    this.abort = new AbortController(); this.admitted = admitted;
    const intent = this.intent;
    // Do not manufacture admission for a queued run merely found in history.
    if (admitted || run.status !== 'queued') this.persist({run_id:run.run_id, task:run.task || intent?.task || ''});
    this.state = {...empty(), run:{...run}, task:run.task || intent?.task || '', phase:recovered ? 'reconnecting' : 'running', recovered, blocked:true};
    this.state.run.task_plan = this.planTracker(this.state).plan;
    this.publish();
    void this.refreshHistory().catch(() => {});
    let attempt = 0;
    while (generation === this.generation) {
      try {
        await this.drain(this.state, generation);
        if (generation !== this.generation) return;
        const live = await this.request(`/agent/runs/${encodeURIComponent(run.run_id)}`);
        if (generation !== this.generation) return;
        this.reconcile(this.state, live); this.publish();
        if (!ACTIVE.has(live.status)) {this.settle(live); return;}
        if (live.status === 'queued' && !admitted) {
          this.publish({phase:'unknown', error:'发现了尚未启动的任务。此记录只供查看，不会自动执行。', blocked:true}); return;
        }
        this.publish({phase:'running', error:null});
        await this.readStream(generation);
        // EOF is not completion. Inspect the durable run even if a terminal
        // event arrived, so a stale approval event cannot expose old controls.
        if (generation !== this.generation) return;
        const latest = await this.request(`/agent/runs/${encodeURIComponent(run.run_id)}`);
        if (generation !== this.generation) return;
        this.reconcile(this.state, latest);
        if (!ACTIVE.has(latest.status)) {await this.drain(this.state, generation); if (generation === this.generation) {this.reconcile(this.state, latest); this.settle(latest);} return;}
        throw new Error('任务仍在执行，正在接回进度。');
      } catch (error) {
        if (generation !== this.generation || error.name === 'AbortError') return;
        this.publish({phase:'reconnecting', recovered:true, error:error.message, blocked:true});
        if (attempt >= this.retryDelays.length) {this.unknown(error); return;}
        try {await this.wait(this.retryDelays[attempt++], this.abort.signal);} catch {return;}
      }
    }
  }
  async drain(target, generation = null) {
    while (true) {
      const page = await this.request(`/agent/runs/${encodeURIComponent(target.run.run_id)}/events?after=${target.cursor}&limit=100`);
      if (generation !== null && generation !== this.generation) return;
      const before = target.cursor;
      for (const event of page.items || []) this.apply(target, event);
      if (target === this.state) this.publish();
      if (!page.has_more) return;
      if (target.cursor <= before) throw new Error('任务记录暂时未同步，稍后重新核对。');
    }
  }
  apply(target, event) {
    if (event.run_id !== target.run.run_id) return;
    const seq = Number(event.seq);
    if (!Number.isSafeInteger(seq) || seq <= target.cursor) return;
    target.cursor = seq;
    const payload = event.payload || {};
    if (event.event === 'plan.updated') {
      const tracker = this.planTracker(target);
      if (tracker.event(event.run_id,payload.task_plan)) target.run.task_plan = tracker.plan;
      // A checklist update is not a tool, emotional state, or task completion.
      return;
    }
    if (event.event === 'llm.delta') target.text += payload.text || '';
    if (event.event === 'tool.started' || event.event === 'tool.result') {
      let item = target.timeline.find(item => item.step === event.step_index && item.tool === payload.tool_name && item.status === 'running');
      if (!item) {item = {id:seq, step:event.step_index, tool:payload.tool_name || '工具'}; target.timeline.push(item);}
      item.status = event.event === 'tool.started' ? 'running' : payload.result?.error ? 'failed' : 'completed';
    }
    if (event.event === 'run.completed') {
      const tracker = this.planTracker(target);
      tracker.event(event.run_id,payload.task_plan);
      target.run = {...target.run, ...payload, task_plan:tracker.plan};
      if (TERMINAL.has(payload.status)) tracker.lock();
      if (payload.answer !== undefined) target.text = payload.answer;
    }
    if (event.event === 'confirmation.required') target.run = {...target.run, status:'requires_confirmation', approval_token:payload.approval_token, required_confirmations:[payload]};
    if (event.event === 'run.cancelled') {target.run.status = 'cancelled'; this.planTracker(target).lock();}
    if (event.event === 'run.failed') {target.run.status = 'failed'; target.error = payload.error; this.planTracker(target).lock();}
    target.lastEvent = event;
  }
  reconcile(target, run) {
    const plan = this.planTracker(target).snapshot(run);
    target.run = {...target.run, ...run, task_plan:plan}; target.task = run.task || target.task;
    if (!ACTIVE.has(run.status) && run.answer) target.text = run.answer;
    for (const step of run.steps || []) {
      if (!step.tool_name || step.step_type !== 'tool_call') continue;
      let item = target.timeline.find(item => item.step === step.step_index && item.tool === step.tool_name);
      if (!item) {item = {id:step.step_id, step:step.step_index, tool:step.tool_name}; target.timeline.push(item);}
      item.status = step.status;
    }
  }
  planTracker(target) {
    let tracker = this.planTrackers.get(target);
    if (!tracker || tracker.runId !== target.run.run_id) {
      tracker = new TaskPlanTracker(target.run); this.planTrackers.set(target,tracker);
    }
    return tracker;
  }
  settle(run) {
    const phase = run.status === 'requires_confirmation' ? 'waiting' : TERMINAL.has(run.status) ? 'finished' : 'unknown';
    this.publish({phase, blocked:phase !== 'finished', error:phase === 'unknown' ? '任务状态待核对' : this.state.error});
    if (phase === 'finished') this.persist(null);
    if (phase === 'finished' && !this.state.recovered && run.status === 'completed') this.onTerminal(copy(this.state));
    void this.refreshHistory().catch(() => {});
  }
  async readStream(generation) {
    const cursor = this.state.cursor, runId = this.state.run.run_id;
    const response = await this.fetchStream(`/api/v1/agent/runs/${encodeURIComponent(runId)}/stream?after=${cursor}`, {
      credentials:'same-origin', signal:this.abort.signal, headers:{Accept:'text/event-stream', 'Last-Event-ID':String(cursor)},
    });
    if (!response.ok || !response.body) throw new Error(`任务连接暂不可用 (${response.status})`);
    const reader = response.body.getReader(), decoder = new TextDecoder();
    const parser = createSSEParser(event => {if (generation === this.generation) {this.apply(this.state, event); this.publish();}});
    let timer;
    try {
      while (generation === this.generation) {
        const {done,value} = await Promise.race([reader.read(), new Promise((_,reject) => {timer=setTimeout(() => reject(new Error('暂未收到任务心跳，正在核对进度。')),this.streamIdleMs);})]);
        clearTimeout(timer); if (done) break; parser.push(decoder.decode(value,{stream:true}));
      }
      parser.push(decoder.decode());
    } finally {clearTimeout(timer); await reader.cancel().catch(() => {}); reader.releaseLock();}
  }
  async inspect(runId) {
    const run = await this.request(`/agent/runs/${encodeURIComponent(runId)}`);
    if (run.conversation_id !== this.conversationId || run.trigger_channel !== 'companion') throw new Error('此任务不属于当前对话。');
    const preview = {...empty(), run, task:run.task, recovered:true};
    await this.drain(preview); this.reconcile(preview, run);
    return {...preview, phase:run.status === 'requires_confirmation' ? 'waiting' : ACTIVE.has(run.status) ? 'running' : 'finished'};
  }
  async accept(run) {
    // Confirmation/cancellation responses can race the observer. Restarting the
    // observer only replays evidence; it never resubmits or repeats confirmation.
    return this.follow(run, {recovered:true, admitted:true});
  }
}
