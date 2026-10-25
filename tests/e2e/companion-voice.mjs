// Standalone headless integration QA; not part of the dependency-free Node suite.
// Install/provide Playwright via normal module resolution or NODE_PATH. Optionally
// set CAIBAO_CHROMIUM_EXECUTABLE and CAIBAO_QA_SCREENSHOT_DIR. No devices, existing
// server, model credentials, or user browser profile are used.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const require = createRequire(import.meta.url);
const {chromium} = require('playwright');
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
let browser;
const checks = [], screenshots = [];

async function fixture({desktop=true,nativeQuery=desktop,capabilities={recognition:true,onDeviceRecognition:true},fastTimeout=false}={}) {
  const context = await browser.newContext({viewport:{width:desktop?440:1100,height:desktop?740:900}});
  const page = await context.newPage(), calls = [], errors = [];
  await page.addInitScript(({capabilities,desktop,fastTimeout}) => {
    localStorage.setItem('caibao.companion.conversation.u1','c1');
    window.__nativeCalls=[];window.__recognizers=[];
    window.SpeechRecognition = class {
      constructor() {window.__recognizers.push(this);this.started=0;this.stopped=0;this.aborted=0;}
      start() {this.started++;}
      stop() {this.stopped++;}
      abort() {this.aborted++;this.onend?.();}
    };
    if (desktop) {
      window.caibaoNative=capabilities;
      Object.defineProperty(window,'webkit',{value:{messageHandlers:{caibao:{postMessage:message=>window.__nativeCalls.push(message)}}},configurable:true});
    }
    if (fastTimeout) {
      const timer=window.setTimeout.bind(window);
      window.setTimeout=(fn,ms,...args)=>timer(fn,ms===37_000||ms===8_000?25:ms,...args);
    }
  },{capabilities,desktop,fastTimeout});
  let admitted = null;
  const run = () => ({run_id:'r1',conversation_id:'c1',trigger_channel:'companion',task:admitted?.task || '',status:'completed',answer:'收到你的草稿了。',steps:[]});
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route => {
    const request=route.request(),url=new URL(request.url());
    if (url.origin!==origin) throw new Error('Unexpected external request: '+url.origin);
    if (!url.pathname.startsWith('/api/v1/')) return route.continue();
    const target=url.pathname.slice('/api/v1'.length), method=request.method();
    calls.push({target,method,body:request.postData()?request.postDataJSON():null});
    let body, status=200;
    if (target==='/auth/me') body={user_id:'u1',display_name:'语音测试用户'};
    else if (target==='/auth/logout') {body=null;status=204;}
    else if (target==='/companion/profile') body={greeting:'你好',state:{activity:'idle',emotion:'idle',label:'我在这里'},runtime:{demo_mode:true}};
    else if (target==='/llm/models') body={items:[]};
    else if (target==='/companion/backends') body=[{id:'api',kind:'api',available:true,label:'API'}];
    else if (target==='/chat/history') body={items:[]};
    else if (target==='/agent/runs') body={items:admitted?[run()]:[]};
    else if (target==='/companion/runs'&&method==='POST') {admitted=request.postDataJSON();body={run_id:'r1',stream_url:'/agent/runs/r1/stream',status:'queued'};status=201;}
    else if (target==='/agent/runs/r1') body=run();
    else if (target==='/agent/runs/r1/events') body={items:[],status:'completed',has_more:false};
    else throw new Error('Unexpected fixture API: '+method+' '+target);
    await route.fulfill({status,contentType:'application/json',body:status===204?'':JSON.stringify(body)});
  });
  await page.goto(origin+'/companion'+(nativeQuery?'?desktop=1':''));
  await page.waitForFunction(()=>!document.querySelector('#account-button').hidden&&document.querySelector('#backend').options.length>0&&!document.querySelector('#send').disabled);
  if (desktop) await page.evaluate(capabilities=>window.dispatchEvent(new CustomEvent('caibao-native-ready',{detail:{...capabilities,speech:true,compact:false}})),capabilities);
  return {context,page,calls,errors,async close(){assert.deepEqual(errors,[]);await context.close();}};
}
const phase = (page,value) => page.waitForFunction(expected=>document.querySelector('#voice-input').dataset.phase===expected,value);
const nativeCalls = page => page.evaluate(()=>window.__nativeCalls);
const starts = async page => (await nativeCalls(page)).filter(call=>call.action==='startListening');
const taskWrites = fixture => fixture.calls.filter(call=>call.method==='POST'&&call.target==='/companion/runs');
async function nativeEvent(page,state,extra={},turnId=null) {
  await page.evaluate(({state,extra,turnId})=>{
    const id=turnId||window.__nativeCalls.findLast(call=>call.action==='startListening')?.turn_id;
    window.dispatchEvent(new CustomEvent('caibao-native-listen',{detail:{turn_id:id,state,...extra}}));
  },{state,extra,turnId});
}
async function browserEvent(page,index,event,text='',isFinal=true) {
  await page.evaluate(({index,event,text,isFinal})=>{
    const recognition=window.__recognizers[index];
    if (event==='result') recognition.onresult?.({resultIndex:0,results:[Object.assign([{transcript:text}],{isFinal})]});
    else recognition['on'+event]?.({error:'not-allowed'});
  },{index,event,text,isFinal});
}

