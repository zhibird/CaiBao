import test from 'node:test';
import assert from 'node:assert/strict';
import {CompanionMemory} from '../app/web/companion-memory.mjs';

const card = (id, extra = {}) => ({memory_id:id,title:`fact ${id}`,content:'a lasting preference',category:'preference',status:'active',...extra});
const document = (revision = 'one', content = 'old context') => ({kind:'long_term',label:'长期背景',revision,content,enabled:true,prompt_limit:4000});
const page = (items = [], extra = {}) => ({items,documents:[document()],has_more:false,next_offset:items.length,markdown_enabled:true,...extra});
const error = (message, status) => Object.assign(new Error(message),{status});
const deferred = () => {let resolve, reject; const promise = new Promise((yes,no) => {resolve = yes; reject = no;}); return {promise,resolve,reject};};
function setup(request, options = {}) {
  const snapshots = [];
  const memory = new CompanionMemory({request,onChange:state => snapshots.push(state),...options});
  memory.setContext('alice','conversation one'); return {memory,snapshots};
}

test('pages are scoped to the active conversation, merge by memory id and keep source text as data',async () => {
  const calls = [];
  const {memory} = setup(async path => {
    calls.push(path);
    return calls.length === 1 ? page([card('one',{source:{request_preview:'<img onerror=alert(1)>',available:true}})],{has_more:true,next_offset:1}) : page([card('one'),card('two')],{next_offset:3});
  },{limit:1});
  await memory.load(); assert.equal(memory.state.items[0].source.request_preview,'<img onerror=alert(1)>');
  await memory.load({append:true});
  assert.deepEqual(memory.state.items.map(item => item.memory_id),['one','two']);
  assert.equal(calls[0],'/companion/memory?conversation_id=conversation%20one&offset=0&limit=1');
  assert.match(calls[1],/offset=1&limit=1$/);
});

test('create, edit, pause, enable and delete require one explicit write each followed only by a read',async () => {
  const calls = [];
  const {memory} = setup(async (path,options = {}) => {
    calls.push({path,...options}); return options.method ? card('one') : page([card('one')]);
  });
  assert.equal((await memory.saveCard({title:'tea',content:'I like green tea',source_message_id:'history-id'})).ok,true);
  await memory.saveCard({memory_id:'one',title:'tea time',content:'only in the morning',source_message_id:'do-not-rebind'});
  await memory.setStatus(card('one'),'disabled'); await memory.setStatus(card('one'),'active'); await memory.remove(card('one'));
  const writes = calls.filter(call => call.method);
  assert.deepEqual(writes.map(call => call.method),['POST','PATCH','PATCH','PATCH','DELETE']);
  assert.deepEqual(JSON.parse(writes[0].body),{title:'tea',content:'I like green tea',category:'preference',source_message_id:'history-id',conversation_id:'conversation one'});
  assert.equal(JSON.parse(writes[1].body).source_message_id,undefined);
  assert.equal(JSON.parse(writes[2].body).status,'disabled');
  assert.equal(writes[4].path,'/companion/memory/cards/one?conversation_id=conversation%20one');
  assert.equal(calls.filter(call => !call.method).length,5);
});

test('validation rejection keeps the draft unsaved and does not manufacture a success',async () => {
  const {memory} = setup(async () => {throw error('Title is required',400);});
  const result = await memory.saveCard({title:'',content:'body'});
  assert.equal(result.ok,false); assert.equal(memory.state.notice,null); assert.equal(memory.state.uncertain,null);
  assert.equal(memory.state.error,'Title is required'); assert.equal(memory.state.saving,false);
});

test('unknown create refreshes for review and cannot be retried automatically or by a double click',async () => {
  let writes = 0;
  const {memory} = setup(async (_path,options = {}) => {
    if (options.method) {writes++; throw new TypeError('response lost');}
    return page([card('server-saved',{title:'tea'})]);
  });
  const result = await memory.saveCard({title:'tea',content:'body'});
  assert.equal(result.ok,false); assert.equal(writes,1); assert.equal(memory.state.notice,null);
  assert.equal(memory.state.uncertain.checked,true); assert.equal(memory.state.items[0].memory_id,'server-saved');
  assert.deepEqual(await memory.saveCard({title:'tea',content:'body'}),{ok:false,blocked:true}); assert.equal(writes,1);
  memory.reviewedUnknown(); assert.equal(memory.state.uncertain,null);
});

