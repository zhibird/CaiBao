import test from 'node:test';
import assert from 'node:assert/strict';
import {CompanionVoice} from '../app/web/companion-voice.mjs';

function harness(options = {}) {
  let draft = options.draft || '', blocked = false, sequence = 0;
  const calls = [], changes = [], stopped = [], timers = new Map(), recognizers = [];
  class Recognition {
    start() {this.started = (this.started || 0) + 1;}
    stop() {this.stopped = (this.stopped || 0) + 1;}
    abort() {this.aborted = (this.aborted || 0) + 1; this.onend?.();}
  }
  const voice = new CompanionVoice({desktop:true,
    transport:(action, detail) => calls.push({action,...detail}),
    capabilities:() => ({recognition:true,onDeviceRecognition:true}),
    createRecognition:() => {const instance = new Recognition(); recognizers.push(instance); return instance;},
    readDraft:() => draft, writeDraft:value => {draft = value;}, canStart:() => !blocked,
    stopSpeech:() => stopped.push('stop'), onChange:value => changes.push(value),
    turnId:() => `turn-${++sequence}`,
    schedule:(fn,ms) => {const key = {}; timers.set(key,{fn,ms}); return key;},
    unschedule:key => timers.delete(key), ...options,
  });
  const emit = (state, extra = {}) => voice.handleNative({turn_id:voice.state.turnId,state,...extra});
  return {voice,calls,changes,stopped,timers,recognizers,emit,
    draft:() => draft, edit:value => {draft = value;}, block:value => {blocked = value;},
    expire:() => {const [key,timer] = [...timers][0] || []; assert.ok(timer,'expected an active timer'); timers.delete(key); timer.fn();},
  };
}

test('native capability inspection and construction never request permission or capture audio',() => {
  const h = harness();
  assert.equal(h.voice.state.phase,'idle'); assert.deepEqual(h.calls,[]);
  assert.deepEqual(h.stopped,[]); assert.equal(h.recognizers.length,0); assert.equal(h.timers.size,0);
});

test('explicit native start, finish and final result only append an editable draft',() => {
  const h = harness({draft:'已有草稿'});
  assert.equal(h.voice.toggle(),true);
  assert.deepEqual(h.stopped,['stop']);
  assert.deepEqual(h.calls,[{action:'startListening',turn_id:'turn-1',lang:'zh-CN'}]);
  assert.equal(h.voice.state.phase,'authorizing'); assert.equal(h.voice.state.active,true);
  assert.equal(h.draft(),'已有草稿');
  h.emit('listening'); assert.equal(h.voice.state.phase,'listening');
  h.voice.toggle(); assert.equal(h.voice.state.phase,'processing');
  assert.deepEqual(h.calls.at(-1),{action:'finishListening',turn_id:'turn-1'});
  h.emit('result',{text:'帮我写一份清单'});
  assert.equal(h.voice.state.phase,'review'); assert.equal(h.voice.state.active,false);
  assert.equal(h.draft(),'已有草稿\n帮我写一份清单'); assert.equal(h.voice.state.inserted,true);
  assert.equal(h.timers.size,0);
  assert.ok(h.calls.every(call => ['startListening','finishListening'].includes(call.action)), 'voice input cannot submit tasks');
});

test('native failed capability never falls back to a browser recognizer',() => {
  for (const capabilities of [{},{recognition:true},{recognition:false,onDeviceRecognition:true}]) {
    const h = harness({capabilities:() => capabilities});
    assert.equal(h.voice.toggle(),false); assert.equal(h.voice.state.phase,'error');
    assert.equal(h.voice.state.reason,'native_unavailable');
    assert.deepEqual(h.calls,[]); assert.equal(h.recognizers.length,0);
  }
});

