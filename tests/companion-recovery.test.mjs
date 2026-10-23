import test from 'node:test';
import assert from 'node:assert/strict';
import {CompanionRecovery} from '../app/web/companion-recovery.mjs';

const run = (status = 'running', extra = {}) => ({run_id:'r1', conversation_id:'c1', trigger_channel:'companion', task:'帮我整理资料', status, answer:'', steps:[], ...extra});
const event = (seq, type, payload = {}, extra = {}) => ({seq, run_id:'r1', event:type, payload, ...extra});
function memory() {const values = new Map(); return {getItem:key => values.get(key) ?? null, setItem:(key,value) => values.set(key,value), removeItem:key => values.delete(key)};}
function streamed(events, {fail = false, onEnd = () => {}} = {}) {
  // Real byte streams split both Chinese UTF-8 and SSE frames. The transport
  // failure arrives after delivered chunks, as it does on a dropped connection.
  const bytes = new TextEncoder().encode(events.map(item => `id: ${item.seq}\r\nevent: ${item.event}\r\ndata: ${JSON.stringify(item)}\r\n\r\n`).join(''));
  let offset = 0;
  return new Response(new ReadableStream({pull(controller) {
    if (offset < bytes.length) {controller.enqueue(bytes.slice(offset, offset + 7)); offset += 7;}
    else {onEnd(); if (fail) controller.error(new TypeError('network disconnected')); else controller.close();}
  }}), {headers:{'Content-Type':'text/event-stream'}});
}
function harness(handler, options = {}) {
  const calls = [], states = [], spoken = [], history = [];
  const fakeFetch = async (url, init = {}) => {
    calls.push({url, method:init.method || 'GET', ...init});
    const result = await handler(url.replace('/api/v1',''),init);
    return result instanceof Response ? result : new Response(JSON.stringify(result),{headers:{'Content-Type':'application/json'}});
  };
  const request = async (path, init) => {
    const response = await fakeFetch(`/api/v1${path}`,init);
    if (!response.ok) {const error = new Error((await response.json()).detail); error.status = response.status; throw error;}
    return response.json();
  };
  const controller = new CompanionRecovery({request, fetchStream:fakeFetch, storage:memory(), userId:'u1', conversationId:'c1', retryDelays:[0,0], wait:async () => {}, requestId:() => 'request-1', onChange:value => states.push(value), onHistory:items => history.push(items), onTerminal:value => spoken.push(value.text), ...options});
  return {controller,calls,states,spoken,history};
}
const journal = (items, status = 'running', has_more = false) => ({items, status, has_more, next_seq:items.at(-1)?.seq || 0});

test('disconnect reconnects with durable cursor and deduplicates journal deltas and tools',async () => {
  let opened = 0, finished = false;
  const first = [event(1,'run.started'), event(4,'llm.delta',{text:'彩宝'}), event(6,'tool.started',{tool_name:'search'},{step_index:1})];
  const second = [event(8,'llm.delta',{text:'记得'}), event(9,'tool.result',{tool_name:'search',result:{}},{step_index:1})];
  const completed = event(12,'run.completed',{status:'completed',answer:'彩宝记得啦'});
  const {controller,calls,states,spoken} = harness(path => {
    if (path.startsWith('/agent/runs?')) return {items:[run(finished?'completed':'running')]};
    if (path === '/agent/runs/r1') return run(finished?'completed':'running',finished?{answer:'彩宝记得啦'}:{});
    if (path.includes('/events?')) {
      const after = Number(new URL(path,'http://local').searchParams.get('after'));
      if (!after) return journal([]);
      // Repeated boundary event must not append its text or tool twice.
      if (after === 6) return journal([first[1],...second]);
      return journal([]);
    }
    if (path.includes('/stream?')) {
      opened++;
      if (opened === 1) return streamed(first,{fail:true});
      return streamed([second[0],completed],{onEnd:() => {finished=true;}});
    }
    throw new Error(`Unexpected ${path}`);
  });
  await controller.follow(run(),{admitted:true,recovered:false});
  assert.equal(controller.state.text,'彩宝记得啦');
  assert.equal(controller.state.cursor,12);
  assert.equal(controller.state.phase,'finished'); assert.equal(controller.state.blocked,false);
  assert.deepEqual(controller.state.timeline.map(item => [item.tool,item.status]),[['search','completed']]);
  assert.ok(states.some(state => state.phase === 'reconnecting'));
  const streams = calls.filter(call => call.url.includes('/stream?'));
  assert.equal(streams.length,2); assert.ok(streams[1].url.endsWith('after=9'));
  assert.equal(streams[1].headers['Last-Event-ID'],'9');
  assert.deepEqual(spoken,[],'recovery must not read the old answer out loud');
  assert.ok(calls.every(call => call.method === 'GET'),'transport retries must never repeat task submission');
});