test('a failed read after an uncertain write cannot unlock another save',async () => {
  const {memory} = setup(async () => {throw new TypeError('offline');});
  await memory.saveCard({title:'tea',content:'body'}); memory.reviewedUnknown();
  assert.equal(memory.state.uncertain.checked,false);
  assert.deepEqual(await memory.saveCard({title:'tea',content:'body'}),{ok:false,blocked:true});
});

test('409 preserves background draft, fetches latest revision and needs an explicit review',async () => {
  const calls = [];
  const {memory} = setup(async (path,options = {}) => {
    calls.push({path,...options});
    if (options.method) throw error('revision changed',409);
    return page([],{documents:[document('two','someone else edited')]});
  });
  await memory.saveDocument({kind:'long_term',content:'my draft',expected_revision:'one'});
  assert.equal(memory.state.conflict.draft.content,'my draft'); assert.equal(memory.state.conflict.reviewReady,true);
  assert.equal(memory.state.documents[0].revision,'two');
  await memory.saveDocument({kind:'long_term',content:'my draft',expected_revision:'one'});
  assert.equal(calls.filter(call => call.method).length,1);
  memory.reviewedConflict(); assert.equal(memory.state.conflict,null);
});

test('conflict review stays blocked if the latest background cannot be read',async () => {
  const {memory} = setup(async (_path,options = {}) => {throw options.method ? error('conflict',409) : error('not available',503);});
  await memory.saveDocument({kind:'long_term',content:'draft',expected_revision:'one'});
  memory.reviewedConflict(); assert.equal(memory.state.conflict.reviewReady,false);
  memory.discardConflict(); assert.equal(memory.state.conflict,null,'explicitly abandoning the draft must not trap all future memory edits');
});

test('background clear is an explicit PUT with an empty body and its reviewed revision',async () => {
  const calls = [];
  const {memory} = setup(async (path,options = {}) => {calls.push({path,...options}); return options.method ? document('two','') : page();});
  assert.equal((await memory.saveDocument({kind:'long_term',content:'',expected_revision:'one'})).ok,true);
  assert.equal(calls[0].path,'/companion/memory/documents/long_term');
  assert.deepEqual(JSON.parse(calls[0].body),{content:'',expected_revision:'one',conversation_id:'conversation one'});
});

test('account switch invalidates a pending read and exposes no previous identity snapshot',async () => {
  const old = deferred(); let calls = 0;
  const {memory,snapshots} = setup(async () => ++calls === 1 ? old.promise : page([card('bob')]));
  const pending = memory.load(); memory.setContext('bob','other-conversation');
  await memory.load(); const marker = snapshots.length;
  old.resolve(page([card('alice-secret')])); await pending;
  assert.deepEqual(memory.state.items.map(item => item.memory_id),['bob']); assert.equal(snapshots.length,marker);
});

test('new conversation and logout ignore late write results without publishing old data',async () => {
  const old = deferred(); const calls = [];
  const {memory,snapshots} = setup(async (path,options = {}) => {calls.push({path,...options}); return old.promise;});
  const pending = memory.saveCard({title:'old private title',content:'private'});
  memory.setContext('alice','new-conversation'); memory.dispose(); const marker = snapshots.length;
  old.resolve(card('late-secret')); assert.deepEqual(await pending,{ok:false,stale:true});
  assert.equal(snapshots.length,marker); assert.deepEqual(memory.state.items,[]); assert.equal(memory.state.notice,null);
  assert.equal(calls.length,1);
});

test('parallel save clicks cannot cause duplicate writes while the first response is pending',async () => {
  const pending = deferred(); let writes = 0;
  const {memory} = setup(async (_path,options = {}) => options.method ? (++writes,pending.promise) : page());
  const first = memory.saveCard({title:'one',content:'body'});
  assert.deepEqual(await memory.saveCard({title:'one',content:'body'}),{ok:false,blocked:true});
  pending.resolve(card('one')); await first; assert.equal(writes,1);
});
