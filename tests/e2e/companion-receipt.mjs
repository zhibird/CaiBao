// Standalone integration QA of the actual companion page. Supply Playwright
// through normal module resolution/NODE_PATH; no production server, browser
// profile, model, microphone, or native window is used.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const {chromium} = createRequire(import.meta.url)('playwright');
const web = fileURLToPath(new URL('../../app/web/',import.meta.url));
const screenshotDir = process.env.CAIBAO_QA_SCREENSHOT_DIR || os.tmpdir();
await fs.mkdir(screenshotDir,{recursive:true});
const server = http.createServer(async (request,response) => {
  try {
    const pathname = new URL(request.url,'http://localhost').pathname;
    const file = pathname.startsWith('/web/') ? path.resolve(web,pathname.slice(5)) : path.join(web,'companion.html');
    if (!file.startsWith(web)) throw new Error('outside fixture root');
    response.setHeader('Content-Type',file.endsWith('.css') ? 'text/css' : /\.m?js$/.test(file) ? 'text/javascript' : 'text/html');
    response.end(await fs.readFile(file));
  } catch {response.statusCode=404;response.end();}
});
await new Promise((resolve,reject) => {server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
const origin = `http://127.0.0.1:${server.address().port}`;
const baseReceipt = {
  version:1,source:'api_tool_loop',tool_budget:3,tool_calls_used:2,
  tool_calls_succeeded:2,tool_calls_failed:0,tool_calls_rejected:0,tool_calls_skipped:0,
  pending_tool_calls:0,omitted_tool_calls:0,stop_reason:'model_finished',incomplete:false,
};
const receipt = changes => ({...baseReceipt,...changes});
const run = (id,status,execution_receipt=receipt()) => ({
  run_id:id,conversation_id:'c1',trigger_channel:'companion',task:`核对任务 ${id}`,status,
  answer:'这里是已返回的回复，请结合回执检查。',steps:[],execution_receipt,
  execution_identity:{kind:'api',backend:'api',model:'actual-recorded-model',model_source:'resolved',binding:'DO-NOT-SHOW-BINDING'},
  context_snapshot:{version:1,source:'caibao.chat_history',selected_message_ids:['DO-NOT-SHOW-MESSAGE-ID'],selected_count:2,omitted_count:0,truncated_count:0,history_enabled:true,continuity:'api_history'},
});
const failedRun = () => run('failed-record','failed',receipt({tool_calls_used:1,tool_calls_succeeded:0,tool_calls_failed:1,incomplete:true}));
const partialRun = () => run('partial-record','partial_success',receipt({tool_calls_succeeded:1,tool_calls_failed:1,omitted_tool_calls:1,incomplete:true}));
const exactRun = () => run('exact-record','completed',receipt({tool_calls_used:3,tool_calls_succeeded:3,stop_reason:'tool_budget'}));
const legacyRun = () => run('legacy-record','completed',null);
const checks = [], failures = [], screenshots = [];
let browser;

async function fixture({records=[],current=null,live=null,desktop=true}={}) {
  const context = await browser.newContext({viewport:{width:desktop?440:1100,height:desktop?740:900}});
  const page = await context.newPage(), calls = [], errors = [];
  let admitted = false, streamed = false;
  const stored = records.map(value=>structuredClone(value));
  if (current && !stored.some(value=>value.run_id===current.run_id)) stored.unshift(structuredClone(current));
  await page.addInitScript(({current,desktop})=>{
    localStorage.setItem('caibao.companion.conversation.u1','c1');
    localStorage.setItem('caibao.companion.preferences',JSON.stringify({voice:true}));
    if (current) localStorage.setItem('caibao.companion.run.u1.c1',JSON.stringify({run_id:current.run_id,task:current.task}));
    window.__nativeCalls=[];
    if (desktop) {
      window.caibaoNative={speech:true,recognition:false,onDeviceRecognition:false};
      Object.defineProperty(window,'webkit',{value:{messageHandlers:{caibao:{postMessage:message=>window.__nativeCalls.push(message)}}},configurable:true});
    }
    window.SpeechRecognition=class {constructor(){throw new Error('Receipt QA must never record audio');}};
  },{current,desktop});
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if (url.origin!==origin) {errors.push('External network request: '+url.origin);return route.abort();}
    if (!url.pathname.startsWith('/api/v1/')) return route.continue();
    const target=url.pathname.slice('/api/v1'.length),method=request.method();
    calls.push({target,method,body:request.postData()?request.postDataJSON():null});
    let body,status=200;
    const selected=live&&admitted?live:stored.find(value=>target.startsWith('/agent/runs/'+value.run_id));
    if (target==='/auth/me') body={user_id:'u1',display_name:'回执验收用户'};
    else if (target==='/companion/profile') body={greeting:'你好',state:{activity:'idle',emotion:'idle'},runtime:{demo_mode:false,model:'next-preference-model'}};
    else if (target==='/llm/models') body={items:[{model_name:'next-preference-model'},{model_name:'another-next-model'}]};
    else if (target==='/companion/backends') body=[{id:'api',kind:'api',available:true,label:'API'},{id:'codex',kind:'cli',available:true,label:'Codex CLI',model:null}];
    else if (target==='/chat/history') body={items:[]};
    else if (target==='/agent/runs') body={items:live&&admitted?[live,...stored]:stored};
    else if (target==='/companion/runs'&&method==='POST'&&live) {admitted=true;body={run_id:live.run_id,status:'queued'};status=201;}
    else if (selected&&target.endsWith('/stream')) {
      streamed=true;
      const event={seq:1,event:live.status==='failed'?'run.failed':'run.completed',run_id:live.run_id,payload:{...live,error:live.status==='failed'?'执行遇到问题':undefined}};
      return route.fulfill({status:200,contentType:'text/event-stream',body:`id: 1\ndata: ${JSON.stringify(event)}\n\n`});
    }
    else if (selected&&target.endsWith('/events')) body={items:[],has_more:false,status:live&&admitted&&!streamed?'running':selected.status};
    else if (selected) body=live&&admitted&&!streamed?{...selected,status:'running',answer:'',execution_receipt:null}:selected;
    else {errors.push('Unexpected fixture API: '+method+' '+target);return route.fulfill({status:404,contentType:'application/json',body:'{}'});}
    await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto(origin+'/companion'+(desktop?'?desktop=1':''));
  await page.waitForFunction(()=>!document.querySelector('#account-button').hidden&&document.querySelector('#backend').options.length>1);
  // Restoration may finish while the native pet is still compact. Expanding
  // afterwards must reveal its latest returned reply, not reset to old chat.
  if (current) await page.waitForFunction(()=>document.querySelector('#run-strip').dataset.phase==='finished');
  if (desktop) await page.evaluate(()=>window.dispatchEvent(new CustomEvent('caibao-native-ready',{detail:{speech:true,compact:false}})));
  if (!current) await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  return {context,page,calls,errors,async close(){await context.close();assert.deepEqual(errors,[]);}};
}
async function check(name,work) {
  try {await work();checks.push(name);} catch(error) {failures.push({name,error:error.stack});console.error(name+': '+error.message);}
}
async function screenshot(page,name) {
  const filename=path.join(screenshotDir,name);await page.screenshot({path:filename,fullPage:true});screenshots.push(filename);
}
async function layout(page) {
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'no horizontal overflow');
  for(const id of ['native-hide','native-quit']) assert.equal(await page.locator('#'+id).isVisible(),true);
  const box=await page.locator('#composer').boundingBox();
  assert.ok(box.y>=0&&box.y+box.height<=740,`composer must remain reachable: ${JSON.stringify(box)}`);
  const send=await page.locator('#send').boundingBox();
  assert.ok(send.y+send.height<=740,'send button must remain in the pet viewport');
}
const writes = f => f.calls.filter(call=>call.method!=='GET');
const spoken = page => page.evaluate(()=>window.__nativeCalls.filter(call=>call.action==='speak'));

try {
  browser=await chromium.launch({headless:true,...(process.env.CAIBAO_CHROMIUM_EXECUTABLE?{executablePath:process.env.CAIBAO_CHROMIUM_EXECUTABLE}:{}),args:['--no-first-run','--no-default-browser-check']});

  for(const [name,makeRun,statusText] of [
    ['failed',failedRun,'执行遇到问题'],['partial',partialRun,'有些步骤没完成'],['exact-budget',exactRun,'回复已返回'],
  ]) await check('recovered current '+name+' receipt and 440x740 layout',async()=>{
    const f=await fixture({current:makeRun()});
    try {
      const p=f.page,text=await p.locator('#run-receipt').textContent();
      assert.match(await p.locator('#run-connection').textContent(),new RegExp(statusText));
      assert.match(text,/工具调用/);assert.match(text,/返回成功/);
      assert.match(text, name==='exact-budget'?/工具调用 3 \/ 3 次/:/返回失败 1/);
      assert.equal(await p.locator('#run-receipt').getAttribute('data-review'),String(name!=='exact-budget'));
      assert.match(await p.locator('#run-execution').textContent(),/actual-recorded-model/);
      assert.match(await p.locator('#model-label').textContent(),/下次.*next-preference-model/);
      assert.equal((await spoken(p)).length,0,'recovery never replays speech');
      assert.equal(writes(f).length,0,'loading a recovered task cannot dispatch work');
      if(name!=='exact-budget') assert.equal(await p.locator('#habitat').getAttribute('data-emotion'),'concerned');
      // The production chat uses smooth scrolling; allow its real animation to
      // settle before checking whether rendering the receipt hid the reply.
      await p.waitForFunction(()=>{
        const messages=document.querySelector('#messages'),last=messages.querySelector('.message.assistant:last-child .content');
        const viewport=messages.getBoundingClientRect(),content=last?.getBoundingClientRect();
        return content&&content.top>=viewport.top&&content.bottom<=viewport.bottom;
      },null,{timeout:2000}).catch(()=>{});
      await screenshot(p,`caibao-receipt-${name}-current-qa.png`);await layout(p);
      const visibleReply=await p.locator('#messages').evaluate(element=>{
        const last=element.querySelector('.message.assistant:last-child .content');
        const viewport=element.getBoundingClientRect(),content=last?.getBoundingClientRect();
        return {text:last?.textContent,scrollTop:element.scrollTop,scrollHeight:element.scrollHeight,clientHeight:element.clientHeight,
          visible:content&&content.top>=viewport.top&&content.bottom<=viewport.bottom};
      });
      assert.equal(visibleReply.visible,true,'latest reply remains visible after rendering a receipt: '+JSON.stringify(visibleReply));
    } finally {await f.close();}
  });

  for(const status of ['failed','partial_success','completed']) await check('new '+status+' task receives its durable receipt',async()=>{
    const value=status==='failed'?failedRun():status==='partial_success'?partialRun():exactRun();
    const f=await fixture({live:value});
    try {
      const p=f.page;await p.locator('#message').fill('只发出这一项测试任务');await p.locator('#send').click();
      await p.waitForFunction(()=>document.querySelector('#run-strip').dataset.phase==='finished');
      assert.match(await p.locator('#run-receipt').textContent(),new RegExp(`工具调用 ${value.execution_receipt.tool_calls_used} / 3 次`));
      assert.equal(writes(f).length,1);assert.equal(writes(f)[0].target,'/companion/runs');
      assert.equal(f.calls.filter(call=>call.target.endsWith('/stream')).length,1);
      assert.equal((await spoken(p)).length,status==='completed'?1:0,'failed and partial tasks never celebrate through automatic speech');
      await p.locator('#task-history > summary').click();
      await p.locator(`#history-list [data-run-id="${value.run_id}"]`).click();
      await p.waitForFunction(()=>document.querySelector('#history-receipt').textContent.includes('工具调用'));
      assert.equal(await p.locator('#history-receipt').textContent(),await p.locator('#run-receipt').textContent());
      assert.equal(writes(f).length,1,'history cannot rerun the task');
      assert.equal((await spoken(p)).length,status==='completed'?1:0,'history never triggers speech');
      if(status==='partial_success') await screenshot(p,'caibao-receipt-current-and-history-qa.png');
      await layout(p);
    } finally {await f.close();}
  });

  await check('cancelled tool with no return remains uncertain instead of awaiting approval',async()=>{
    const cancelled=run('cancelled-record','cancelled',receipt({
      tool_calls_succeeded:1,pending_tool_calls:1,stop_reason:'cancelled',incomplete:true,
    }));
    const f=await fixture({current:cancelled});
    try {
      const p=f.page,text=await p.locator('#run-receipt').textContent();
      assert.match(text,/结果未确认|未确认结果/);
      assert.doesNotMatch(text,/待确认 1|待确认的操作尚未执行/);
      assert.match(text,/已开始.*可能.*结束|已经开始.*可能.*结束/);
      assert.equal(await p.locator('#approval').isVisible(),false);
      assert.equal(writes(f).length,0);assert.equal((await spoken(p)).length,0);
      await layout(p);
    } finally {await f.close();}
  });

  await check('history receipts are read-only, distinguish actual identity and preserve legacy uncertainty',async()=>{
    const cli=run('cli-record','completed',null);cli.execution_identity={kind:'cli',backend:'codex',model:null,model_source:'runtime_default'};
    const old=legacyRun();old.execution_identity=null;old.context_snapshot=null;
    old.request_payload={max_steps:99};old.steps=[{step_type:'tool_call',step_index:1,tool_name:'old_tool',status:'completed'}];
    const privateRecord=partialRun();privateRecord.execution_receipt.private_history='DO-NOT-SHOW-TRANSCRIPT';
    privateRecord.execution_receipt.raw_error='DO-NOT-SHOW-RAW-ERROR';
    privateRecord.execution_receipt.arguments={api_key:'DO-NOT-SHOW-KEY'};
    const f=await fixture({records:[failedRun(),privateRecord,exactRun(),old,cli]});
    try {
      const p=f.page;await p.locator('#task-history > summary').click();
      for(const value of [failedRun(),privateRecord,exactRun(),old,cli]) {
        await p.locator(`#history-list [data-run-id="${value.run_id}"]`).click();
        await p.waitForFunction(task=>document.querySelector('#history-title').textContent===task,value.task);
        const summary=await p.locator('#history-receipt').textContent();
        if(value.execution_receipt) assert.match(summary,/工具调用/);
        else {assert.match(summary,/本次未记录 API 工具回执/);assert.doesNotMatch(summary,/99|返回成功|工具调用 \d/);}
        if(value.run_id===cli.run_id) assert.match(await p.locator('#history-execution').textContent(),/由 CLI 自选/);
        if(value.run_id===old.run_id) assert.match(await p.locator('#history-execution').textContent(),/尚无运行记录/);
        assert.match(await p.locator('#history-state').textContent(),/只读记录/);
        assert.doesNotMatch(await p.locator('#history-preview').textContent(),/DO-NOT-SHOW|任务已完成|这一件事，完成/);
      }
      await p.locator(`#history-list [data-run-id="${privateRecord.run_id}"]`).click();
      await p.waitForFunction(()=>document.querySelector('#history-receipt').textContent.includes('返回失败 1'));
      const before=await p.locator('#history-receipt').textContent();
      await screenshot(p,'caibao-receipt-history-qa.png');await layout(p);
      await p.locator('#settings-toggle').click();await p.locator('#model-select').selectOption('another-next-model');
      assert.equal(await p.locator('#history-receipt').textContent(),before);
      assert.match(await p.locator('#history-execution').textContent(),/actual-recorded-model/);
      assert.equal(writes(f).length,0);assert.equal(f.calls.some(call=>call.target.endsWith('/stream')),false);
      assert.equal((await spoken(p)).length,0);
    } finally {await f.close();}
  });

  console.log(JSON.stringify({passed:failures.length===0,checks,failures,screenshots},null,2));
  assert.equal(failures.length,0,'Headless receipt QA failed');
} finally {await browser?.close();await new Promise(resolve=>server.close(resolve));}