test('reload discovers active task and recovers partial text before subscribing, without a local marker',async () => {
  let finished = false, partialSeen = false;
  const {controller,calls,spoken} = harness(path => {
    if (path.startsWith('/agent/runs?')) return {items:[run(finished?'completed':'running')]};
    if (path === '/agent/runs/r1') return run(finished?'completed':'running',finished?{answer:'前半段后半段'}:{});
    if (path.includes('/events?after=0')) return journal([event(2,'run.started'),event(3,'llm.delta',{text:'前半段'}),event(5,'tool.started',{tool_name:'read_file'},{step_index:1})]);
    if (path.includes('/events?')) return journal([]);
    if (path.includes('/stream?')) {
      partialSeen = controller.state.text === '前半段';
      return streamed([event(8,'llm.delta',{text:'后半段'}),event(10,'run.completed',{status:'completed',answer:'前半段后半段'})],{onEnd:() => {finished=true;}});
    }
    throw new Error(path);
  });
  await controller.restore();
  assert.equal(partialSeen,true); assert.equal(controller.state.text,'前半段后半段');
  assert.ok(calls.find(call => call.url.endsWith('/stream?after=5')));
  assert.equal(controller.state.timeline[0].tool,'read_file'); assert.deepEqual(spoken,[]);
});

test('EOF inspects the live run and completes from durable answer even with a missing terminal frame',async () => {
  let ended = false;
  const {controller,spoken} = harness(path => {
    if (path.startsWith('/agent/runs?')) return {items:[]};
    if (path === '/agent/runs/r1') return run(ended?'completed':'running',ended?{answer:'最终答复'}:{});
    if (path.includes('/events?')) return journal([]);
    if (path.includes('/stream?')) return streamed([event(1,'llm.delta',{text:'最终'})],{onEnd:() => {ended=true;}});
    throw new Error(path);
  });
  await controller.follow(run(),{admitted:true,recovered:false});
  assert.equal(controller.state.phase,'finished'); assert.equal(controller.state.text,'最终答复');
  assert.deepEqual(spoken,['最终答复']);
});

test('connection retry budget ends in an unresolved task and blocks a duplicate submit',async () => {
  const {controller,calls} = harness(path => {
    if (path === '/agent/runs/r1') return run();
    if (path.includes('/events?')) return journal([]);
    if (path.includes('/stream?')) throw new TypeError('offline');
    if (path.startsWith('/agent/runs?')) return {items:[run()]};
    throw new Error(path);
  });
  await controller.follow(run(),{admitted:true,recovered:false});
  assert.equal(controller.state.phase,'unknown'); assert.equal(controller.state.blocked,true);
  assert.equal(calls.filter(call => call.url.includes('/stream?')).length,3);
  await assert.rejects(() => controller.submit({task:'重复整理'}),/上个任务/);
  assert.ok(calls.every(call => call.method === 'GET'));
});

test('lost create response keeps immutable request key across a reload and safe admission retry',async () => {
  const storage = memory(); let createCount = 0, attempts = 0; const submitted = [];
  const handler = (path,init) => {
    if (path === '/companion/runs') {
      submitted.push(JSON.parse(init.body)); attempts++;
      if (attempts === 1) {createCount++; throw new TypeError('response lost after admission');}
      return {run_id:'r1',status:'queued'};
    }
    // A list may lag admission. Retrying the key is still safe.
    if (path.startsWith('/agent/runs?')) return {items:[]};
    if (path === '/agent/runs/r1') return run('completed',{answer:'已经执行过了',client_request_id:'request-1'});
    if (path.includes('/events?')) return journal([],'completed');
    throw new Error(path);
  };
  const original = harness(handler,{storage}).controller;
  await original.submit({task:'帮我整理资料',backend:'api',mode:'agent'});
  assert.equal(original.state.phase,'unknown'); assert.ok(original.intent.submission);
  original.dispose();
  const {controller,calls,spoken} = harness(handler,{storage});
  await controller.restore();
  assert.equal(controller.state.phase,'finished'); assert.equal(controller.state.text,'已经执行过了');
  assert.equal(createCount,1); assert.equal(attempts,2); assert.deepEqual(submitted[0],submitted[1]);
  assert.equal(submitted[1].client_request_id,'request-1'); assert.deepEqual(spoken,[]);
  assert.equal(calls.filter(call => call.url.includes('/stream?')).length,0);
});

test('historical queued selection is read-only and cannot start work or replace the current task',async () => {
  const {controller,calls} = harness(path => {
    if (path === '/agent/runs/old') return run('queued',{run_id:'old',task:'昨天排队的事'});
    if (path.startsWith('/agent/runs/old/events?')) return journal([],'queued');
    throw new Error(path);
  });
  controller.state.run = run(); controller.state.phase = 'running'; controller.state.blocked=true;
  const preview = await controller.inspect('old');
  assert.equal(preview.run.status,'queued'); assert.equal(preview.task,'昨天排队的事');
  assert.equal(controller.state.run.run_id,'r1'); assert.equal(controller.state.phase,'running');
  assert.ok(calls.every(call => call.method === 'GET' && !call.url.includes('/stream')));
});

