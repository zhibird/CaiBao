import test from 'node:test';
import assert from 'node:assert/strict';
import {executionSummary} from '../app/web/companion-execution.mjs';

const api = {kind:'api',backend:'api',model:'deepseek-flash',model_source:'resolved'};
const cli = {kind:'cli',backend:'codex',model:null,model_source:'runtime_default'};
const context = {version:1,source:'caibao.chat_history',history_enabled:true,selected_count:2,omitted_count:0,truncated_count:0};

test('a run displays its recorded model even when future preferences differ',()=>{
  const run = {execution_identity:api,context_snapshot:{...context,continuity:'api_history'},model:'old-request-alias'};
  const summary = executionSummary(run,{backend:'claude_code',model:'future-model'});
  assert.equal(summary.identity,'API · deepseek-flash');
  assert.equal(summary.context,'已装入这次对话 · 补充 2 轮聊天');
  assert.ok(!JSON.stringify(summary).includes('future-model'));
  assert.ok(!JSON.stringify(summary).includes('old-request-alias'));
});

test('CLI runtime defaults remain unknown instead of falling back to API defaults',()=>{
  const summary = executionSummary({execution_identity:{...cli,model:'stale-api-default'},model:'api-default'});
  assert.equal(summary.identity,'Codex · 由 CLI 自选');
  assert.ok(!JSON.stringify(summary).includes('default'));
  for(const backend of ['codex','claude_code','pi']) {
    const configured = executionSummary({execution_identity:{...cli,backend,model:'actual-cli-model',model_source:'configured'}});
    assert.match(configured.identity,/actual-cli-model/);
  }
});

test('legacy and queued runs never infer actual execution from old model or request fields',()=>{
  for(const run of [null,{}, {status:'queued',model:'requested'}, {model:'legacy',request_payload:{backend:'pi',model:'guess'}}, {execution_identity:{kind:'unrecognized',model:'guess'}}]) {
    assert.deepEqual(executionSummary(run),{identity:'尚无运行记录',context:'尚无对话衔接记录',recorded:false});
  }
});

test('native continuation distinguishes existing context from newly supplied history',()=>{
  for (const native_checkpoint of [false, true, undefined]) {
    const summary = executionSummary({execution_identity:cli,context_snapshot:{...context,continuity:'native_resume',native_checkpoint,selected_count:0}});
    assert.equal(summary.context,'接回原 CLI 会话 · 本轮没有新增聊天需要补充');
  }
  const supplemented = executionSummary({execution_identity:cli,context_snapshot:{...context,continuity:'native_resume',omitted_count:5,truncated_count:1}});
  assert.equal(supplemented.context,'接回原 CLI 会话 · 补充 2 轮聊天 · 略去 5 轮 · 1 轮过长内容已截短');
});

test('fresh sessions describe reconstruction and its actual reset reason',()=>{
  for(const [reset_reason,label] of [['new','已重新装入对话'],['binding_changed','执行设置变了'],['legacy','旧会话缺少衔接记录'],['uncertain','上次衔接未确认']]) {
    const summary = executionSummary({execution_identity:cli,context_snapshot:{...context,continuity:'fresh',reset_reason}});
    assert.ok(summary.context.startsWith(label));
    assert.ok(!summary.context.includes('接回原 CLI 会话'));
  }
});

test('disabled conversation history does not claim omitted messages were supplied',()=>{
  const summary = executionSummary({execution_identity:api,context_snapshot:{...context,continuity:'api_history',history_enabled:false,selected_count:7}});
  assert.equal(summary.context,'本轮未补充聊天记录');
  assert.ok(!summary.context.includes('7'));
});

test('unrecognized context sources and incompatible API markers do not claim continuity',()=>{
  for(const snapshot of [{...context,source:'untrusted',continuity:'fresh'}, {...context,version:99,continuity:'fresh'}, {...context,continuity:'unknown'}]) {
    assert.equal(executionSummary({execution_identity:cli,context_snapshot:snapshot}).context,'尚无对话衔接记录');
  }
  assert.equal(executionSummary({execution_identity:api,context_snapshot:{...context,continuity:'native_resume'}}).context,'尚无对话衔接记录');
});

test('private identifiers and endpoint credentials are never included in presentation',()=>{
  const summary = executionSummary({
    execution_identity:{...cli,binding:'private-fingerprint',endpoint:'https://user:secret@example.com?api_key=private-key',session_id:'private-session',workdir:'/private/workspace'},
    context_snapshot:{...context,continuity:'fresh',reset_reason:'private-unknown-reason',selected_message_ids:['private-message-id'],session_id:'private-session'},
  });
  const displayed = JSON.stringify(summary);
  for(const privateValue of ['private-','secret','user:','workspace','example.com']) assert.ok(!displayed.includes(privateValue));
});

test('malformed counters and missing models remain readable without guessing',()=>{
  const summary = executionSummary({execution_identity:{kind:'api',backend:'api',model:{}},context_snapshot:{...context,continuity:'api_history',selected_count:'9',omitted_count:-1,truncated_count:Infinity}});
  assert.equal(summary.identity,'API · 模型未记录');
  assert.equal(summary.context,'已装入这次对话 · 本轮没有新增聊天需要补充');
});

test('unrecognized names remain data and cannot access inherited formatter properties',()=>{
  for(const value of ['constructor','toString','__proto__']) {
    const summary = executionSummary({execution_identity:{...cli,backend:value},context_snapshot:{...context,continuity:'fresh',reset_reason:value}});
    assert.equal(summary.identity,'CLI · 由 CLI 自选');
    assert.equal(summary.context,'已重新装入对话 · 补充 2 轮聊天');
  }
});
