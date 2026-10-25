import test from 'node:test';
import assert from 'node:assert/strict';
import {receiptSummary} from '../app/web/companion-receipt.mjs';
import {CompanionRecovery,runStatusLabel} from '../app/web/companion-recovery.mjs';

const receipt = {
  version:1, source:'api_tool_loop', tool_budget:4, tool_calls_used:2,
  tool_calls_succeeded:2, tool_calls_failed:0, tool_calls_rejected:0, tool_calls_skipped:0,
  pending_tool_calls:0, omitted_tool_calls:0, stop_reason:'model_finished', incomplete:false,
};
const summary = (changes,run = {status:'completed'}) => receiptSummary({...run,execution_receipt:{...receipt,...changes}});

test('public receipt shows recorded tool budget and returned outcomes',()=>{
  assert.deepEqual(summary({}),{
    recorded:true, usage:'工具调用 2 / 4 次',
    outcomes:'返回成功 2 · 返回失败 0 · 拒绝 0 · 预演 0',
    pending:'结果未确认 0 · 未执行 0', ending:'模型已结束本轮回复。',
    note:'回执记录工具返回，外部结果请结合回复核对。', needsReview:false,
  });
});

test('all recorded categories remain visible without inferring operation success',()=>{
  const value = summary({tool_calls_used:9,tool_budget:10,tool_calls_succeeded:3,tool_calls_failed:2,
    tool_calls_rejected:1,tool_calls_skipped:2,pending_tool_calls:1,omitted_tool_calls:3,incomplete:true,stop_reason:'waiting_confirmation'},
    {status:'requires_confirmation'});
  assert.equal(value.usage,'工具调用 9 / 10 次');
  assert.equal(value.outcomes,'返回成功 3 · 返回失败 2 · 拒绝 1 · 预演 2');
  assert.equal(value.pending,'待确认 1 · 未执行 3');
  assert.ok(value.needsReview);
  for(const text of ['有工具返回失败','有调用被拒绝','有调用尚未执行','预演只返回计划','待确认的操作尚未执行','请核对回复及实际结果']) assert.ok(value.note.includes(text));
  assert.ok(!JSON.stringify(value).includes('任务已完成'));
});

test('exactly exhausting the budget alone does not imply failure',()=>{
  for(const stop_reason of ['model_finished','tool_budget']) {
    const value = summary({tool_budget:2,tool_calls_used:2,stop_reason});
    assert.equal(value.needsReview,false);
    assert.ok(!value.note.includes('失败'));
    assert.ok(!value.note.includes('尚未执行'));
  }
});

test('zero tools is a recorded loop rather than a missing receipt',()=>{
  const value = summary({tool_calls_used:0,tool_calls_succeeded:0});
  assert.equal(value.recorded,true);
  assert.equal(value.usage,'工具调用 0 / 4 次');
  assert.equal(value.needsReview,false);
});

test('budget stop explicitly says why the model stopped without calling task finished',()=>{
  const value = summary({stop_reason:'tool_budget',omitted_tool_calls:2,incomplete:true});
  assert.equal(value.ending,'已达到本轮工具调用上限。');
  assert.ok(value.note.includes('有调用尚未执行'));
  assert.ok(value.needsReview);
});

test('duplicate and iteration guards have distinct whitelisted explanations',()=>{
  assert.equal(summary({stop_reason:'duplicate_guard',incomplete:true}).ending,'重复调用保护已停止后续步骤。');
  assert.equal(summary({stop_reason:'iteration_limit',incomplete:true}).ending,'已达到本轮处理轮数上限。');
});

test('waiting confirmation never says pending actions already ran',()=>{
  const value = summary({stop_reason:'waiting_confirmation',tool_calls_used:4,pending_tool_calls:2,incomplete:true}, {status:'requires_confirmation'});
  assert.equal(value.ending,'正在等待确认，对应操作尚未执行。');
  assert.equal(value.pending,'待确认 2 · 未执行 0');
  assert.ok(value.note.includes('待确认的操作尚未执行'));
  assert.ok(!value.note.includes('有工具返回失败'));
});

test('cancelled receipt cautions that already started operations may still finish',()=>{
  const value = summary({stop_reason:'cancelled',incomplete:true});
  assert.equal(value.ending,'已停止后续步骤；已开始的操作可能仍会结束。');
  assert.ok(value.needsReview);
});

test('in-progress reservations say results are pending rather than operations never started',()=>{
  const value = summary({stop_reason:'in_progress',tool_calls_used:3,pending_tool_calls:1,incomplete:true}, {status:'running'});
  assert.equal(value.pending,'结果待返回 1 · 未执行 0');
  assert.equal(value.ending,'仍在处理中，工具结果可能还未返回。');
  assert.ok(value.note.includes('已预约的调用结果仍待返回'));
  assert.ok(!value.note.includes('尚未执行'));
  assert.ok(!value.pending.includes('待确认'));
});