test('discovered queued task is retained for review but never automatically launched',async () => {
  const {controller,calls} = harness(path => {
    if (path.startsWith('/agent/runs?')) return {items:[run('queued')]};
    if (path === '/agent/runs/r1') return run('queued');
    if (path.includes('/events?')) return journal([],'queued');
    throw new Error(path);
  });
  await controller.restore(); assert.equal(controller.state.phase,'unknown'); assert.equal(controller.state.blocked,true);
  await controller.retry();
  assert.ok(calls.every(call => !call.url.includes('/stream')));
});

test('paginated replay reconstructs tools and exposes current approval token only after full run read',async () => {
  const full = run('requires_confirmation',{answer:'请确认',approval_token:'fresh-token',required_confirmations:[{tool_name:'write_file',arguments:{path:'note.txt'}}],steps:[{step_id:'s1',step_index:1,step_type:'tool_call',tool_name:'read_file',status:'completed'}]});
  const {controller,states,spoken,calls} = harness(path => {
    if (path.startsWith('/agent/runs?')) return {items:[full]};
    if (path === '/agent/runs/r1') return full;
    if (path.includes('/events?after=0')) return journal([event(2,'tool.started',{tool_name:'read_file'},{step_index:1})],'requires_confirmation',true);
    if (path.includes('/events?after=2')) return journal([event(5,'tool.result',{tool_name:'read_file',result:{}},{step_index:1}),event(8,'confirmation.required',{tool_name:'write_file',arguments:{path:'note.txt'},approval_token:'old-token'})],'requires_confirmation');
    throw new Error(path);
  });
  await controller.restore();
  assert.equal(controller.state.phase,'waiting'); assert.equal(controller.state.blocked,true);
  assert.equal(controller.state.run.approval_token,'fresh-token');
  assert.ok(states.filter(state => state.phase==='waiting').every(state => state.run.approval_token==='fresh-token'));
  assert.deepEqual(controller.state.timeline.map(item=>item.status),['completed']);
  assert.deepEqual(spoken,[]); assert.ok(calls.every(call=>!call.url.includes('/stream')));
});

for (const status of ['failed','cancelled','partial_success','interrupted']) {
  test(`${status} recovery preserves the outcome and never speaks or repeats execution`,async()=>{
    const {controller,calls,spoken} = harness(path=>{
      if (path.startsWith('/agent/runs?')) return {items:[]};
      if (path==='/agent/runs/r1') return run(status,{answer:`保留结果 ${status}`});
      if (path.includes('/events?')) return journal([event(1,'llm.delta',{text:'部分文字'})],status);
      throw new Error(path);
    });
    await controller.follow(run(status),{recovered:true,admitted:true});
    assert.equal(controller.state.run.status,status); assert.equal(controller.state.phase,'finished'); assert.equal(controller.state.blocked,false);
    assert.equal(controller.state.text,`保留结果 ${status}`); assert.deepEqual(spoken,[]);
    assert.ok(calls.every(call=>!call.url.includes('/stream')));
  });
}

test('idle stream is cancelled and checked rather than leaving the task permanently busy',async () => {
  let streamCancelled = false, reads = 0;
  const {controller,states} = harness(path => {
    if (path === '/agent/runs/r1') {reads++; return run(streamCancelled?'completed':'running',{answer:streamCancelled?'后台已完成':''});}
    if (path.includes('/events?')) return journal([]);
    if (path.startsWith('/agent/runs?')) return {items:[]};
    if (path.includes('/stream?')) return new Response(new ReadableStream({cancel() {streamCancelled=true;}}));
    throw new Error(path);
  },{streamIdleMs:5});
  await controller.follow(run(),{admitted:true,recovered:false});
  assert.equal(streamCancelled,true); assert.ok(reads>=2);
  assert.ok(states.some(state=>state.phase==='reconnecting'));
  assert.equal(controller.state.text,'后台已完成'); assert.equal(controller.state.phase,'finished');
});

test('disposing an observer suppresses delayed admission and history callbacks from the old account',async () => {
  let resolveAdmission,resolveHistory;
  const {controller,states,history,calls} = harness(path => {
    if (path==='/companion/runs') return new Promise(resolve=>{resolveAdmission=resolve;});
    if (path.startsWith('/agent/runs?')) return new Promise(resolve=>{resolveHistory=resolve;});
    throw new Error(path);
  });
  const submitted=controller.submit({task:'等待返回'}), refreshing=controller.refreshHistory();
  const changes=states.length; controller.dispose();
  resolveAdmission({run_id:'r1',status:'queued'});resolveHistory({items:[run()]});
  await Promise.all([submitted,refreshing]);
  assert.equal(states.length,changes); assert.equal(history.length,0); assert.equal(calls.length,2);
});
