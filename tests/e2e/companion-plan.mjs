// Actual-page tests with a temporary local fixture and an isolated headless
// browser. Never use a user profile, model, microphone, or native pet window.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const {chromium}=createRequire(import.meta.url)('playwright');
const web=fileURLToPath(new URL('../../app/web/',import.meta.url));
const screenshotDir=process.env.CAIBAO_QA_SCREENSHOT_DIR||os.tmpdir();
const server=http.createServer(async(request,response)=>{
  try {
    const pathname=new URL(request.url,'http://localhost').pathname;
    const file=pathname.startsWith('/web/')?path.resolve(web,pathname.slice(5)):path.join(web,'companion.html');
    if(!file.startsWith(web))throw new Error('outside fixture');
    response.setHeader('Content-Type',file.endsWith('.css')?'text/css':/\.m?js$/.test(file)?'text/javascript':'text/html');
    response.end(await fs.readFile(file));
  }catch{response.statusCode=404;response.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
const step=(id,status='pending',content=`处理步骤 ${id}`)=>({id,status,content});
const plan=(changes={})=>({version:1,revision:1,source:'api.update_plan',model_reported:true,
  steps:[step('one','completed'),step('two','in_progress')],explanation:'这是模型报告的步骤安排。',limited:false,...changes});
const receipt={version:1,source:'api_tool_loop',tool_budget:3,tool_calls_used:2,tool_calls_succeeded:2,
  tool_calls_failed:0,tool_calls_rejected:0,tool_calls_skipped:0,pending_tool_calls:0,omitted_tool_calls:0,stop_reason:'model_finished',incomplete:false};
const run=(id,task_plan=plan(),changes={})=>({run_id:id,conversation_id:'c1',trigger_channel:'companion',task:`核对计划 ${id}`,
  status:'completed',answer:'这是已返回的回复，请结合回执核对。',steps:[],execution_receipt:receipt,task_plan,
  execution_identity:{kind:'api',backend:'api',model:'recorded-model',model_source:'resolved'},...changes});
let browser;
const checks=[],failures=[],screenshots=[];
async function check(name,work){try{await work();checks.push(name);}catch(error){failures.push({name,error:error.stack});}}
async function screenshot(page,name){await fs.mkdir(screenshotDir,{recursive:true});const file=path.join(screenshotDir,name);await page.screenshot({path:file});screenshots.push(file);}
async function fixture({records=[],current=null,live=null}={}){
  const context=await browser.newContext({viewport:{width:440,height:740}}),page=await context.newPage();
  const errors=[],calls=[];let final=false,admitted=false,streamed=false;
  await page.addInitScript(({current})=>{
    localStorage.setItem('caibao.companion.conversation.u1','c1');
    localStorage.setItem('caibao.companion.preferences',JSON.stringify({voice:true}));
    if(current)localStorage.setItem('caibao.companion.run.u1.c1',JSON.stringify({run_id:current.run_id,task:current.task}));
    window.__nativeCalls=[];window.caibaoNative={speech:true,recognition:false,onDeviceRecognition:false};
    Object.defineProperty(window,'webkit',{value:{messageHandlers:{caibao:{postMessage:value=>window.__nativeCalls.push(value)}}}});
    window.SpeechRecognition=class{constructor(){throw new Error('plan QA must not request audio');}};
  },{current});
  page.on('pageerror',error=>errors.push(error.message));
  const stored=current?[current,...records.filter(value=>value.run_id!==current.run_id)]:records;
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==origin){errors.push('external network');return route.abort();}
    if(!url.pathname.startsWith('/api/v1/'))return route.continue();
    const target=url.pathname.slice(7),method=request.method();calls.push({target,method});let body,status=200;
    const selected=live&&admitted&&target.startsWith('/agent/runs/'+live.run_id)?live:stored.find(value=>target.startsWith('/agent/runs/'+value.run_id));
    if(target==='/auth/me')body={user_id:'u1',display_name:'计划验收'};
    else if(target==='/companion/profile')body={greeting:'你好',state:{activity:'idle',emotion:'idle'},runtime:{demo_mode:false,model:'next-model'}};
    else if(target==='/llm/models')body={items:[{model_name:'next-model'}]};
    else if(target==='/companion/backends')body=[{id:'api',kind:'api',available:true,label:'API'},{id:'codex',kind:'cli',available:true,label:'Codex'}];
    else if(target==='/chat/history')body={items:[]};
    else if(target==='/agent/runs')body={items:live&&admitted?[live,...stored]:stored};
    else if(target==='/companion/runs'&&method==='POST'&&live){admitted=true;body={run_id:live.run_id,status:'queued'};status=201;}
    else if(selected&&target.endsWith('/events'))body={items:current?[{seq:1,run_id:current.run_id,event:'plan.updated',payload:{task_plan:plan({revision:60,steps:[step('replay','completed')]})}}]:[],has_more:false};
    else if(selected&&target.endsWith('/stream')){
      streamed=true;const updates=[
        {seq:1,run_id:live.run_id,event:'plan.updated',payload:{task_plan:plan({revision:2})}},
        {seq:2,run_id:live.run_id,event:'plan.updated',payload:{task_plan:plan({revision:1,steps:[step('stale')]})}},
        {seq:3,run_id:live.run_id,event:'plan.updated',payload:{task_plan:plan({revision:20,source:'codex.todo_list',steps:[step('wrong-source')]})}},
        {seq:4,run_id:'wrong-run',event:'plan.updated',payload:{task_plan:plan({revision:30,steps:[step('wrong-run')]})}},
        {seq:5,run_id:live.run_id,event:'plan.updated',payload:{task_plan:plan({revision:3,steps:[step('live','in_progress','只更新计划，不代表操作结果')]})}},
      ];
      return route.fulfill({contentType:'text/event-stream',body:updates.map(value=>'data: '+JSON.stringify(value)+'\n\n').join('')});
    }
    else if(selected)body=live&&admitted&&!final?{...live,status:'running',answer:'',task_plan:null,execution_receipt:null}:selected;
    else {errors.push('unexpected '+target);status=404;body={};}
    await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto(origin+'/companion?desktop=1');
  await page.waitForFunction(()=>!document.querySelector('#account-button').hidden&&document.querySelector('#backend').options.length>1);
  if(current)await page.waitForFunction(()=>document.querySelector('#run-strip').dataset.phase==='finished');
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent('caibao-native-ready',{detail:{speech:true,compact:false}})));
  return {context,page,calls,errors,finalize(){assert.ok(streamed);final=true;},async close(){await context.close();assert.deepEqual(errors,[]);}};
}
async function layout(page){
  const result=await page.evaluate(()=>({messages:document.querySelector('#messages').clientHeight,
    composer:document.querySelector('#composer').getBoundingClientRect().bottom,send:document.querySelector('#send').getBoundingClientRect().bottom,
    horizontal:document.documentElement.scrollWidth>innerWidth}));
  assert.ok(result.messages>=100,JSON.stringify(result));assert.ok(result.composer<=740&&result.send<=740,JSON.stringify(result));assert.equal(result.horizontal,false);
  for(const id of ['native-hide','native-quit'])assert.ok(await page.locator('#'+id).isVisible());
}
const speech=page=>page.evaluate(()=>window.__nativeCalls.filter(value=>value.action==='speak'));
async function latestReply(page){
  await page.waitForFunction(()=>{
    const box=document.querySelector('#messages').getBoundingClientRect();
    const reply=document.querySelector('#messages .message.assistant:last-child .content')?.getBoundingClientRect();
    return reply&&reply.top>=box.top&&reply.bottom<=box.bottom;
  },null,{timeout:2000});
}
try{
  browser=await chromium.launch({headless:true,...(process.env.CAIBAO_CHROMIUM_EXECUTABLE?{executablePath:process.env.CAIBAO_CHROMIUM_EXECUTABLE}:{}),args:['--no-first-run','--no-default-browser-check']});
  for(const source of ['api.update_plan','codex.todo_list','claude.TodoWrite','claude.tasks'])await check('recovered '+source+' plan preserves model attribution',async()=>{
    const value=run('current',plan({source,revision:4})),f=await fixture({current:value});
    try{
      const p=f.page;assert.ok(await p.locator('#run-plan').isVisible());await p.locator('#run-plan > summary').click();
      const text=await p.locator('#run-plan').textContent();assert.match(text,/模型报告的计划/);assert.match(text,/模型标记完成 1 \/ 2 项/);
      assert.match(text,/第 4 版/);assert.doesNotMatch(text,/replay/);assert.equal((await speech(p)).length,0);
      assert.ok(f.calls.every(value=>value.method==='GET'));await latestReply(p);await layout(p);
    }finally{await f.close();}
  });
  await check('plan plus receipt and historical preview keep messages and composer reachable',async()=>{
    const long=plan({steps:Array.from({length:10},(_,i)=>step(String(i),i===0?'in_progress':'pending','这是模型报告的待核对步骤。'.repeat(6)))});
    const value=run('long',long),f=await fixture({current:value});
    try{
      const p=f.page;await p.locator('#run-plan > summary').click();await p.locator('#task-history > summary').click();
      await p.locator('#history-list [data-run-id="long"]').click();await p.locator('#history-plan > summary').click();
      assert.equal(await p.locator('#run-plan .plan-steps li').count(),10);assert.equal(await p.locator('#history-plan .plan-steps li').count(),10);
      await latestReply(p);await layout(p);await screenshot(p,'caibao-plan-current-history-qa.png');
      await p.locator('#native-hide').click();await p.locator('#native-quit').click();
      assert.ok(f.calls.every(value=>value.method==='GET'));assert.equal((await speech(p)).length,0);
    }finally{await f.close();}
  });
  await check('history distinguishes absent, cleared, limited and malformed plans without unsafe rendering',async()=>{
    const absent=run('absent',null),cleared=run('cleared',plan({steps:[]})),limited=run('limited',plan({revision:65,limited:true}));
    const invalid=run('invalid',plan({private_history:'DO-NOT-SHOW'})),literal=run('literal',plan({steps:[step('x','pending','<img src=x onerror=alert(1)>')]}));
    const f=await fixture({records:[absent,cleared,limited,invalid,literal]});
    try{
      const p=f.page;await p.locator('#task-history > summary').click();
      for(const value of [absent,cleared,limited,invalid,literal]){
        await p.locator(`#history-list [data-run-id="${value.run_id}"]`).click();await p.waitForFunction(title=>document.querySelector('#history-title').textContent===title,value.task);
        if(['absent','invalid'].includes(value.run_id)){assert.equal(await p.locator('#history-plan').isVisible(),false);continue;}
        await p.locator('#history-plan > summary').click();const text=await p.locator('#history-plan').textContent();
        if(value===cleared){assert.match(text,/模型已清空/);assert.equal(await p.locator('#history-plan li').count(),0);}
        if(value===limited){assert.match(text,/停止更新.*保留/);assert.equal(await p.locator('#history-plan li').count(),2);}
        if(value===literal){assert.match(text,/<img/);assert.equal(await p.locator('#history-plan img').count(),0);}
        assert.doesNotMatch(text,/DO-NOT-SHOW/);await layout(p);
      }
      assert.ok(f.calls.every(value=>value.method==='GET'));assert.equal((await speech(p)).length,0);
    }finally{await f.close();}
  });
  await check('live plan events reject stale run/source/revisions and never complete or speak by themselves',async()=>{
    const final=run('live',plan({revision:4,steps:[step('final','pending','最终快照仍未完成的步骤')]})),f=await fixture({live:final});
    try{
      const p=f.page;await p.locator('#message').fill('测试这一个任务');await p.locator('#send').click();
      await p.waitForFunction(()=>document.querySelector('#run-plan').textContent.includes('第 3 版'));
      assert.match(await p.locator('#run-plan').textContent(),/只更新计划/);assert.doesNotMatch(await p.locator('#run-plan').textContent(),/wrong-source|wrong-run|stale/);
      assert.notEqual(await p.locator('#run-strip').getAttribute('data-phase'),'finished');assert.equal((await speech(p)).length,0);
      f.finalize();await p.waitForFunction(()=>document.querySelector('#run-strip').dataset.phase==='finished');
      assert.match(await p.locator('#run-plan').textContent(),/第 4 版/);assert.match(await p.locator('#run-plan').textContent(),/最终快照仍未完成/);
      assert.equal(f.calls.filter(value=>value.method==='POST').length,1);assert.equal((await speech(p)).length,0,'reconnecting must not replay speech');await latestReply(p);await layout(p);
      await screenshot(p,'caibao-plan-live-qa.png');
    }finally{await f.close();}
  });
  await check('authoritative terminal null removes a plan observed before reconnect',async()=>{
    const final=run('clear-final',null),f=await fixture({live:final});
    try{
      const p=f.page;await p.locator('#message').fill('测试终态没有计划');await p.locator('#send').click();
      await p.waitForFunction(()=>document.querySelector('#run-plan').textContent.includes('第 3 版'));
      assert.ok(await p.locator('#run-plan').isVisible());f.finalize();
      await p.waitForFunction(()=>document.querySelector('#run-strip').dataset.phase==='finished');
      assert.equal(await p.locator('#run-plan').isVisible(),false);assert.equal((await speech(p)).length,0);
      assert.equal(f.calls.filter(value=>value.method==='POST').length,1);await layout(p);
    }finally{await f.close();}
  });
  console.log(JSON.stringify({passed:failures.length===0,checks,failures,screenshots},null,2));assert.equal(failures.length,0);
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