test('cancelled and interrupted reservations never claim no operation ran',()=>{
  for(const status of ['cancelled','interrupted']) {
    const value = summary({stop_reason:status,tool_calls_used:3,pending_tool_calls:1,incomplete:true}, {status});
    assert.equal(value.pending,'结果未确认 1 · 未执行 0');
    assert.ok(value.note.includes('已开始的操作可能仍会结束'));
    assert.ok(value.note.includes('请核对实际结果'));
    assert.ok(!value.note.includes('尚未执行'));
    assert.ok(!value.pending.includes('待确认'));
  }
});

test('error and invalid-checkpoint receipts have explicit whitelisted explanations',()=>{
  for(const [stop_reason,ending] of [
    ['execution_error','本轮执行遇到问题；已开始的操作结果仍需核对。'],
    ['invalid_checkpoint','执行记录未通过校验，已停止继续处理。'],
  ]) {
    const value = summary({stop_reason,tool_calls_used:3,pending_tool_calls:1,incomplete:true}, {status:'failed'});
    assert.equal(value.ending,ending);
    assert.equal(value.pending,'结果未确认 1 · 未执行 0');
    assert.ok(value.note.includes('请核对实际结果'));
    assert.ok(!value.note.includes('尚未执行'));
  }
});

test('authoritative cancellation and interruption override a stale approval receipt',()=>{
  for(const status of ['cancelled','interrupted']) {
    const value = summary({stop_reason:'waiting_confirmation',tool_calls_used:3,pending_tool_calls:1,incomplete:true}, {status});
    assert.equal(value.pending,'结果未确认 1 · 未执行 0');
    assert.ok(!value.ending.includes('正在等待确认'));
    assert.ok(!value.note.includes('尚未执行'));
  }
  const failed = summary({stop_reason:'in_progress',tool_calls_used:3,pending_tool_calls:1,incomplete:true},{status:'failed'});
  assert.equal(failed.ending,'本轮执行遇到问题；已开始的操作结果仍需核对。');
  assert.equal(failed.pending,'结果未确认 1 · 未执行 0');
});

test('unknown pending state stays unconfirmed without inventing approval or execution state',()=>{
  const value = summary({stop_reason:'unknown',tool_calls_used:3,pending_tool_calls:1,incomplete:true},{});
  assert.equal(value.pending,'结果未确认 1 · 未执行 0');
  assert.ok(value.note.includes('请核对实际结果'));
  assert.ok(!value.note.includes('尚未执行'));
});

test('tool failure needs checking even when model finished normally',()=>{
  const value = summary({tool_calls_succeeded:1,tool_calls_failed:1,stop_reason:'model_finished',incomplete:true});
  assert.ok(value.needsReview);
  assert.equal(value.ending,'模型已结束本轮回复。');
  assert.ok(value.note.includes('有工具返回失败'));
  assert.ok(value.note.includes('请核对回复及实际结果'));
});

test('rejected and omitted calls each independently need checking',()=>{
  for(const field of ['tool_calls_rejected','omitted_tool_calls']) {
    const value = summary({[field]:1,...(field === 'tool_calls_rejected' ? {tool_calls_used:3} : {}),incomplete:true});
    assert.ok(value.needsReview);
    assert.ok(value.note.includes('请核对回复及实际结果'));
  }
});

test('dry run is explicitly a preview rather than performed external operation',()=>{
  const value = summary({tool_calls_succeeded:0,tool_calls_skipped:2});
  assert.ok(value.outcomes.includes('预演 2'));
  assert.ok(value.note.includes('预演只返回计划，未执行对应操作'));
});

test('incomplete is independent of numeric budget consumption',()=>{
  const value = summary({tool_calls_used:1,tool_calls_succeeded:1,incomplete:true});
  assert.ok(value.needsReview);
  assert.ok(value.note.includes('执行步骤尚未完整返回'));
  assert.ok(!value.ending.includes('上限'));
});

test('legacy, CLI and workflow runs with no receipt do not invent API limits',()=>{
  for(const run of [null,{}, {status:'completed',model:'legacy'},
    {execution_identity:{kind:'cli',backend:'codex'},mode:'cli',tool_budget:25},
    {mode:'workflow',steps:[{tool:'some_tool',status:'completed'}]},
    {request_payload:{max_tool_calls:123},preferences:{tool_budget:99}}]) {
    const value = receiptSummary(run);
    assert.equal(value.recorded,false);
    assert.equal(value.usage,'本次未记录 API 工具回执。');
    assert.equal(value.outcomes,'');
    assert.equal(value.ending,'');
    assert.equal(value.needsReview,false);
    assert.ok(!JSON.stringify(value).includes('25'));
    assert.ok(!JSON.stringify(value).includes('123'));
    assert.ok(!JSON.stringify(value).includes('99'));
  }
});

