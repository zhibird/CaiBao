import {initialState, reduceEvent, createSSEParser, speechText} from './companion-state.mjs';

const $ = id => document.getElementById(id);
const desktop = new URLSearchParams(location.search).get('desktop') === '1';
const native = (action, extra = {}) => window.webkit?.messageHandlers?.caibao?.postMessage({action, ...extra});
let state = initialState(), account = null, conversationId = null, currentRun = null;
let runtimeProfile = null, backends = [];
let busy = false, pendingApproval = false, registering = false, recognition = null, toastTimer;
let speechGeneration = 0;
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
  $('habitat').dataset.activity = state.activity;
  $('habitat').dataset.emotion = state.emotion;
  const labels = {idle:'悠闲', happy:'开心', curious:'好奇', focused:'认真', sleepy:'困困', concerned:'担心'};
  $('mood-chip').textContent = labels[state.emotion] || '陪伴中';
  $('pet-status').querySelector('span').textContent = state.label;
  $('pet-bubble').textContent = state.label;
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
function message(role, text = '') {
  $('welcome')?.remove();
  const element = document.createElement('div'); element.className = `message ${role}`;
  const label = document.createElement('div'); label.className = 'speaker'; label.textContent = role === 'user' ? '你' : '彩宝';
  const content = document.createElement('div'); content.className = 'content'; content.textContent = text;
  element.append(label, content); $('messages').append(element); scrollMessages(); return content;
}
function scrollMessages() { $('messages').scrollTop = $('messages').scrollHeight; }
function connection(text, online) {
  $('connection-label').textContent = text; $('connection-dot').classList.toggle('offline', !online);
}
function showAuth() { if (!$('auth-dialog').open) $('auth-dialog').showModal(); }
async function signedIn(user) {
  const switched = account?.user_id !== user.user_id;
  if (switched) {
    $('messages').replaceChildren(); $('task-log').replaceChildren();
    clearApproval(); currentRun = null; stopSpeech();
    $('task-status').textContent = '没有任务的时候，发会儿呆也很好。';
  }
  account = user; $('account-button').hidden = false;
  conversationId = localStorage.getItem(`caibao.companion.conversation.${user.user_id}`);
  const profile = await api('/companion/profile'); runtimeProfile = profile;
  const models = await api('/llm/models');
  $('model-select').replaceChildren(new Option('默认模型', ''));
  for (const model of models.items) $('model-select').add(new Option(model.model_name,model.model_name));
  const preferredModel = localStorage.getItem(`caibao.companion.model.${user.user_id}`);
  if (models.items.some(model => model.model_name === preferredModel)) $('model-select').value = preferredModel;
  backends = await api('/companion/backends');
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
      const items = history.items.filter(item => item.channel === 'companion').reverse();
      for (const item of items) {message('user', item.request_text); message('assistant', item.response_text);}
      // Recover a waiting approval after reload without replaying the execution stream.
      const last = items.at(-1)?.response_payload;
      if (last?.status === 'requires_confirmation') {
        const live = await api(`/agent/runs/${encodeURIComponent(last.run_id)}`);
        if (live.status === 'requires_confirmation') showApproval(live);
      }
      if (!items.length) message('assistant',profile.greeting);
    } catch (error) {
      if (error.status === 404) {conversationId = null; localStorage.removeItem(`caibao.companion.conversation.${user.user_id}`);}
      else throw error;
    }
  }
}
async function ensureConversation() {
  if (!conversationId) {
    const item = await api('/conversations', {method:'POST',body:JSON.stringify({title:'与彩宝的小日常'})});
    conversationId = item.conversation_id;
    localStorage.setItem(`caibao.companion.conversation.${account.user_id}`, conversationId);
  }
}
function setBusy(value) {
  busy = value; $('send').disabled = value || pendingApproval; $('new-chat').disabled = value || pendingApproval;
  $('account-button').disabled = value || pendingApproval;
  $('backend').disabled = value;
  const cli = $('backend').value !== 'api';
  $('mode').disabled = value || cli; $('memory').disabled = value || cli; $('model-select').disabled = value || cli;
  $('stop-task').hidden = !value || cli;
}
function logTask(text) {
  const line = document.createElement('div'); line.textContent = text; $('task-log').append(line);
  $('task-log').scrollTop = $('task-log').scrollHeight;
}
function showApproval(run) {
  currentRun = run.run_id; pendingApproval = true; $('approval').hidden = false; $('send').disabled = true;
  $('approval-detail').textContent = (run.required_confirmations || []).map(call => `${call.tool_name}\n${JSON.stringify(call.arguments, null, 2)}`).join('\n\n') || '请到工作台查看操作详情后确认。';
  $('approve').disabled = !run.required_confirmations?.length; $('new-chat').disabled = true;
}
function clearApproval() { pendingApproval = false; $('approval').hidden = true; $('send').disabled = busy; $('new-chat').disabled = busy; }
async function consumeRun(start, content) {
  currentRun = start.run_id;
  // Never retry GET: opening a queued SSE endpoint starts real work.
  const response = await fetch(`/api/v1${start.stream_url}`, {credentials:'same-origin', headers:{Accept:'text/event-stream'}});
  if (!response.ok || !response.body) throw new Error('任务流未连接，请到工作台查看状态后再试。');
  let completed = false, streamError = null, finalAnswer = '';
  const parser = createSSEParser(event => {
    if (event.run_id !== currentRun) return;
    setState(reduceEvent(state, event));
    const payload = event.payload || {};
    if (event.event === 'llm.delta') {content.textContent += payload.text || ''; scrollMessages();}
    if (event.event === 'tool.started') {logTask(`进行中 · ${payload.tool_name}`); $('task-status').textContent = state.label;}
    if (event.event === 'tool.result') logTask(`已返回 · ${payload.tool_name}`);
    if (event.event === 'confirmation.required') {
      showApproval({run_id:currentRun, required_confirmations:[payload]});
      $('task-status').textContent = '等待你的确认，尚未执行。';
    }
    if (event.event === 'run.cancelled') {
      completed = true; finalAnswer = '';
      content.textContent ||= '任务已停止。已经开始的操作可能仍会结束，详情见任务记录。';
      clearApproval(); $('task-status').textContent = '已停止后续步骤。已经开始的操作可能仍会结束。';
    }
    if (event.event === 'run.in_progress') {completed = true; streamError = '这个任务已在另一个连接执行，请到工作台查看状态。';}
    if (event.event === 'run.failed') {completed = true; streamError = payload.error || '任务执行失败';}
    if (event.event === 'run.completed' && payload.status === 'cancelled') {
      completed = true; clearApproval(); $('task-status').textContent = '已停止后续步骤。'; return;
    }
    if (event.event === 'run.completed') {
      completed = true; finalAnswer = payload.answer || content.textContent;
      content.textContent = finalAnswer || '这次没有生成回复。'; scrollMessages();
      if (payload.status === 'requires_confirmation') showApproval(payload);
      else if (payload.status === 'failed') streamError = payload.answer || '任务执行失败';
      else {clearApproval(); $('task-status').textContent = '这一件事，完成了。';}
    }
  });
  const reader = response.body.getReader(), decoder = new TextDecoder();
  try { while (true) {const {done,value} = await reader.read(); if (done) break; parser.push(decoder.decode(value,{stream:true}));} parser.push(decoder.decode()); }
  finally {reader.releaseLock();}
  if (streamError) throw new Error(streamError);
  if (!completed) throw new Error('连接中断；任务可能仍在执行，请到工作台查看，避免重复操作。');
  if (finalAnswer && !pendingApproval) speak(finalAnswer);
}
async function send(task) {
  if (busy || pendingApproval || !task.trim()) return;
  if (!account) {showAuth(); return;}
  stopSpeech(); setBusy(true); $('message').value = ''; $('task-log').replaceChildren();
  setState({activity:'thinking',emotion:'focused',label:'接到了，让我想一想'});
  $('task-status').textContent = '正在准备这次对话…';
  message('user', task); const content = message('assistant', '');
  try {
    await ensureConversation();
    const start = await api('/companion/runs', {method:'POST', body:JSON.stringify({task,conversation_id:conversationId,mode:$('mode').value,include_memory:$('memory').checked,model:$('backend').value === 'api' ? $('model-select').value || null : null,backend:$('backend').value})});
    await consumeRun(start, content);
  } catch (error) {
    content.parentElement.classList.add('error'); content.textContent = error.message;
    setState({activity:'error',emotion:'concerned',label:'遇到一点问题，我们慢慢来'});
    $('task-status').textContent = error.message;
    if (error.status === 401) {account = null; connection('请重新登录', false); showAuth();}
  } finally {setBusy(false);}
}
function stopSpeech() {speechGeneration++; window.speechSynthesis?.cancel(); native('stopSpeech'); if (state.activity === 'speaking') setState({activity:'idle',label:'嗯，我在听'});}
function speak(text) {
  if (!$('voice').checked) return;
  const clean = speechText(text); if (!clean) return;
  if (desktop && window.webkit?.messageHandlers?.caibao) {native('speak', {text:clean,lang:'zh-CN'}); return;}
  if (!window.speechSynthesis) {toast('这个浏览器暂不支持朗读。'); return;}
  stopSpeech(); const generation = speechGeneration;
  const utterance = new SpeechSynthesisUtterance(clean); utterance.lang = 'zh-CN'; utterance.rate = 1.05;
  utterance.onstart = () => {if (generation === speechGeneration) setState({activity:'speaking',label:'把话说给你听'});};
  utterance.onend = utterance.onerror = () => {if (generation === speechGeneration && !busy) setState({activity:'idle',label:'说完啦，轮到你了'});};
  window.speechSynthesis.speak(utterance);
}
async function interact(kind) {
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
$('settings-toggle').onclick = () => { $('settings').hidden = !$('settings').hidden; };
for (const id of ['voice','memory','mode']) $(id).onchange = () => {savePreferences(); if (id === 'voice' && !$('voice').checked) stopSpeech();};
function renderModelStatus() {
  if ($('backend').value !== 'api') {
    const backend = backends.find(item => item.id === $('backend').value);
    connection(`${backend?.label || 'CLI'} · 使用 CLI 自身登录与工具`,true);
    $('model-label').textContent = backend?.model || backend?.label || 'CLI'; return;
  }
  const selected = $('model-select').value;
  const demo = !selected && runtimeProfile?.runtime.demo_mode;
  connection(demo ? '演示模式 · 配置模型后即可真实对话' : `已连接 · ${account?.display_name || '彩宝'}`, true);
  $('model-label').textContent = selected || (demo ? 'DEMO · MOCK MODEL' : runtimeProfile?.runtime.model || 'LOCAL COMPANION');
}
function updateBackend() {
  const selected = backends.find(item => item.id === $('backend').value);
  const cli = selected?.kind === 'cli';
  $('backend-note').textContent = selected?.reason || 'DeepSeek 等 API 模型在工作台中配置。';
  if (cli) $('mode').value = 'agent';
  $('mode').disabled = cli; $('memory').disabled = cli; $('model-select').disabled = cli;
  renderModelStatus();
}
$('backend').onchange = () => {
  if (account) localStorage.setItem(`caibao.companion.backend.${account.user_id}`, $('backend').value);
  updateBackend();
};
$('model-select').onchange = () => {
  if (account) localStorage.setItem(`caibao.companion.model.${account.user_id}`, $('model-select').value);
  renderModelStatus();
};
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
function compact(value) {document.body.classList.toggle('compact', value); native('mode',{compact:value});}
$('native-hide').onclick = () => {stopSpeech(); native('hide');};
$('native-quit').onclick = () => {stopSpeech(); native('quit');};
$('compact-open').onclick = () => compact(false); $('collapse').onclick = () => compact(true);
window.addEventListener('caibao-native-mode', event => document.body.classList.toggle('compact', Boolean(event.detail.compact)));
window.addEventListener('caibao-native-ready', event => {document.body.classList.add('desktop'); document.body.classList.toggle('compact', Boolean(event.detail.compact));});
window.addEventListener('caibao-native-speech', event => {if (event.detail.state === 'start') setState({activity:'speaking',label:'把话说给你听'}); else if (!busy) setState({activity:'idle',label:'嗯，我在听'});});
$('new-chat').onclick = () => {
  if (busy) return;
  clearApproval(); conversationId = null; currentRun = null; stopSpeech();
  if (account) localStorage.removeItem(`caibao.companion.conversation.${account.user_id}`);
  $('messages').replaceChildren(); message('assistant','新的一页。今天想从哪里聊起？');
  setState({activity:'idle',emotion:'idle',label:'新的一页，也一起过'});
};
$('approve').onclick = async () => {
  if (!currentRun || busy) return;
  setBusy(true); $('approve').disabled = true; $('decline').disabled = true;
  try {
    const run = await api(`/agent/runs/${encodeURIComponent(currentRun)}/confirm`,{method:'POST',body:'{}'});
    clearApproval(); message('assistant',run.answer || '操作已返回。');
    setState(reduceEvent(state,{event:'run.completed',payload:run}));
    if (run.status === 'requires_confirmation') showApproval(run);
    else if (run.status !== 'failed') speak(run.answer);
    $('task-status').textContent = run.status === 'failed' ? '执行遇到问题，详情见回复。' : '确认后的操作已返回。';
  } catch (error) {toast(error.message);} finally {setBusy(false); $('approve').disabled = false; $('decline').disabled = false;}
};
async function cancelTask() {
  if (!currentRun) return;
  $('stop-task').disabled = true; $('decline').disabled = true;
  try {
    const result = await api(`/agent/runs/${encodeURIComponent(currentRun)}/cancel`,{method:'POST'});
    if (result.status === 'cancelled') {
      clearApproval(); stopSpeech();
      setState({activity:'idle',emotion:'idle',label:'好，这次先停在这里'});
      $('task-status').textContent = '已停止后续步骤。已开始的工具操作可能仍会结束。';
    } else toast('任务已经结束，结果已保留。');
  } catch (error) {toast(error.message);} finally {$('stop-task').disabled = false; $('decline').disabled = false;}
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
  try {await api('/auth/logout',{method:'POST'}); account = null; conversationId = null; currentRun = null; stopSpeech(); clearApproval(); $('messages').replaceChildren(); $('account-button').hidden = true; connection('未登录 · 可以先摸摸彩宝',false); showAuth();}
  catch (error) {toast(error.message);}
};
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
$('mic').onclick = () => {
  if (desktop || !Recognition) {toast('可以使用 macOS 系统听写（在输入框中按听写快捷键）。'); $('message').focus(); return;}
  if (recognition) {recognition.stop(); return;}
  stopSpeech(); recognition = new Recognition(); recognition.lang = 'zh-CN'; recognition.interimResults = false;
  recognition.onstart = () => setState({activity:'listening',emotion:'curious',label:'你说，我在听'});
  recognition.onresult = event => {$('message').value = event.results[0][0].transcript; $('message').focus();};
  recognition.onerror = () => toast('没能听清。检查麦克风权限，或者打字告诉我。');
  recognition.onend = () => {recognition = null; if (!busy) setState({activity:'idle',label:'听到了，确认文字后再发送'});};
  try {recognition.start();} catch {recognition = null; toast('语音输入暂不可用。');}
};
window.addEventListener('pagehide',stopSpeech);
(async () => {
  try {let user; try {user = await api('/auth/me');} catch (error) {if (error.status !== 401) throw error; await api('/auth/refresh',{method:'POST'}); user = await api('/auth/me');} await signedIn(user);}
  catch (error) {connection(error.status === 401 ? '未登录 · 可以先摸摸彩宝' : '连接失败 · 请检查本地服务',false); if (error.status !== 401) toast(error.message);}
})();