test('native on-device-unavailable is an error with no cloud or browser fallback',() => {
  const h = harness({draft:'请保留'}); h.voice.toggle();
  h.emit('error',{reason:'on_device_unavailable'});
  assert.equal(h.voice.state.phase,'error'); assert.match(h.voice.state.message,/不会切换到云端/);
  assert.equal(h.draft(),'请保留'); assert.equal(h.recognizers.length,0);
});

test('unknown or malformed error reasons use a plain fallback message',() => {
  for (const reason of ['constructor','__proto__','toString',{unexpected:true}]) {
    const h = harness(); h.voice.toggle(); h.emit('error',{reason});
    assert.equal(h.voice.state.phase,'error'); assert.equal(typeof h.voice.state.message,'string');
    assert.match(h.voice.state.message,/没能完成/);
  }
});

test('click during authorization cancels without waiting for a permission decision',() => {
  const h = harness(); h.voice.toggle(); assert.equal(h.timers.size,0);
  h.voice.toggle(); assert.equal(h.voice.state.phase,'idle');
  assert.deepEqual(h.calls.at(-1),{action:'cancelListening',turn_id:'turn-1'});
  assert.equal(h.voice.handleNative({turn_id:'turn-1',state:'listening'}),false);
  assert.equal(h.voice.handleNative({turn_id:'turn-1',state:'result',text:'迟来的文字'}),false);
  assert.equal(h.draft(),'');
});

test('repeated finish and out-of-order states cannot restart or duplicate capture',() => {
  const h = harness(); h.voice.toggle(); h.emit('listening');
  h.voice.toggle(); h.voice.toggle(); h.voice.finish();
  assert.equal(h.calls.filter(call => call.action === 'finishListening').length,1);
  assert.equal(h.emit('authorizing'),false); assert.equal(h.emit('listening'),false);
  assert.equal(h.voice.state.phase,'processing');
  h.emit('result',{text:'最后文字'});
  assert.equal(h.emit('result',{text:'最后文字'}),false);
  assert.equal(h.draft(),'最后文字');
});

test('the newest draft edits are preserved instead of overwritten by a final transcript',() => {
  const h = harness({draft:'最早草稿'}); h.voice.toggle(); h.emit('listening');
  h.edit('我刚刚手动更改的要求'); h.emit('result',{text:'语音补充要求'});
  assert.equal(h.draft(),'我刚刚手动更改的要求\n语音补充要求');
});

test('full draft retains the whole transcript for explicit review rather than truncating either text',() => {
  const h = harness({draft:'x'.repeat(3998)}); h.voice.toggle(); h.emit('listening');
  h.emit('result',{text:'完整的新要求'});
  assert.equal(h.voice.state.phase,'review'); assert.equal(h.voice.state.inserted,false);
  assert.equal(h.voice.state.reason,'draft_full'); assert.equal(h.voice.state.text,'完整的新要求');
  assert.equal(h.draft(),'x'.repeat(3998)); assert.equal(h.voice.toggle(),false);
  h.edit('缩短的草稿'); assert.equal(h.voice.insertTranscript(),true);
  assert.equal(h.draft(),'缩短的草稿\n完整的新要求'); assert.equal(h.voice.insertTranscript(),false);
});

test('an existing trailing newline is preserved without adding another blank line',() => {
  const h = harness({draft:'已有草稿\n'}); h.voice.toggle(); h.emit('result',{text:'补充'});
  assert.equal(h.draft(),'已有草稿\n补充');
});

test('overlong or blank transcripts never erase the existing draft',() => {
  for (const text of ['  ','x'.repeat(4001)]) {
    const h = harness({draft:'保留'}); h.voice.toggle(); h.emit('result',{text});
    assert.equal(h.voice.state.phase,'error'); assert.equal(h.draft(),'保留');
    assert.equal(h.timers.size,0);
  }
});