test('unknown source or version is not mistaken for a supported API receipt',()=>{
  for(const execution_receipt of [[], 'receipt', {...receipt,version:2}, {...receipt,source:'cli'}, {...receipt,source:'constructor'}]) {
    assert.equal(receiptSummary({execution_receipt}).recorded,false);
  }
});

test('missing any counter makes the record unavailable rather than assuming zero',()=>{
  for(const field of ['tool_budget','tool_calls_used','tool_calls_succeeded','tool_calls_failed','tool_calls_rejected','tool_calls_skipped','pending_tool_calls','omitted_tool_calls']) {
    const execution_receipt = {...receipt}; delete execution_receipt[field];
    assert.equal(receiptSummary({execution_receipt}).recorded,false,field);
  }
});

test('invalid numeric counters are never coerced into trusted observations',()=>{
  for(const invalid of ['2',-1,NaN,Infinity,1.5,Number.MAX_SAFE_INTEGER+1,null,{},true]) {
    assert.equal(summary({tool_calls_used:invalid}).recorded,false);
  }
});

test('unknown stop reasons remain undisclosed and cannot access inherited formatter properties',()=>{
  for(const stop_reason of ['private-raw-error','constructor','__proto__','toString',{},null,{toString:'bad'}]) {
    const value = summary({stop_reason});
    assert.equal(value.recorded,true);
    assert.equal(value.ending,'结束原因未记录。');
    assert.ok(!JSON.stringify(value).includes('private-raw-error'));
  }
});

test('private history, arguments, identifiers and raw errors never enter presentation',()=>{
  const run = {
    execution_receipt:{...receipt,private_history:'private transcript',tool_arguments:{api_key:'secret-key'},raw_error:'secret-error',
      pending_calls:[{arguments:{target:'/private/path'}}]},
    private_history:[{content:'do not display'}],steps:[{arguments:{password:'secret-password'}}],
    execution_identity:{kind:'api',endpoint:'https://user:secret@example.com'},
  };
  const displayed = JSON.stringify(receiptSummary(run));
  for(const hidden of ['private','secret','api_key','password','example.com','do not display']) assert.ok(!displayed.includes(hidden));
});

test('recovered and inspected snapshots with identical receipt produce identical presentation',()=>{
  const live = {run_id:'live',status:'completed',execution_receipt:{...receipt,tool_calls_used:3,tool_calls_failed:1,incomplete:true}};
  const restored = {...live,status:'completed'};
  const history = JSON.parse(JSON.stringify(restored));
  assert.deepEqual(receiptSummary(live),receiptSummary(restored));
  assert.deepEqual(receiptSummary(restored),receiptSummary(history));
});

test('recovery and history inspection preserve the authoritative run receipt over stale journal evidence',async()=>{
  const latest = {run_id:'r1',conversation_id:'c1',trigger_channel:'companion',task:'请核对工具结果',status:'completed',steps:[],answer:'查看结果',
    execution_receipt:{...receipt,tool_budget:7,tool_calls_used:3,tool_calls_succeeded:2,tool_calls_failed:1,incomplete:true}};
  const stale = {...receipt,tool_budget:99,tool_calls_used:1,tool_calls_succeeded:1};
  const values = new Map([['caibao.companion.run.u1.c1',JSON.stringify({run_id:'r1',task:latest.task})]]);
  const seen = [];
  const recovery = new CompanionRecovery({
    userId:'u1',conversationId:'c1',
    storage:{getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
    onChange:snapshot=>seen.push(snapshot),
    fetchStream:()=>assert.fail('terminal history must not open an executing stream'),
    request:async(path,options)=>{
      assert.ok(!options?.method || options.method === 'GET','recovery must not execute a new task');
      if(path.startsWith('/agent/runs?')) return {items:[latest]};
      if(path === '/agent/runs/r1') return latest;
      if(path.startsWith('/agent/runs/r1/events?')) return {items:[{seq:1,run_id:'r1',event:'run.completed',payload:{status:'completed',execution_receipt:stale}}],has_more:false};
      assert.fail(path);
    },
  });
  await recovery.restore();
  const inspected = await recovery.inspect('r1');
  const current = receiptSummary(recovery.state.run), history = receiptSummary(inspected.run);
  assert.equal(recovery.state.phase,'finished');
  assert.ok(seen.some(snapshot=>snapshot.phase === 'finished'));
  assert.equal(current.usage,'工具调用 3 / 7 次');
  assert.ok(current.needsReview);
  assert.deepEqual(current,history);
  assert.equal(runStatusLabel('completed'),'已返回');
  assert.equal(runStatusLabel('partial_success'),'需核对');
  recovery.dispose();
});