try {
  browser=await chromium.launch({headless:true,...(process.env.CAIBAO_CHROMIUM_EXECUTABLE?{executablePath:process.env.CAIBAO_CHROMIUM_EXECUTABLE}:{}),args:['--no-first-run','--no-default-browser-check']});

  {
    const f=await fixture({nativeQuery:false});
    try {
      await f.page.locator('#mic').click();
      assert.equal(await f.page.locator('#voice-input').getAttribute('data-phase'),'authorizing','a real native bridge must remain device-only without a query flag');
      assert.equal((await starts(f.page)).length,1);
      assert.equal(await f.page.evaluate(()=>window.__recognizers.length),0);
      assert.equal(await f.page.locator('#voice-consent').isVisible(),false);
    } finally {await f.close();}
  }
  checks.push('native bridge remains device-only after navigation without a desktop query flag');

  for (const capabilities of [{},{recognition:true},{recognition:false,onDeviceRecognition:true}]) {
    const f=await fixture({capabilities});
    try {
      assert.equal((await nativeCalls(f.page)).filter(call=>call.action==='startListening').length,0);
      await f.page.locator('#message').fill('请保留这份草稿');
      await f.page.locator('#mic').click();await phase(f.page,'error');
      assert.equal(await f.page.evaluate(()=>window.__recognizers.length),0);
      assert.equal((await starts(f.page)).length,0);
      assert.equal(await f.page.locator('#message').inputValue(),'请保留这份草稿');
      assert.equal(taskWrites(f).length,0);
    } finally {await f.close();}
  }
  checks.push('native capability failures never start capture or browser fallback');

  {
    const f=await fixture();
    try {
      const p=f.page;
      assert.equal((await starts(p)).length,0,'loading the actual page must not request capture');
      await p.locator('#message').fill('已有草稿');await p.locator('#mic').click();await phase(p,'authorizing');
      await nativeEvent(p,'listening');await phase(p,'listening');
      await p.evaluate(()=>window.dispatchEvent(new CustomEvent('caibao-native-speech',{detail:{state:'start'}})));
      assert.equal(await p.locator('#habitat').getAttribute('data-activity'),'listening');
      await p.locator('#mute').click();
      assert.equal(await p.locator('#habitat').getAttribute('data-activity'),'listening');
      await p.locator('#message').fill('录音时手工改过的草稿');
      await nativeEvent(p,'partial',{text:'不能自动写入的半句话'});
      assert.equal(await p.locator('#message').inputValue(),'录音时手工改过的草稿');
      assert.equal(await p.locator('#send').isDisabled(),true);
      await p.locator('#mic').click();await phase(p,'processing');
      const id=(await starts(p)).at(-1).turn_id;
      const final='删除旧文件 <b>仅为识别文字</b>';
      await nativeEvent(p,'result',{text:final});await phase(p,'review');
      await nativeEvent(p,'result',{text:'重复结果不应进入'},id);
      assert.equal(await p.locator('#message').inputValue(),'录音时手工改过的草稿\n'+final);
      assert.equal(await p.locator('#voice-transcript b').count(),0);
      assert.equal(taskWrites(f).length,0,'a final transcript cannot submit a task');
      const screenshot=path.join(screenshotDir,'caibao-voice-native-qa.png');await p.screenshot({path:screenshot,fullPage:true});screenshots.push(screenshot);
      assert.equal(await p.locator('#native-hide').isVisible(),true);assert.equal(await p.locator('#native-quit').isVisible(),true);
      assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      const composer=await p.locator('#composer').boundingBox();assert.ok(composer.y>=0&&composer.y+composer.height<=740,'composer remains in the native viewport');
      await p.locator('#send').click();await p.waitForFunction(()=>document.querySelector('#run-strip').dataset.phase==='finished');
      assert.equal(taskWrites(f).length,1);assert.equal(taskWrites(f)[0].body.task,'录音时手工改过的草稿\n'+final);
      assert.equal(await p.evaluate(()=>window.__recognizers.length),0);
    } finally {await f.close();}
  }
  checks.push('native draft edits, stale TTS, partial/final, manual-only submit and 440x740 layout');

  for (const scope of ['hide','compact','conversation','logout','pagehide','hidden']) {
    const f=await fixture();
    try {
      const p=f.page;await p.locator('#message').fill('取消前草稿');await p.locator('#mic').click();
      const old=(await starts(p)).at(-1).turn_id;
      if(scope==='hide') await p.locator('#native-hide').click();
      else if(scope==='compact') await p.locator('#collapse').click();
      else if(scope==='conversation') {await p.locator('#settings-toggle').click();await p.locator('#new-chat').click();}
      else if(scope==='logout') await p.locator('#account-button').click();
      else if(scope==='pagehide') await p.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
      else await p.evaluate(()=>{Object.defineProperty(document,'hidden',{get:()=>true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));});
      await phase(p,'idle');
      const preserved=await p.locator('#message').inputValue();
      await nativeEvent(p,'listening',{},old);await nativeEvent(p,'result',{text:'旧权限或旧回调不能写进新草稿'},old);
      assert.equal(await p.locator('#message').inputValue(),preserved);
      assert.ok((await nativeCalls(p)).some(call=>call.action==='cancelListening'&&call.turn_id===old));
      assert.equal(taskWrites(f).length,0);
    } finally {await f.close();}
  }
  checks.push('hide/compact/conversation/logout/pagehide/visibility invalidate delayed native callbacks');

  for(const reason of ['speech_denied','microphone_denied','on_device_unavailable','recognition_failed']) {
    const f=await fixture();
    try {
      await f.page.locator('#message').fill('失败也保留');await f.page.locator('#mic').click();
      await nativeEvent(f.page,'error',{reason});await phase(f.page,'error');
      assert.equal(await f.page.locator('#message').inputValue(),'失败也保留');
      assert.equal(await f.page.evaluate(()=>window.__recognizers.length),0);
      assert.equal(await f.page.locator('#send').isDisabled(),false);
      await f.page.locator('#mic').click();await phase(f.page,'authorizing');
      assert.equal((await starts(f.page)).length,2);assert.equal(taskWrites(f).length,0);
    } finally {await f.close();}
  }
  checks.push('native permission/device/errors preserve drafts and allow a new explicit attempt');

  {
    const f=await fixture({fastTimeout:true});
    try {
      await f.page.locator('#mic').click();await nativeEvent(f.page,'listening');await phase(f.page,'error');
      assert.match(await f.page.locator('#voice-status').textContent(),/超时/);
      assert.ok((await nativeCalls(f.page)).some(call=>call.action==='cancelListening'));
      assert.equal(await f.page.evaluate(()=>window.__recognizers.length),0);assert.equal(taskWrites(f).length,0);
    } finally {await f.close();}
  }
  checks.push('frontend timeout cancels native capture without fallback or submission');

  {
    const f=await fixture({desktop:false});
    try {
      const p=f.page;await p.locator('#message').fill('浏览器草稿');await p.locator('#mic').click();await phase(p,'consent');
      assert.equal(await p.evaluate(()=>window.__recognizers.length),0);
      assert.match(await p.locator('#voice-consent').textContent(),/浏览器提供商.*并非彩宝的设备端识别/s);
      await p.locator('#voice-continue').click();assert.equal(await p.evaluate(()=>window.__recognizers.length),1);
      await browserEvent(p,0,'start');await phase(p,'listening');
      await browserEvent(p,0,'result','不应进入草稿',false);assert.equal(await p.locator('#message').inputValue(),'浏览器草稿');
      await p.locator('#voice-cancel').click();await phase(p,'idle');
      await browserEvent(p,0,'result','已取消的结果');assert.equal(await p.locator('#message').inputValue(),'浏览器草稿');
      await p.locator('#mic').click();await phase(p,'consent');assert.equal(await p.evaluate(()=>window.__recognizers.length),1);
      await p.locator('#voice-continue').click();await browserEvent(p,1,'start');
      await browserEvent(p,1,'result','只形成新的草稿');await phase(p,'review');
      await browserEvent(p,1,'result','重复末尾');
      assert.equal(await p.locator('#message').inputValue(),'浏览器草稿\n只形成新的草稿');assert.equal(taskWrites(f).length,0);
      assert.ok(await p.evaluate(()=>window.__recognizers.every(recognition=>recognition.aborted>0)));
      const screenshot=path.join(screenshotDir,'caibao-voice-browser-qa.png');await p.screenshot({path:screenshot,fullPage:true});screenshots.push(screenshot);
    } finally {await f.close();}
  }
  checks.push('browser consent precedes construction, cancellation resets consent and final text remains a draft');

  {
    const f=await fixture();
    try {
      const p=f.page;await p.locator('#message').fill('原'.repeat(3998));await p.locator('#mic').click();
      await nativeEvent(p,'listening');await nativeEvent(p,'result',{text:'无法挤入的语音 <b>纯文本</b>'});await phase(p,'review');
      assert.equal(await p.locator('#message').inputValue(),'原'.repeat(3998));assert.equal(await p.locator('#voice-review').isVisible(),true);
      assert.equal(await p.locator('#voice-transcript b').count(),0);assert.equal(await p.locator('#mic').isDisabled(),true);
      await p.locator('#message').fill('手工缩短');await p.locator('#voice-insert').click();
      assert.equal(await p.locator('#message').inputValue(),'手工缩短\n无法挤入的语音 <b>纯文本</b>');
      assert.equal(await p.locator('#voice-review').isVisible(),false);assert.equal(taskWrites(f).length,0);
    } finally {await f.close();}
  }
  checks.push('a full draft retains the transcript for explicit insertion without truncation or HTML execution');
  console.log(JSON.stringify({passed:true,checks,screenshots},null,2));
} finally {await browser?.close();await new Promise(resolve=>server.close(resolve));}