for (const reason of ['microphone_denied','speech_denied','speech_restricted','recognizer_unavailable',
  'audio_unavailable','recognition_failed','no_speech','final_timeout','invalid_request']) {
  test(`native ${reason} ends the turn without changing the draft`,() => {
    const h = harness({draft:'原草稿'}); h.voice.toggle(); h.emit('error',{reason});
    assert.equal(h.voice.state.phase,'error'); assert.equal(h.voice.state.active,false);
    assert.equal(h.draft(),'原草稿'); assert.equal(h.timers.size,0);
    assert.ok(h.voice.state.message); assert.equal(h.emit('result',{text:'迟到'}),false);
  });
}

for (const reason of ['account','conversation','pagehide','hidden','quit','compact','navigation','memory','backend']) {
  test(`${reason} cancellation rejects old-turn callbacks and preserves user draft`,() => {
    const h = harness({draft:'正在写的草稿'}); h.voice.toggle(); const old = h.voice.state.turnId;
    h.emit('listening'); h.voice.cancel(reason);
    h.voice.toggle(); const next = h.voice.state.turnId; assert.notEqual(next,old);
    assert.equal(h.voice.handleNative({turn_id:old,state:'result',text:'旧对话机密'}),false);
    assert.equal(h.voice.handleNative({turn_id:old,state:'cancelled'}),false);
    assert.equal(h.voice.state.turnId,next); assert.equal(h.voice.state.phase,'authorizing');
    assert.equal(h.draft(),'正在写的草稿');
  });
}

test('native cancellation events preserve both existing and already recognized draft text',() => {
  const h = harness({draft:'已有'}); h.voice.toggle(); h.emit('cancelled',{reason:'hidden'});
  assert.equal(h.voice.state.phase,'idle'); assert.equal(h.draft(),'已有');
  h.voice.toggle(); h.emit('result',{text:'识别结果'}); h.voice.cancel('compact');
  assert.equal(h.draft(),'已有\n识别结果');
});

test('busy or pending-approval guard blocks capture and fences in-flight final text',() => {
  const h = harness({draft:'草稿'}); h.block(true);
  assert.equal(h.voice.toggle(),false); assert.deepEqual(h.calls,[]);
  h.block(false); h.voice.toggle(); h.emit('listening'); h.block(true);
  assert.equal(h.emit('result',{text:'不能插入'}),false); assert.equal(h.draft(),'草稿');
  assert.equal(h.voice.state.phase,'idle');
  h.block(false); h.voice.toggle(); h.block(true); h.voice.updateAvailability();
  assert.equal(h.voice.state.active,false);
});

test('native listening timeout cancels capture and ignores later final results',() => {
  const h = harness(); h.voice.toggle(); h.emit('listening'); const turn = h.voice.state.turnId;
  assert.equal([...h.timers.values()][0].ms,37000); h.expire();
  assert.equal(h.voice.state.reason,'timeout'); assert.equal(h.voice.state.phase,'error');
  assert.deepEqual(h.calls.at(-1),{action:'cancelListening',turn_id:turn});
  assert.equal(h.voice.handleNative({turn_id:turn,state:'result',text:'超时后'}),false);
});

test('final processing timeout also releases the turn',() => {
  const h = harness(); h.voice.toggle(); h.emit('listening'); h.voice.finish();
  assert.equal([...h.timers.values()][0].ms,8000); h.expire();
  assert.equal(h.voice.state.active,false); assert.equal(h.timers.size,0);
});

test('browser recognition waits for a visible provider consent step',() => {
  const h = harness({desktop:false}); h.voice.toggle();
  assert.equal(h.voice.state.phase,'consent'); assert.equal(h.recognizers.length,0);
  assert.equal(h.timers.size,0); h.voice.allowBrowser(h.voice.state.turnId);
  assert.equal(h.voice.state.phase,'authorizing'); assert.equal(h.recognizers.length,1);
  assert.equal(h.recognizers[0].started,1); assert.equal(h.recognizers[0].lang,'zh-CN');
  assert.deepEqual(h.calls,[]);
});

