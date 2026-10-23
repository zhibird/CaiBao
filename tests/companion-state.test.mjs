import test from 'node:test';
import assert from 'node:assert/strict';
import {initialState, reduceEvent, createSSEParser, speechText} from '../app/web/companion-state.mjs';

test('real runtime events drive attention, tools, approvals and completion', () => {
  let state = reduceEvent(initialState(),{event:'run.started'});
  assert.equal(state.activity,'thinking');
  state=reduceEvent(state,{event:'tool.started',payload:{tool_name:'web_search'}});
  assert.equal(state.activity,'working'); assert.equal(state.tool,'web_search');
  state=reduceEvent(state,{event:'tool.result',payload:{tool_name:'web_search'}});
  assert.equal(state.tool,null);
  state=reduceEvent(state,{event:'confirmation.required'}); assert.equal(state.activity,'waiting');
  state=reduceEvent(state,{event:'run.completed',payload:{status:'requires_confirmation'}});
  assert.equal(state.activity,'waiting');
  state=reduceEvent(state,{event:'run.completed',payload:{status:'completed'}});
  assert.equal(state.activity,'idle'); assert.equal(state.emotion,'happy');
});
test('failures are never presented as a successful happy pet',()=>{
  for(const event of [{event:'run.failed'},{event:'run.completed',payload:{status:'failed'}}]){
    const state=reduceEvent(initialState(),event);
    assert.equal(state.activity,'error');assert.equal(state.emotion,'concerned');
  }
});
test('SSE accepts arbitrary UTF-8 byte boundaries, CRLF and multiple frames',()=>{
  const source=': heartbeat\r\n\r\nevent: llm.delta\r\ndata: {"payload":{"text":"彩宝"},"run_id":"r"}\r\n\r\ndata: {"event":"run.completed","payload":{"status":"completed"}}\n\n';
  const bytes=new TextEncoder().encode(source);
  const events=[], parser=createSSEParser(event=>events.push(event)), decoder=new TextDecoder();
  for(const byte of bytes) parser.push(decoder.decode(new Uint8Array([byte]),{stream:true}));
  parser.push(decoder.decode());
  assert.equal(events.length,2);assert.equal(events[0].payload.text,'彩宝');assert.equal(events[0].event,'llm.delta');
});
test('TTS removes code and visual markup, bounds playback length',()=>{
  assert.equal(speechText('## 你好 **彩宝** [网页](https://example.com)\n```sh\nrm -rf x\n```'),' 你好 彩宝 网页\n代码在聊天里。');
  assert.equal(speechText('好'.repeat(3000)).length,1800);
});

test('cancelled task returns to neutral presence rather than success celebration',()=>{
  const state=reduceEvent({...initialState(),activity:'working',tool:'web_search'}, {event:'run.cancelled'});
  assert.equal(state.activity,'idle');assert.equal(state.emotion,'idle');assert.equal(state.tool,null);
});
