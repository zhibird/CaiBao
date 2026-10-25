import test from 'node:test';
import assert from 'node:assert/strict';
import {parseTaskPlan,taskPlanSummary,TaskPlanTracker} from '../app/web/companion-plan.mjs';
import {CompanionRecovery} from '../app/web/companion-recovery.mjs';

const step = (id,status='pending',content=`处理步骤 ${id}`)=>({id,content,status});
const plan = (changes={})=>({version:1,revision:1,source:'api.update_plan',model_reported:true,
  steps:[step('one','in_progress'),step('two')],explanation:null,limited:false,...changes});
const run = (changes={})=>({run_id:'r1',status:'running',conversation_id:'c1',trigger_channel:'companion',task:'整理资料',steps:[],task_plan:plan(),...changes});
const event = (seq,value,run_id='r1')=>({seq,run_id,event:'plan.updated',payload:{task_plan:value}});
const observer = ()=>new CompanionRecovery({request:async()=>({items:[]}),userId:'u1',conversationId:'c1'});
const target = (changes={})=>({run:run(changes),cursor:0,text:'existing reply',timeline:[],lastEvent:{event:'llm.delta',payload:{text:'existing reply'}}});

test('all four public sources receive friendly labels and explicit model attribution',()=>{
  for(const [source,label] of [['api.update_plan','API 模型'],['codex.todo_list','Codex'],['claude.TodoWrite','Claude Code'],['claude.tasks','Claude Code']]) {
    const value=taskPlanSummary(run({task_plan:plan({source,steps:[step('1','completed')]})}));
    assert.equal(value.source,label);assert.equal(value.progress,'模型标记完成 1 / 1 项');
    assert.equal(value.steps[0].label,'模型标记完成');assert.match(value.note,/模型报告/);
  }
});
test('missing legacy plan stays absent without guessing from tools or preferences',()=>{
  for(const value of [null,{},run({task_plan:null}),run({task_plan:undefined,steps:[{tool_name:'update_plan'}],preferences:{plan:['guessed']}})]) assert.equal(taskPlanSummary(value),null);
});
test('empty list means model cleared its checklist rather than successful completion',()=>{
  const value=taskPlanSummary(run({task_plan:plan({steps:[]})}));
  assert.equal(value.progress,'模型已清空计划列表');assert.deepEqual(value.steps,[]);assert.doesNotMatch(value.progress,/完成/);
});
test('limited plan retains the reported steps and states that updates stopped',()=>{
  const value=taskPlanSummary(run({task_plan:plan({revision:65,limited:true})}));
  assert.equal(value.steps.length,2);assert.match(value.note,/停止更新.*保留/);assert.equal(value.limited,true);
});
test('text remains literal data, including markup, without exposing unrecognized fields',()=>{
  const content='<img src=x onerror=alert(1)>',explanation='请把 <script> 当文字';
  const value=taskPlanSummary(run({task_plan:plan({steps:[step('x','pending',content)],explanation})}));
  assert.equal(value.steps[0].content,content);assert.equal(value.explanation,explanation);
  assert.equal(parseTaskPlan(plan({private_history:'secret'})),null);
  assert.equal(parseTaskPlan(plan({steps:[{...step('x'),arguments:{password:'secret'}}]})),null);
});
test('strict validation rejects unknown, inherited, malformed and incomplete public fields',()=>{
  for(const value of [[],null,'plan',plan({version:true}),plan({version:2}),plan({model_reported:1}),plan({model_reported:false}),
    plan({source:'constructor'}),plan({source:'__proto__'}),plan({source:{}}),plan({limited:'true'}),plan({explanation:{}}),plan({steps:{}})]) assert.equal(parseTaskPlan(value),null);
  for(const field of ['version','revision','source','model_reported','steps','explanation','limited']) {const value=plan();delete value[field];assert.equal(parseTaskPlan(value),null,field);}
  const inherited=Object.create(plan());assert.equal(parseTaskPlan(inherited),null);
});
test('revision and list bounds are exact',()=>{
  for(const revision of [0,66,-1,1.5,'1',Infinity,NaN]) assert.equal(parseTaskPlan(plan({revision})),null);
  assert.ok(parseTaskPlan(plan({revision:65,limited:true,steps:Array.from({length:10},(_,i)=>step(String(i)))})));
  assert.equal(parseTaskPlan(plan({steps:Array.from({length:11},(_,i)=>step(String(i)))})),null);
});
test('step IDs are unique and fields/statuses are strict',()=>{
  for(const steps of [[step('x'),step('x')],[step('x','unknown')],[step('x','constructor')],[{id:'x',content:'hi'}],
    [step('')],[step('x','pending','')],[step(' '.repeat(2))],[step('x','pending','   ')],[{...step('x'),private:'secret'}]]) assert.equal(parseTaskPlan(plan({steps})),null);
  assert.equal(parseTaskPlan(plan({steps:[step('x','in_progress'),step('y','in_progress')]})),null);
  assert.ok(parseTaskPlan(plan({steps:[step('x','completed'),step('y','cancelled'),step('z','pending')]})));
});
test('Unicode bounds follow full characters and never silently truncate plans',()=>{
  assert.ok(parseTaskPlan(plan({steps:[step('🐱'.repeat(64),'pending','🐱'.repeat(160))],explanation:'🐱'.repeat(300)})));
  assert.equal(parseTaskPlan(plan({steps:[step('a'.repeat(65))]})),null);
  assert.equal(parseTaskPlan(plan({steps:[step('x','pending','a'.repeat(161))]})),null);
  assert.equal(parseTaskPlan(plan({explanation:'a'.repeat(301)})),null);
});
test('validator returns detached immutable public data',()=>{
  const original=plan(),parsed=parseTaskPlan(original);original.steps[0].content='mutated';
  assert.equal(parsed.steps[0].content,'处理步骤 one');assert.ok(Object.isFrozen(parsed));assert.ok(Object.isFrozen(parsed.steps[0]));
});
test('live tracking only advances matching run, source, and revision',()=>{
  const tracker=new TaskPlanTracker(run());
  assert.equal(tracker.event('other',plan({revision:9})),false);
  assert.equal(tracker.event('r1',plan()),false);
  assert.equal(tracker.event('r1',plan({revision:2,source:'codex.todo_list'})),false);
  assert.equal(tracker.event('r1',plan({revision:2,steps:[step('one','completed')]})),true);
  assert.equal(tracker.event('r1',plan({revision:1})),false);assert.equal(tracker.plan.revision,2);
});
test('limited tracker freezes further events',()=>{
  const tracker=new TaskPlanTracker(run({task_plan:plan({revision:64})}));
  assert.ok(tracker.event('r1',plan({revision:65,limited:true})));
  assert.equal(tracker.event('r1',plan({revision:65,limited:true,steps:[]})),false);
  assert.equal(tracker.plan.steps.length,2);assert.equal(tracker.plan.revision,65);
});
test('only revision 65 may be limited and that final revision must be limited',()=>{
  for(const revision of [1,2,64]) assert.equal(parseTaskPlan(plan({revision,limited:true})),null);
  assert.equal(parseTaskPlan(plan({revision:65,limited:false})),null);
  assert.ok(parseTaskPlan(plan({revision:64,limited:false})));
  assert.ok(parseTaskPlan(plan({revision:65,limited:true})));
});
test('nonterminal GET never rolls back a newer event while terminal GET is authoritative',()=>{
  const tracker=new TaskPlanTracker(run());tracker.event('r1',plan({revision:5}));
  tracker.snapshot(run({task_plan:plan({revision:2})}));assert.equal(tracker.plan.revision,5);
  tracker.snapshot(run({status:'completed',task_plan:plan({revision:3,steps:[]})}));
  assert.equal(tracker.plan.revision,3);assert.deepEqual(tracker.plan.steps,[]);
  assert.equal(tracker.event('r1',plan({revision:9})),false);
});
test('terminal null or invalid snapshot removes a prior plan and cannot be resurrected',()=>{
  for(const task_plan of [null,undefined,plan({revision:999})]) {
    const tracker=new TaskPlanTracker(run());tracker.snapshot(run({status:'interrupted',task_plan}));
    assert.equal(tracker.plan,null);assert.equal(tracker.event('r1',plan({revision:8})),false);
  }
});
test('authority may choose a new source while old source events remain fenced',()=>{
  const tracker=new TaskPlanTracker(run());tracker.snapshot(run({task_plan:plan({source:'claude.tasks',revision:2})}));
  assert.equal(tracker.event('r1',plan({revision:3})),false);
  assert.equal(tracker.event('r1',plan({source:'claude.tasks',revision:3})),true);
});
test('plan.updated consumes journal cursor without driving status, tools, lastEvent or speech',()=>{
  const recovery=observer(),state=target(),previous=state.lastEvent;
  recovery.apply(state,event(1,plan({revision:2,steps:[step('x','completed')]})));
  assert.equal(state.cursor,1);assert.equal(state.run.task_plan.revision,2);assert.equal(state.run.status,'running');
  assert.equal(state.lastEvent,previous);assert.equal(state.text,'existing reply');assert.deepEqual(state.timeline,[]);
  recovery.apply(state,event(2,plan({revision:1})));assert.equal(state.run.task_plan.revision,2);
  recovery.apply(state,event(10,plan({revision:9}),'other'));assert.equal(state.cursor,2);
});
test('recovery rejects malformed and source-swapped events but still advances durable cursor',()=>{
  const recovery=observer(),state=target();
  recovery.apply(state,event(1,plan({steps:[step('x','in_progress'),step('y','in_progress')]})));
  recovery.apply(state,event(2,plan({source:'codex.todo_list',revision:10})));
  assert.equal(state.cursor,2);assert.equal(state.run.task_plan.revision,1);
});
test('terminal event plan cannot overwrite newer data or accept subsequent plan replay',()=>{
  const recovery=observer(),state=target({task_plan:plan({revision:5})});
  recovery.apply(state,{seq:1,run_id:'r1',event:'run.completed',payload:{status:'completed',task_plan:plan({revision:2})}});
  recovery.apply(state,event(2,plan({revision:6})));
  assert.equal(state.run.task_plan.revision,5);assert.equal(state.run.status,'completed');
  recovery.reconcile(state,run({status:'completed',task_plan:null}));assert.equal(state.run.task_plan,null);
});
test('cancelled and failed run events retain model-reported steps without marking them completed',()=>{
  for(const type of ['run.cancelled','run.failed']) {
    const recovery=observer(),state=target();recovery.apply(state,{seq:1,run_id:'r1',event:type,payload:{error:'error'}});
    recovery.apply(state,event(2,plan({revision:2,steps:[step('one','completed')]})));
    assert.equal(state.run.task_plan.steps[0].status,'in_progress');assert.equal(state.run.task_plan.revision,1);
  }
});
test('historical GET plan wins over newer malicious replay and inspection stays read-only',async()=>{
  const latest=run({status:'completed',task_plan:plan({revision:4,steps:[step('one','pending')]}),answer:'actual reply'}),calls=[];
  const recovery=new CompanionRecovery({userId:'u1',conversationId:'c1',request:async(path,options)=>{
    calls.push([path,options?.method]);
    if(path==='/agent/runs/r1') return latest;
    if(path.includes('/events?')) return {items:[event(1,plan({revision:55,steps:[step('fake','completed')]}))],has_more:false};
    assert.fail(path);
  },fetchStream:()=>assert.fail('inspection must not open stream'),onTerminal:()=>assert.fail('inspection must not speak')});
  const result=await recovery.inspect('r1');assert.equal(result.run.task_plan.revision,4);
  assert.equal(result.run.task_plan.steps[0].status,'pending');assert.ok(calls.every(([,method])=>!method||method==='GET'));
});
test('live stream plan updates are independent of receipt and only terminal reply may speak',async()=>{
  let streamed=false,spoken=0,seen=[];
  const initial=run({task_plan:null,execution_receipt:{sentinel:'receipt'}}),finished=run({status:'completed',task_plan:plan({revision:2}),answer:'final reply',execution_receipt:{sentinel:'receipt'}});
  const recovery=new CompanionRecovery({userId:'u1',conversationId:'c1',onChange:value=>seen.push(value),onTerminal:()=>spoken++,request:async(path)=>{
    if(path.startsWith('/agent/runs?')) return {items:[]};
    if(path.includes('/events?')) return {items:[],has_more:false};
    return streamed?finished:initial;
  },fetchStream:async()=>{streamed=true;return new Response([
    event(1,plan()),event(2,plan({revision:2})),
    {seq:3,run_id:'r1',event:'run.completed',payload:finished},
  ].map(value=>'data: '+JSON.stringify(value)+'\n\n').join(''),{headers:{'Content-Type':'text/event-stream'}});}});
  await recovery.follow(initial,{recovered:false,admitted:true});
  assert.ok(seen.some(value=>value.run?.task_plan?.revision===1&&value.run.status==='running'));
  assert.equal(recovery.state.run.task_plan.revision,2);assert.deepEqual(recovery.state.run.execution_receipt,{sentinel:'receipt'});
  assert.equal(spoken,1);recovery.dispose();
});
