import {initialState, reduceEvent, speechText} from './companion-state.mjs';
import {CompanionRecovery, runStatusLabel} from './companion-recovery.mjs';
import {CompanionMemory} from './companion-memory.mjs';
import {executionSummary} from './companion-execution.mjs';
import {receiptSummary} from './companion-receipt.mjs';
import {CompanionVoice} from './companion-voice.mjs';

const $ = id => document.getElementById(id);
const desktop = new URLSearchParams(location.search).get('desktop') === '1'
  || typeof window.webkit?.messageHandlers?.caibao?.postMessage === 'function';
const native = (action, extra = {}) => window.webkit?.messageHandlers?.caibao?.postMessage({action, ...extra});
let state = initialState(), account = null, conversationId = null, currentRun = null;
let runtimeProfile = null, backends = [];
let busy = false, pendingApproval = false, registering = false, toastTimer;
let voiceState = {phase:'idle',active:false};
let speechGeneration = 0, speaking = false, controller = null, currentContent = null, approvalToken = null, previewGeneration = 0;
let identityGeneration = 0, conversationPromise = null, memoryDraft = null, documentDraft = null, pendingMemoryDelete = null;
const memoryNotebook = new CompanionMemory({request:api,onChange:renderMemory});
const runContents = new Map();
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const voiceInput = new CompanionVoice({desktop,transport:native,capabilities:() => window.caibaoNative || {},
  createRecognition:Recognition ? () => new Recognition() : undefined,
  readDraft:() => $('message').value,
  writeDraft:value => {$('message').value = value; $('message').dispatchEvent(new Event('input',{bubbles:true}));},
  canStart:() => !busy && !pendingApproval && !$('auth-dialog').open && $('memory-notebook').hidden,
  stopSpeech,onChange:renderVoice,
});
const preferences = (() => {try {return JSON.parse(localStorage.getItem('caibao.companion.preferences') || '{}');} catch {return {};}})();
$('voice').checked = preferences.voice === true;
$('memory').checked = preferences.memory !== false;
$('mode').value = preferences.mode === 'chat' ? 'chat' : 'agent';
if (desktop) document.body.classList.add('desktop', 'compact');
function savePreferences() {
  localStorage.setItem('caibao.companion.preferences', JSON.stringify({voice: $('voice').checked, memory: $('memory').checked, mode: $('mode').value}));
}
function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}
function setState(next) {
  state = {...state, ...next};
  renderPresence();
}
function renderPresence() {
  const overlay = {
    consent:{activity:'idle',emotion:'curious',label:'先看看语音输入的说明'},
    authorizing:{activity:'idle',emotion:'curious',label:'等你允许这次语音输入'},
    listening:{activity:'listening',emotion:'curious',label:'你说，我在听'},
    processing:{activity:'transcribing',emotion:'focused',label:'正在整理刚才的话'},
  }[voiceState.phase];
  const visible = overlay || state;
  $('habitat').dataset.activity = speaking && !voiceState.active ? 'speaking' : visible.activity;
  $('habitat').dataset.emotion = visible.emotion;
  const labels = {idle:'悠闲', happy:'开心', curious:'好奇', focused:'认真', sleepy:'困困', concerned:'担心'};
  $('mood-chip').textContent = labels[visible.emotion] || '陪伴中';
  $('pet-status').querySelector('span').textContent = visible.label;
  $('pet-bubble').textContent = visible.label;
}
async function api(path, options = {}, retry = true) {
  const response = await fetch(`/api/v1${path}`, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json', ...options.headers}});
  if (response.status === 401 && retry && !path.startsWith('/auth/')) {
    const refresh = await fetch('/api/v1/auth/refresh', {method:'POST', credentials:'same-origin'});
    if (refresh.ok) return api(path, options, false);
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(typeof data.detail === 'string' ? data.detail : `请求没有完成 (${response.status})`);
    error.status = response.status; throw error;
  }
  return response.status === 204 ? null : response.json();
}
function message(role, text = '', metadata = {}) {
  $('welcome')?.remove();
  const element = document.createElement('div'); element.className = `message ${role}`;
  const label = document.createElement('div'); label.className = 'speaker'; label.textContent = role === 'user' ? '你' : '彩宝';
  const content = document.createElement('div'); content.className = 'content'; content.textContent = text;
  element.append(label, content);
  if (role === 'user') {
    const owner = account?.user_id;
    const save = document.createElement('button'); save.type = 'button'; save.className = 'message-save-memory'; save.textContent = '存到记忆';
    save.onclick = () => {if (owner === account?.user_id) void openMemory({text,sourceId:metadata.message_id});};
    element.append(save);
  }
  $('messages').append(element); scrollMessages(); return content;
}
function scrollMessages() { $('messages').scrollTop = $('messages').scrollHeight; }
function connection(text, online) {
  $('connection-label').textContent = text; $('connection-dot').classList.toggle('offline', !online);
}
function showAuth() {voiceInput.cancel('account'); if (!$('auth-dialog').open) $('auth-dialog').showModal();}
async function signedIn(user) {
  const switched = account?.user_id !== user.user_id;
  if (switched) {
    voiceInput.cancel('account'); $('message').value = '';
    identityGeneration++; resetMemoryNotebook();
    $('messages').replaceChildren(); $('task-log').replaceChildren();
    controller?.dispose(); controller = null; currentContent = null; runContents.clear(); previewGeneration++;
    $('history-preview').hidden = true; renderTaskHistory([]); $('run-strip').hidden = true;
    clearApproval(); currentRun = null; stopSpeech();
    $('task-status').textContent = '没有任务的时候，发会儿呆也很好。';
  }
  account = user; $('account-button').hidden = false;
  const generation = identityGeneration;
  const current = () => generation === identityGeneration && account?.user_id === user.user_id;
  conversationId = localStorage.getItem(`caibao.companion.conversation.${user.user_id}`);
  try {
    const profile = await api('/companion/profile'); if (!current()) return;
    runtimeProfile = profile;
    const models = await api('/llm/models');
    if (!current()) return;
    $('model-select').replaceChildren(new Option('默认模型', ''));
    for (const model of models.items) $('model-select').add(new Option(model.model_name,model.model_name));
    const preferredModel = localStorage.getItem(`caibao.companion.model.${user.user_id}`);
    if (models.items.some(model => model.model_name === preferredModel)) $('model-select').value = preferredModel;
    const availableBackends = await api('/companion/backends'); if (!current()) return;
    backends = availableBackends;
    $('backend').replaceChildren();
    for (const item of backends) {
      const option = new Option(item.label + (item.available ? '' : ' · 未就绪'),item.id);
      option.disabled = !item.available; $('backend').add(option);
    }
    const preferredBackend = localStorage.getItem(`caibao.companion.backend.${user.user_id}`);
    if (backends.some(item => item.id === preferredBackend && item.available)) $('backend').value = preferredBackend;
    updateBackend();

    renderModelStatus();
    setState({...profile.state, label: profile.state.activity === 'sleeping' ? '眯一会儿，叫我就好' : '嗯，我在。今天想做点什么？'});
    if (!conversationId) message('assistant', profile.greeting);
    if (conversationId) {
      try {
        const history = await api(`/chat/history?conversation_id=${encodeURIComponent(conversationId)}&limit=40`);
        if (!current()) return;
        const items = history.items.filter(item => item.channel === 'companion').reverse();
        for (const item of items) {
          message('user', item.request_text, {message_id:item.message_id}); const content = message('assistant', item.response_text);
          if (item.response_payload?.run_id) runContents.set(item.response_payload.run_id, content);
        }
        if (!items.length) message('assistant',profile.greeting);
      } catch (error) {
        if (!current()) return;
        if (error.status === 404) {conversationId = null; localStorage.removeItem(`caibao.companion.conversation.${user.user_id}`);}
        else throw error;
      }
    }
    if (conversationId) {await createController().restore(); if (!current()) return;}
  } catch (error) {if (current()) throw error;}
}
async function ensureConversation() {
  if (!conversationId && !conversationPromise) {
    const generation = identityGeneration, owner = account?.user_id;
    const pending = (async () => {
      const item = await api('/conversations', {method:'POST',body:JSON.stringify({title:'与彩宝的小日常'})});
      if (generation !== identityGeneration || owner !== account?.user_id) throw new Error('对话已经切换，请在当前对话中继续。');
      conversationId = item.conversation_id;
      localStorage.setItem(`caibao.companion.conversation.${account.user_id}`, conversationId);
    })();
    conversationPromise = pending;
    try {await pending;} finally {if (conversationPromise === pending) conversationPromise = null;}
  } else if (conversationPromise) {
    await conversationPromise;
  }
}
function setBusy(value) {
  busy = value; voiceInput.updateAvailability(); updateComposer(); $('new-chat').disabled = value || pendingApproval;
  $('account-button').disabled = value || pendingApproval;
  $('backend').disabled = value;
  const cli = $('backend').value !== 'api';
  $('mode').disabled = value || cli; $('memory').disabled = value || cli; $('model-select').disabled = value || cli;
  $('stop-task').hidden = !value || !currentRun || pendingApproval;
}
function showApproval(run) {
  currentRun = run.run_id; approvalToken = run.approval_token || null; pendingApproval = true; $('approval').hidden = false; $('send').disabled = true;
  voiceInput.updateAvailability(); updateComposer();
  $('approval-detail').textContent = (run.required_confirmations || []).map(call => `${call.tool_name}\n${JSON.stringify(call.arguments, null, 2)}`).join('\n\n') || '请到工作台查看操作详情后确认。';
  $('approve').disabled = !run.required_confirmations?.length; $('new-chat').disabled = true; $('account-button').disabled = true;
}
function clearApproval() {pendingApproval = false; approvalToken = null; $('approval').hidden = true; updateComposer(); $('new-chat').disabled = busy;}
function createController() {
  if (controller?.conversationId === conversationId && controller.userId === account?.user_id) return controller;
  controller?.dispose();
  controller = new CompanionRecovery({request:api, storage:localStorage, userId:account.user_id, conversationId,
    onChange:renderRun, onHistory:renderTaskHistory,
    onTerminal:snapshot => {if (snapshot.text) speak(snapshot.text);},
  });
  return controller;
}
const taskHint = snapshot => {
  if (snapshot.phase === 'loading') return '正在核对任务记录…';
  if (snapshot.phase === 'reconnecting') return '正在接回进度，任务会继续；无需重复发送。';
  if (snapshot.phase === 'unknown') return '还没核对到最终状态。已保留任务，请恢复连接后再继续。';
  if (snapshot.phase === 'waiting') return '等待你的确认，操作尚未执行。';
  if (snapshot.phase === 'running') return '正在处理，离开页面后也会继续。';
  if (!snapshot.run) return '没有任务的时候，发会儿呆也很好。';
  return {completed:'这次回复已返回，请查看结果。', partial_success:'有些步骤没完成，详情见回复。', failed:'执行遇到问题，详情见回复。', cancelled:'已停止后续步骤。已经开始的操作可能仍会结束。', interrupted:'运行中断，已保留结果；检查记录后再决定下一步。'}[snapshot.run.status] || snapshot.error || '任务记录已保留。';
};
function renderTimeline(container, timeline, status) {
  container.replaceChildren();
  const labels = {running:['cancelled','interrupted','failed'].includes(status) ? '未返回' : '进行中', completed:'已返回', skipped:'已跳过', failed:'失败'};
  for (const item of timeline) {
    const line = document.createElement('div'); line.textContent = `${labels[item.status] || item.status} · ${item.tool}`;
    container.append(line);
  }
}
function renderExecution(container, run) {
  if (!run) {container.replaceChildren(); container.hidden = true; delete container.dataset.execution; return;}
  const summary = executionSummary(run);
  const key = JSON.stringify(summary);
  if (!container.hidden && container.dataset.execution === key) return;
  container.replaceChildren(); container.hidden = false; container.dataset.execution = key;
  const heading = document.createElement('div'); heading.className = 'execution-heading';
  const label = document.createElement('span'); label.className = 'execution-label'; label.textContent = '本次执行';
  const identity = document.createElement('span'); identity.className = 'execution-identity'; identity.textContent = summary.identity;
  const context = document.createElement('p'); context.className = 'execution-context'; context.textContent = summary.context;
  heading.append(label,identity); container.append(heading,context);
}
function renderReceipt(container, run) {
  if (!run) {container.replaceChildren(); container.hidden = true; delete container.dataset.receipt; return;}
  const summary = receiptSummary(run);
  const key = JSON.stringify(summary);
  if (!container.hidden && container.dataset.receipt === key) return;
  container.replaceChildren(); container.hidden = false; container.dataset.receipt = key;
  container.dataset.review = String(summary.needsReview);
  const heading = document.createElement('div'); heading.className = 'receipt-heading';
  const label = document.createElement('span'); label.className = 'execution-label'; label.textContent = '执行回执';
  const usage = document.createElement('span'); usage.className = 'receipt-usage'; usage.textContent = summary.usage;
  heading.append(label,usage); container.append(heading);
  for (const [field,className] of [['outcomes','receipt-outcomes'],['pending','receipt-pending'],['ending','receipt-ending'],['note','receipt-note']]) {
    if (!summary[field]) continue;
    const line = document.createElement('p'); line.className = className; line.textContent = summary[field]; container.append(line);
  }
}
function renderRun(snapshot) {
  currentRun = snapshot.run?.run_id || null;
  if (currentRun) {
    if (runContents.has(currentRun)) currentContent = runContents.get(currentRun);
    else {
      if (!currentContent) {if (snapshot.task) message('user',snapshot.task); currentContent = message('assistant','');}
      runContents.set(currentRun,currentContent);
    }
  }
  if (currentContent && (snapshot.text || snapshot.phase === 'finished')) {
    currentContent.textContent = snapshot.text || snapshot.error || (snapshot.run?.status === 'cancelled' ? '任务已停止。' : '这次没有生成回复。');
    currentContent.parentElement.classList.toggle('error',['failed','partial_success','interrupted'].includes(snapshot.run?.status));
  }
  clearApproval();
  // Confirmation controls only use the authoritative GET run snapshot after
  // journal recovery; a replayed old approval event cannot authorize anything.
  if (snapshot.phase === 'waiting') showApproval(snapshot.run);
  if (snapshot.phase === 'unknown' && snapshot.errorStatus === 401) {connection('请重新登录，再接回任务',false); showAuth();}
  setBusy(snapshot.blocked && snapshot.phase !== 'waiting');
  const hint = taskHint(snapshot);
  $('task-status').textContent = hint; $('run-connection').textContent = hint;
  renderExecution($('run-execution'),snapshot.run);
  renderReceipt($('run-receipt'),snapshot.run);
  $('run-strip').hidden = snapshot.phase === 'idle';
  $('run-strip').dataset.phase = snapshot.phase;
  $('run-reconnect').hidden = !['unknown','reconnecting'].includes(snapshot.phase);
  renderTimeline($('task-log'),snapshot.timeline,snapshot.run?.status);
  if (snapshot.phase === 'reconnecting' || snapshot.phase === 'unknown' || snapshot.phase === 'loading')
    setState({activity:'reconnecting',emotion:'focused',label:snapshot.phase === 'unknown' ? '任务状态还在核对，先不重复操作' : '接回进度，事情还记着呢'});
  else if (snapshot.phase === 'waiting') setState({activity:'waiting',emotion:'curious',label:'这个操作，需要你点头'});
  else if (snapshot.phase === 'finished' && snapshot.run) setState(reduceEvent(state,{event:snapshot.run.status === 'interrupted' ? 'run.interrupted' : snapshot.run.status === 'cancelled' ? 'run.cancelled' : 'run.completed',payload:snapshot.run}));
  else if (snapshot.lastEvent) setState(reduceEvent(state,snapshot.lastEvent));
  else if (snapshot.phase === 'running') setState({activity:'thinking',emotion:'focused',label:'接着做，进度还在'});
  // The receipt changes the chat viewport height. Scroll only after all run
  // details and visibility have settled so the returned reply stays visible.
  if (currentContent && (snapshot.text || snapshot.phase === 'finished')) scrollMessages();
}
function renderTaskHistory(items) {
  $('history-count').textContent = items.length ? ` · ${items.length}` : '';
  $('history-list').replaceChildren();
  if (!items.length) {const empty = document.createElement('p'); empty.className = 'history-empty'; empty.textContent = '这里会保留本次对话的任务与执行记录。'; $('history-list').append(empty);}
  for (const run of items) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'history-item';
    button.dataset.runId = run.run_id;
    const title = document.createElement('span'); title.textContent = run.task;
    const status = document.createElement('span'); status.className = 'history-status'; status.dataset.status = run.status; status.textContent = runStatusLabel(run.status);
    button.append(title,status); button.onclick = () => inspectTask(run.run_id); $('history-list').append(button);
  }
}
async function inspectTask(runId) {
  voiceInput.cancel('history');
  const generation = ++previewGeneration; $('history-preview').hidden = false; $('history-answer').textContent = '正在读取记录…';
  $('history-title').textContent = '读取任务记录'; $('history-state').textContent = ''; $('history-timeline').replaceChildren(); renderExecution($('history-execution'),null); renderReceipt($('history-receipt'),null);
  try {
    const preview = await controller.inspect(runId); if (generation !== previewGeneration) return;
    $('history-title').textContent = preview.task;
    $('history-state').textContent = `${runStatusLabel(preview.run.status)} · 只读记录`;
    renderExecution($('history-execution'),preview.run);
    renderReceipt($('history-receipt'),preview.run);
    $('history-answer').textContent = preview.text || (preview.run.status === 'queued' ? '这个任务尚未启动。查看记录不会执行任务。' : '还没有回复，已保留执行记录。');
    renderTimeline($('history-timeline'),preview.timeline,preview.run.status);
    $('history-preview').scrollIntoView({block:'nearest'});
  } catch (error) {if (generation === previewGeneration) $('history-answer').textContent = error.message;}
}
$('history-close').onclick = () => {previewGeneration++; $('history-preview').hidden = true;};
$('history-refresh').onclick = async () => {try {await controller?.refreshHistory();} catch (error) {toast(error.message);}};
$('run-reconnect').onclick = () => controller?.retry();
async function send(task) {
  if (busy || pendingApproval || voiceState.active || !task.trim()) return;
  if (!account) {showAuth(); return;}
  voiceInput.cancel('sent');
  stopSpeech(); setBusy(true); $('message').value = ''; $('task-log').replaceChildren();
  setState({activity:'thinking',emotion:'focused',label:'接到了，让我想一想'});
  $('task-status').textContent = '正在准备这次对话…';
  message('user', task); const content = message('assistant', '');
  try {
    await ensureConversation();
    currentContent = content;
    await createController().submit({task,conversation_id:conversationId,mode:$('mode').value,include_memory:$('memory').checked,model:$('backend').value === 'api' ? $('model-select').value || null : null,backend:$('backend').value});
  } catch (error) {
    content.parentElement.classList.add('error'); content.textContent = error.message;
    setState({activity:'error',emotion:'concerned',label:'遇到一点问题，我们慢慢来'});
    $('task-status').textContent = error.message;
    if (error.status === 401) {account = null; connection('请重新登录', false); showAuth();}
  } finally {setBusy(Boolean(controller?.state.blocked && controller.state.phase !== 'waiting'));}
}
function showSpeaking(value) {
  if (value && voiceState.active) {stopSpeech(); return;}
  speaking = value; renderPresence();
}
function stopSpeech() {speechGeneration++; window.speechSynthesis?.cancel(); native('stopSpeech'); showSpeaking(false);}
function speak(text) {
  if (!$('voice').checked || voiceState.active) return;
  const clean = speechText(text); if (!clean) return;
  stopSpeech();
  if (desktop && window.webkit?.messageHandlers?.caibao) {native('speak', {text:clean,lang:'zh-CN'}); return;}
  if (!window.speechSynthesis) {toast('这个浏览器暂不支持朗读。'); return;}
  const generation = speechGeneration;
  const utterance = new SpeechSynthesisUtterance(clean); utterance.lang = 'zh-CN'; utterance.rate = 1.05;
  utterance.onstart = () => {if (generation === speechGeneration) showSpeaking(true);};
  utterance.onend = utterance.onerror = () => {if (generation === speechGeneration) showSpeaking(false);};
  window.speechSynthesis.speak(utterance);
}
async function interact(kind) {
  voiceInput.cancel('interaction');
  if (busy || pendingApproval) {toast('彩宝正在认真处理任务，等一下就好。'); return;}
  const local = {pet:'摸到啦，嘿嘿。',feed:'小鱼干！今天的快乐加一。',rest:'眯一会儿，叫我就好。',wake:'醒啦，继续陪你。'};
  if (!account) {setState({activity:kind === 'rest' ? 'sleeping' : 'idle',emotion:kind === 'rest' ? 'sleepy' : 'happy',label:local[kind]}); return;}
  try {
    const result = await api('/companion/events',{method:'POST',body:JSON.stringify({kind})});
    setState({...result.state,label:result.message});
  } catch (error) {toast(error.message);}
}
$('composer').addEventListener('submit', event => {event.preventDefault(); send($('message').value.trim());});
$('message').addEventListener('keydown', event => {if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); $('composer').requestSubmit();}});
$('messages').addEventListener('click', event => {const prompt = event.target.closest('[data-prompt]'); if (prompt) send(prompt.dataset.prompt);});
$('settings-toggle').onclick = () => {voiceInput.cancel('settings'); $('settings').hidden = !$('settings').hidden;};
for (const id of ['voice','memory','mode']) $(id).onchange = () => {voiceInput.cancel('settings'); savePreferences(); if (id === 'voice' && !$('voice').checked) stopSpeech();};
function renderModelStatus() {
  if ($('backend').value !== 'api') {
    const backend = backends.find(item => item.id === $('backend').value);
    connection(`${backend?.label || 'CLI'} · 使用 CLI 自身登录与工具`,true);
    $('model-label').textContent = `下次 · ${backend?.label || 'CLI'} · ${backend?.model || '由 CLI 自选'}`; return;
  }
  const selected = $('model-select').value;
  const demo = !selected && runtimeProfile?.runtime.demo_mode;
  connection(demo ? '演示模式 · 配置模型后即可真实对话' : `已连接 · ${account?.display_name || '彩宝'}`, true);
  $('model-label').textContent = `下次 · API · ${selected || (demo ? '演示模型' : runtimeProfile?.runtime.model || '默认模型')}`;
}
function updateBackend() {
  const selected = backends.find(item => item.id === $('backend').value);
  const cli = selected?.kind === 'cli';
  $('backend-note').textContent = selected?.reason || 'DeepSeek 等 API 模型在工作台中配置。';
  $('memory-setting-note').textContent = cli ? 'CLI 使用自己的会话；切换后端时，会按本轮记录补充对话。彩宝不会把记忆卡或背景文本注入 CLI。' : '包括记忆卡与可用的背景文本。关闭后，当前对话的聊天上下文仍然保留。';
  if (cli) $('mode').value = 'agent';
  $('mode').disabled = cli; $('memory').disabled = cli; $('model-select').disabled = cli;
  renderModelStatus();
}
$('backend').onchange = () => {
  voiceInput.cancel('backend');
  if (account) localStorage.setItem(`caibao.companion.backend.${account.user_id}`, $('backend').value);
  updateBackend();
};
$('model-select').onchange = () => {
  voiceInput.cancel('model');
  if (account) localStorage.setItem(`caibao.companion.model.${account.user_id}`, $('model-select').value);
  renderModelStatus();
};
function resetMemoryNotebook() {
  conversationPromise = null;
  memoryDraft = null; documentDraft = null; pendingMemoryDelete = null;
  memoryNotebook.dispose(); $('memory-notebook').hidden = true;
  $('memory-editor').hidden = true; $('memory-document-editor').hidden = true;
  $('memory-card-title').value = ''; $('memory-card-content').value = ''; $('memory-document-content').value = '';
}
async function openMemory(seed = {}) {
  voiceInput.cancel('memory');
  if (!account) {showAuth(); return;}
  const generation = identityGeneration;
  try {
    await ensureConversation(); if (generation !== identityGeneration) return;
    memoryNotebook.setContext(account.user_id,conversationId);
    $('memory-notebook').hidden = false;
    if (seed.text !== undefined) editMemoryCard({content:seed.text,source_message_id:seed.sourceId});
    await memoryNotebook.load();
  } catch (error) {if (generation === identityGeneration) toast(error.message);}
}
function notebookElement(tag, className, text) {
  const node = document.createElement(tag); if (className) node.className = className;
  if (text !== undefined) node.textContent = text; return node;
}
function notebookButton(label, action, disabled = false) {
  const button = notebookElement('button','',label); button.type = 'button'; button.disabled = disabled;
  button.onclick = action; return button;
}
function renderMemory(snapshot) {
  const blocked = snapshot.saving || snapshot.loading || Boolean(snapshot.uncertain || snapshot.conflict);
  $('memory-status').textContent = snapshot.loading ? '正在翻开你的小本…' : snapshot.saving ? '正在保存，请稍候…' : snapshot.error ||
    (snapshot.uncertain ? '结果待核对；不会自动重试。' : snapshot.conflict ? '请先比较最新背景与草稿。' : snapshot.notice || '查看和编辑小本，不需要调用生成模型。');
  $('memory-status').classList.toggle('has-error',Boolean(snapshot.error || snapshot.uncertain || snapshot.conflict));
  $('memory-unknown').hidden = !snapshot.uncertain;
  $('memory-unknown-reviewed').disabled = snapshot.loading || snapshot.saving || !snapshot.uncertain?.checked;
  $('memory-card-count').textContent = snapshot.items.length ? `· ${snapshot.items.length}${snapshot.hasMore ? '+' : ''}` : '';
  for (const id of ['memory-save','memory-document-save','memory-document-clear','memory-add']) $(id).disabled = blocked;
  if (documentDraft && !snapshot.documents.find(doc => doc.kind === documentDraft.kind)?.enabled) $('memory-document-save').disabled = true;
  $('memory-refresh').disabled = snapshot.loading || snapshot.saving;
  $('memory-more').hidden = !snapshot.hasMore; $('memory-more').disabled = snapshot.loadingMore || snapshot.loading || snapshot.saving;
  $('memory-more').textContent = snapshot.loadingMore ? '正在读取…' : '再看一些记忆';
  $('memory-cards').replaceChildren();
  if (!snapshot.items.length) $('memory-cards').append(notebookElement('p','notebook-empty',snapshot.loading ? '正在读取记忆…' : '小本还空着。把值得留住的事，亲手存下来吧。'));
  for (const item of snapshot.items) {
    const card = notebookElement('article','memory-card'); card.dataset.memoryId = item.memory_id;
    const heading = notebookElement('div','memory-card-heading');
    heading.append(notebookElement('h4','',item.title));
    const effective = item.effective_status || item.status;
    const stateLabel = {active:'使用中',disabled:'已暂停',expired:'已过期'}[effective] || '待核对';
    const badge = notebookElement('span','memory-card-badge',stateLabel); badge.dataset.status = effective; heading.append(badge);
    const scope = item.scope_level === 'global' || item.scope_level === 'personal' || !item.space_id ? '个人全局' : '当前空间';
    card.append(heading,notebookElement('p','memory-card-meta',`${scope} · ${item.category}`),notebookElement('p','memory-card-body',item.content));
    if (item.expires_at) {
      const date = new Date(item.expires_at); card.append(notebookElement('p','memory-card-meta',`有效期至 ${Number.isNaN(date.valueOf()) ? item.expires_at : date.toLocaleString('zh-CN')}`));
    }
    if (item.source_message_id || item.source) {
      const source = notebookElement('details','memory-source'); source.append(notebookElement('summary','',item.source?.available ? '查看来自哪次聊天' : '来源聊天目前不可用'));
      if (item.source?.available) {
        source.append(notebookElement('p','','你：' + (item.source.request_preview || '没有可显示的原文')));
        if (item.source.response_preview) source.append(notebookElement('p','','彩宝：' + item.source.response_preview));
      } else source.append(notebookElement('p','','记忆正文仍然保留；此处不会猜测或补造原文。'));
      card.append(source);
    } else card.append(notebookElement('p','memory-card-meta','手动保存 · 未关联聊天来源'));
    const actions = notebookElement('div','notebook-actions');
    actions.append(notebookButton('编辑',() => editMemoryCard(item),blocked));
    if (effective !== 'expired') actions.append(notebookButton(item.status === 'active' ? '暂停使用' : '重新启用',() => void memoryNotebook.setStatus(item,item.status === 'active' ? 'disabled' : 'active'),blocked));
    actions.append(notebookButton('删除',() => {pendingMemoryDelete = item.memory_id; renderMemory(memoryNotebook.state);},blocked));
    card.append(actions);
    if (pendingMemoryDelete === item.memory_id) {
      const confirm = notebookElement('div','memory-delete-review'); confirm.setAttribute('role','alert');
      confirm.append(notebookElement('p','','只删除这条卡片，聊天与其他背景仍然保留。'));
      confirm.append(notebookButton('确认删除这条记忆',async () => {
        const result = await memoryNotebook.remove(item);
        if (result.ok) {pendingMemoryDelete = null; if (memoryDraft?.memory_id === item.memory_id) closeMemoryEditor();}
      },blocked),notebookButton('保留',() => {pendingMemoryDelete = null; renderMemory(memoryNotebook.state);},snapshot.saving));
      card.append(confirm);
    }
    $('memory-cards').append(card);
  }
  $('memory-documents').replaceChildren();
  for (const doc of snapshot.documents) {
    const section = notebookElement('section','memory-document');
    const label = notebookElement('div','memory-document-heading'); label.append(notebookElement('h4','',doc.label),notebookButton('编辑',() => editMemoryDocument(doc),blocked || !doc.enabled));
    section.append(label,notebookElement('p','memory-card-meta',doc.enabled ? `回答时最多参考 ${doc.prompt_limit} 字符` : '尚未启用背景记忆，可查看内容；记忆卡仍可管理'));
    const preview = notebookElement('details','memory-document-preview'); preview.append(notebookElement('summary','','查看正文'),notebookElement('pre','',doc.content || '（空白）'));
    section.append(preview); $('memory-documents').append(section);
  }
  const conflict = snapshot.conflict && documentDraft?.kind === snapshot.conflict.kind;
  $('memory-conflict').hidden = !conflict;
  if (conflict) $('memory-document-latest').textContent = snapshot.documents.find(doc => doc.kind === documentDraft.kind)?.content ?? '最新正文未读取成功，请刷新后再核对。';
  $('memory-conflict-reviewed').disabled = snapshot.loading || snapshot.saving || !snapshot.conflict?.reviewReady;
}
function editMemoryCard(item = {}) {
  if (memoryNotebook.state.conflict) {toast('请先核对背景文本的更新，或取消那份草稿。'); return;}
  memoryDraft = {memory_id:item.memory_id,source_message_id:item.source_message_id};
  documentDraft = null; $('memory-document-editor').hidden = true;
  $('memory-editor-title').textContent = item.memory_id ? '重新写清这件事' : '留下一件事';
  $('memory-card-title').value = item.title || '';
  $('memory-card-content').value = item.content || '';
  $('memory-card-category').value = item.category || 'preference';
  $('memory-editor-source').textContent = item.source_message_id ? '会保留这次聊天的来源关联。请先检查正文，再明确保存。' : '这是一份待保存的草稿，尚未写入记忆。未关联聊天记录时只保存你确认的文字。';
  $('memory-editor').hidden = false; $('memory-editor').scrollIntoView({block:'nearest'}); $('memory-card-title').focus();
}
function closeMemoryEditor() {memoryDraft = null; $('memory-editor').hidden = true; $('memory-card-title').value = ''; $('memory-card-content').value = '';}
function editMemoryDocument(doc) {
  documentDraft = {kind:doc.kind,expected_revision:doc.revision}; closeMemoryEditor();
  $('memory-document-title').textContent = doc.label;
  $('memory-document-content').value = doc.content;
  $('memory-document-note').textContent = '可清空正文；点“保存背景文本”后才会写入。旧会话和 CLI 自身的上下文不会因此清空。';
  $('memory-document-editor').hidden = false; $('memory-document-editor').scrollIntoView({block:'nearest'});
  renderMemory(memoryNotebook.state); $('memory-document-content').focus();
}
$('memory-toggle').onclick = () => void openMemory();
$('memory-close').onclick = () => {$('memory-notebook').hidden = true; $('memory-toggle').focus();};
$('memory-add').onclick = () => editMemoryCard();
$('memory-refresh').onclick = () => void memoryNotebook.load();
$('memory-more').onclick = () => void memoryNotebook.load({append:true});
$('memory-edit-cancel').onclick = closeMemoryEditor;
$('memory-editor').onsubmit = async event => {
  event.preventDefault(); if (!memoryDraft) return;
  const generation = identityGeneration;
  const result = await memoryNotebook.saveCard({...memoryDraft,title:$('memory-card-title').value.trim(),content:$('memory-card-content').value.trim(),category:$('memory-card-category').value.trim()});
  if (generation === identityGeneration && result.ok) closeMemoryEditor();
};
$('memory-document-editor').onsubmit = async event => {
  event.preventDefault(); if (!documentDraft) return;
  const generation = identityGeneration;
  const result = await memoryNotebook.saveDocument({...documentDraft,content:$('memory-document-content').value});
  if (generation === identityGeneration && result.ok) {documentDraft = null; $('memory-document-editor').hidden = true;}
};
$('memory-document-clear').onclick = () => {$('memory-document-content').value = ''; $('memory-document-content').focus();};
$('memory-document-cancel').onclick = () => {documentDraft = null; $('memory-document-editor').hidden = true; if (memoryNotebook.state.conflict) memoryNotebook.discardConflict();};
$('memory-unknown-reviewed').onclick = () => memoryNotebook.reviewedUnknown();
$('memory-conflict-reviewed').onclick = () => {
  const latest = memoryNotebook.state.documents.find(doc => doc.kind === documentDraft?.kind);
  if (latest) {documentDraft.expected_revision = latest.revision; memoryNotebook.reviewedConflict();}
};
$('memory-notebook').addEventListener('keydown',event => {if (event.key === 'Escape') {$('memory-notebook').hidden = true; $('memory-toggle').focus();}});
$('mute').onclick = stopSpeech;
$('pet-action').onclick = () => interact('pet'); $('feed-action').onclick = () => interact('feed');
$('rest-action').onclick = () => interact(state.activity === 'sleeping' ? 'wake' : 'rest');
// Mouse drag is native; a click remains a pet interaction in both environments.
let pointerStart = null, dragged = false;
$('pet').addEventListener('pointerdown', event => {pointerStart = {x:event.screenX,y:event.screenY}; dragged = false;});
$('pet').addEventListener('pointermove', event => {
  if (desktop && pointerStart && !dragged && Math.hypot(event.screenX-pointerStart.x,event.screenY-pointerStart.y)>5) {dragged = true; native('drag');}
});
$('pet').addEventListener('pointerup', () => {pointerStart = null;});
$('pet').addEventListener('click', () => {if (!dragged) interact('pet');});
function applyCompact(value) {
  document.body.classList.toggle('compact', value);
  // Recovery can finish while compact mode has no chat layout. Once expanded,
  // wait for its viewport before restoring the latest reply's scroll position.
  if (!value) requestAnimationFrame(() => {
    if (!document.body.classList.contains('compact')) scrollMessages();
  });
}
function compact(value) {voiceInput.cancel('compact'); applyCompact(value); native('mode',{compact:value});}
$('native-hide').onclick = () => {voiceInput.cancel('hidden'); stopSpeech(); native('hide');};
$('native-quit').onclick = () => {voiceInput.cancel('quit'); stopSpeech(); native('quit');};
$('compact-open').onclick = () => compact(false); $('collapse').onclick = () => compact(true);
window.addEventListener('caibao-native-mode', event => {voiceInput.cancel('compact'); applyCompact(Boolean(event.detail.compact));});
window.addEventListener('caibao-native-ready', event => {
  window.caibaoNative = {...window.caibaoNative,...event.detail};
  document.body.classList.add('desktop'); applyCompact(Boolean(event.detail.compact));
});
window.addEventListener('caibao-native-speech', event => showSpeaking(event.detail.state === 'start'));
window.addEventListener('caibao-native-listen', event => voiceInput.handleNative(event.detail));
$('new-chat').onclick = () => {
  if (busy || pendingApproval) return;
  voiceInput.cancel('conversation'); $('message').value = '';
  identityGeneration++; resetMemoryNotebook();
  controller?.dispose(); controller = null; currentContent = null; runContents.clear(); previewGeneration++;
  $('history-preview').hidden = true; renderTaskHistory([]); $('run-strip').hidden = true;
  clearApproval(); conversationId = null; currentRun = null; stopSpeech();
  if (account) localStorage.removeItem(`caibao.companion.conversation.${account.user_id}`);
  $('messages').replaceChildren(); message('assistant','新的一页。今天想从哪里聊起？');
  setState({activity:'idle',emotion:'idle',label:'新的一页，也一起过'});
};
$('approve').onclick = async () => {
  if (!currentRun || busy) return;
  setBusy(true); $('approve').disabled = true; $('decline').disabled = true;
  try {
    const run = await api(`/agent/runs/${encodeURIComponent(currentRun)}/confirm`,{method:'POST',body:JSON.stringify({approval_token:approvalToken})});
    clearApproval(); await controller.accept(run);
  } catch (error) {toast(error.message); await controller?.retry();}
  finally {setBusy(Boolean(controller?.state.blocked && controller.state.phase !== 'waiting')); $('approve').disabled = !pendingApproval; $('decline').disabled = false;}

};
async function cancelTask() {
  if (!currentRun) return;
  $('stop-task').disabled = true; $('decline').disabled = true;
  try {
    const result = await api(`/agent/runs/${encodeURIComponent(currentRun)}/cancel`,{method:'POST'});
    stopSpeech(); await controller.accept(result);
    if (result.status !== 'cancelled') toast('任务已经结束，结果已保留。');
  } catch (error) {toast(error.message); await controller?.retry();} finally {$('stop-task').disabled = false; $('decline').disabled = false;}
}
$('stop-task').onclick = cancelTask;
$('decline').onclick = cancelTask;
$('auth-switch').onclick = () => {
  registering = !registering; $('display-name-label').hidden = !registering;
  $('auth-title').textContent = registering ? '很高兴认识你' : '和彩宝认识一下';
  $('auth-submit').textContent = registering ? '创建账号' : '登录';
  $('auth-switch').textContent = registering ? '已经认识了？登录' : '第一次见面？创建本地账号';
  $('auth-form').elements.password.autocomplete = registering ? 'new-password' : 'current-password';
  $('auth-error').textContent = '';
};
$('auth-later').onclick = () => $('auth-dialog').close();
$('auth-form').onsubmit = async event => {
  event.preventDefault(); $('auth-submit').disabled = true; $('auth-error').textContent = '';
  const form = new FormData(event.target), password = form.get('password');
  const body = {user_id:form.get('user_id').trim(),password};
  if (registering) Object.assign(body,{display_name:form.get('display_name').trim() || body.user_id,confirm_password:password});
  try {
    const user = await api(`/auth/${registering ? 'register' : 'login'}`,{method:'POST',body:JSON.stringify(body)});
    await signedIn(user); $('auth-dialog').close(); event.target.reset();
  } catch (error) {$('auth-error').textContent = error.message;} finally {$('auth-submit').disabled = false;}
};
$('account-button').onclick = async () => {
  if (busy || pendingApproval) return;
  voiceInput.cancel('account');
  try {await api('/auth/logout',{method:'POST'}); identityGeneration++; resetMemoryNotebook(); controller?.dispose(); controller = null; currentContent = null; runContents.clear(); previewGeneration++; $('history-preview').hidden = true; renderTaskHistory([]); $('run-strip').hidden = true; account = null; conversationId = null; currentRun = null; $('message').value = ''; stopSpeech(); clearApproval(); $('messages').replaceChildren(); $('account-button').hidden = true; connection('未登录 · 可以先摸摸彩宝',false); showAuth();}
  catch (error) {toast(error.message);}
};
function updateComposer() {
  $('send').disabled = busy || pendingApproval || voiceState.active;
  $('mic').disabled = busy || pendingApproval || voiceState.phase === 'processing'
    || (voiceState.phase === 'review' && !voiceState.inserted);
}
function renderVoice(next) {
  voiceState = next;
  $('voice-input').hidden = next.phase === 'idle'; $('voice-input').dataset.phase = next.phase;
  const labels = {consent:'浏览器语音输入需要你确认处理方式。',authorizing:'等待语音权限；随时可以取消。',
    listening:desktop?'正在设备端听你说，再点麦克风结束。':'正在听你说，再点麦克风结束。',
    processing:'已结束收音，正在整理文字…'};
  $('voice-status').textContent = next.message || labels[next.phase] || '';
  $('voice-consent').hidden = next.phase !== 'consent';
  $('voice-review').hidden = next.phase !== 'review' || next.inserted;
  $('voice-transcript').textContent = next.text || '';
  $('voice-insert').disabled = busy || pendingApproval || next.inserted;
  $('voice-cancel').textContent = next.phase === 'review' && !next.inserted ? '放弃这次语音' : next.active ? '取消语音' : '收起';
  const title = next.phase === 'listening' ? '结束语音输入'
    : ['consent','authorizing'].includes(next.phase) ? '取消语音输入' : '开始语音输入';
  $('mic').title = title; $('mic').setAttribute('aria-label',title);
  $('mic').setAttribute('aria-pressed',String(next.active)); $('mic').dataset.phase = next.phase;
  updateComposer(); renderPresence();
}
$('mic').onclick = () => voiceInput.toggle();
$('voice-continue').onclick = () => voiceInput.allowBrowser(voiceState.turnId);
$('voice-cancel').onclick = () => voiceInput.cancel();
$('voice-insert').onclick = () => voiceInput.insertTranscript();
$('voice-privacy').textContent = desktop ? '设备端识别 · 不上云 · 确认文字后发送' : '语音先形成草稿 · 浏览器处理方式见使用说明';
renderVoice(voiceInput.state);
document.addEventListener('click',event => {if (event.target.closest?.('a[href]')) voiceInput.cancel('navigation');},true);
document.addEventListener('visibilitychange',() => {if (document.hidden) voiceInput.cancel('hidden');});
document.addEventListener('keydown',event => {if (event.key === 'Escape' && voiceState.active) {voiceInput.cancel(); event.preventDefault();}});
window.addEventListener('pagehide',() => {voiceInput.cancel('pagehide'); stopSpeech(); controller?.dispose();});
window.addEventListener('pageshow',event => {if (event.persisted && controller) void controller.restore();});
window.addEventListener('online',() => {if (['unknown','reconnecting'].includes(controller?.state.phase)) void controller.retry();});
(async () => {
  try {let user; try {user = await api('/auth/me');} catch (error) {if (error.status !== 401) throw error; await api('/auth/refresh',{method:'POST'}); user = await api('/auth/me');} await signedIn(user);}
  catch (error) {connection(error.status === 401 ? '未登录 · 可以先摸摸彩宝' : '连接失败 · 请检查本地服务',false); if (error.status !== 401) toast(error.message);}
})();
