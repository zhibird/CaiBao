// Presentation states are derived from actual execution events, never model thoughts.
export const initialState = () => ({activity: 'idle', emotion: 'idle', label: '在这里，陪着你', tool: null});
export function reduceEvent(state, event) {
  const payload = event.payload || {};
  switch (event.event) {
    case 'run.started': return {...state, activity: 'thinking', emotion: 'focused', label: '想一想，怎么做好', tool: null};
    case 'llm.delta': return {...state, activity: 'thinking', label: '正在组织回答'};
    case 'tool.started': return {...state, activity: 'working', emotion: 'focused', label: `正在使用 ${payload.tool_name || '工具'}`, tool: payload.tool_name};
    case 'tool.result': return {...state, activity: 'thinking', label: '工具结果收到了', tool: null};
    case 'confirmation.required': return {...state, activity: 'waiting', emotion: 'curious', label: '这个操作，需要你点头'};
    case 'run.completed': return payload.status === 'requires_confirmation'
      ? {...state, activity: 'waiting', emotion: 'curious', label: '等你确认这个操作'}
      : payload.status === 'cancelled' ? {...state, activity:'idle', emotion:'idle', label:'好，这次先停在这里', tool:null}
      : {...state, activity: ['failed','partial_success','interrupted'].includes(payload.status) ? 'error' : 'idle', emotion: ['failed','partial_success','interrupted'].includes(payload.status) ? 'concerned' : 'happy', label: ['failed','partial_success','interrupted'].includes(payload.status) ? '有些步骤没完成，看看回复' : '好啦，我说完了', tool: null};
    case 'run.cancelled': return {...state, activity: 'idle', emotion: 'idle', label: '好，这次先停在这里', tool: null};
    case 'run.interrupted': return {...state, activity: 'error', emotion: 'concerned', label: '任务中断了，先看看留下的记录', tool: null};
    case 'run.failed': return {...state, activity: 'error', emotion: 'concerned', label: '遇到一点问题，我们再试试', tool: null};
    default: return state;
  }
}
// Incremental SSE parser handles UTF-8 decoding upstream, CRLF, split frames, comments.
export function createSSEParser(onEvent) {
  let pending = '';
  return {
    push(chunk) {
      pending += chunk;
      let match;
      while ((match = /\r?\n\r?\n/.exec(pending))) {
        const frame = pending.slice(0, match.index);
        pending = pending.slice(match.index + match[0].length);
        const lines = frame.split(/\r?\n/);
        const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!data) continue;
        const parsed = JSON.parse(data);
        if (!parsed.event) parsed.event = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
        if (!parsed.seq) {
          const id = Number(lines.find(line => line.startsWith('id:'))?.slice(3).trim());
          if (Number.isSafeInteger(id) && id > 0) parsed.seq = id;
        }
        onEvent(parsed);
      }
    },
  };
}
export function speechText(text) {
  return String(text).replace(/```[\s\S]*?```/g, '代码在聊天里。').replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[#*_`>]/g, '').slice(0, 1800);
}