test('browser cancelled consent never constructs a recognizer or requests audio',() => {
  const h = harness({desktop:false}); h.voice.toggle(); h.voice.cancel();
  assert.equal(h.voice.allowBrowser(h.voice.state.turnId),false); assert.equal(h.recognizers.length,0);
});

for (const reason of ['account','conversation','hidden','pagehide']) {
  test(`browser ${reason} invalidates provider consent and old confirmation callbacks`,() => {
    const h = harness({desktop:false}); h.voice.toggle(); const oldId = h.voice.state.turnId;
    h.voice.allowBrowser(oldId); assert.equal(h.recognizers.length,1);
    h.voice.cancel(reason); h.voice.toggle();
    assert.equal(h.voice.state.phase,'consent'); assert.equal(h.recognizers.length,1);
    assert.equal(h.voice.allowBrowser(oldId),false); assert.equal(h.recognizers.length,1);
    assert.equal(h.voice.allowBrowser(h.voice.state.turnId),true); assert.equal(h.recognizers.length,2);
  });
}

test('browser final result is a snapshot, ignores interim revisions and never sends a task',() => {
  const h = harness({desktop:false,draft:'原草稿'}); h.voice.toggle(); h.voice.allowBrowser(h.voice.state.turnId);
  const recognition = h.recognizers[0]; recognition.onstart();
  const interim = [{transcript:'删'}]; interim.isFinal = false;
  recognition.onresult({results:[interim],resultIndex:0}); assert.equal(h.draft(),'原草稿');
  const final = [{transcript:'删除这句话里的错别字'}]; final.isFinal = true;
  recognition.onresult({results:[final],resultIndex:0});
  assert.equal(h.draft(),'原草稿\n删除这句话里的错别字'); assert.equal(h.voice.state.phase,'review');
  assert.equal(recognition.aborted,1); assert.deepEqual(h.calls,[]);
  recognition.onresult({results:[final]}); assert.equal(h.draft(),'原草稿\n删除这句话里的错别字');
});

test('browser final from an old recognizer cannot affect a new native-independent turn',() => {
  const h = harness({desktop:false}); h.voice.toggle(); h.voice.allowBrowser(h.voice.state.turnId);
  const old = h.recognizers[0]; old.onstart(); h.voice.cancel('conversation');
  h.voice.toggle(); h.voice.allowBrowser(h.voice.state.turnId); const current = h.recognizers[1];
  old.onresult({results:[[{transcript:'旧内容'}]]}); old.onerror({error:'not-allowed'}); old.onend();
  assert.equal(h.draft(),''); assert.equal(h.voice.state.phase,'authorizing');
  current.onstart(); current.onresult({results:[[{transcript:'新内容'}]]});
  assert.equal(h.draft(),'新内容');
});

test('browser permission failure and synchronous start errors preserve a usable editor',() => {
  const h = harness({desktop:false,draft:'保留'}); h.voice.toggle(); h.voice.allowBrowser(h.voice.state.turnId);
  h.recognizers[0].onerror({error:'not-allowed'});
  assert.equal(h.voice.state.phase,'error'); assert.equal(h.draft(),'保留');
  const broken = harness({desktop:false,createRecognition:() => {throw new Error('not supported');}});
  broken.voice.toggle(); broken.voice.allowBrowser(broken.voice.state.turnId); assert.equal(broken.voice.state.phase,'error');
});

test('browser finish only stops capture and final text still requires explicit send elsewhere',() => {
  const h = harness({desktop:false}); h.voice.toggle(); h.voice.allowBrowser(h.voice.state.turnId);
  const recognition = h.recognizers[0]; recognition.onstart(); h.voice.toggle();
  assert.equal(recognition.stopped,1); assert.equal(h.voice.state.phase,'processing');
  assert.equal(h.draft(),''); assert.deepEqual(h.calls,[]);
  recognition.onresult({results:[[{transcript:'确认后发送的文字'}]]});
  assert.equal(h.voice.state.phase,'review'); assert.deepEqual(h.calls,[]);
});
