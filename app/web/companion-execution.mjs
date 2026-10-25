// Execution records describe that run. Preferences only describe the next send.
const BACKENDS = Object.freeze({claude_code:'Claude Code', codex:'Codex', pi:'Pi'});
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
const text = value => typeof value === 'string' ? value.trim().slice(0,160) : '';
const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : null;

export function executionSummary(run) {
  const identity = record(run?.execution_identity);
  const known = identity && (identity.kind === 'api' || identity.kind === 'cli');
  if (!known) return {identity:'尚无运行记录', context:'尚无对话衔接记录', recorded:false};

  const cli = identity.kind === 'cli';
  const backend = cli ? (Object.hasOwn(BACKENDS,identity.backend) ? BACKENDS[identity.backend] : 'CLI') : 'API';
  const model = cli && (identity.model_source === 'runtime_default' || !text(identity.model))
    ? '由 CLI 自选' : text(identity.model) || '模型未记录';
  const context = record(run?.context_snapshot);
  const result = {identity:`${backend} · ${model}`, context:'尚无对话衔接记录', recorded:true};
  if (!context || context.version !== 1 || context.source !== 'caibao.chat_history') return result;

  const details = [];
  if (context.continuity === 'native_resume' && cli) {
    details.push('接回原 CLI 会话');
  } else if (context.continuity === 'fresh') {
    const reset = {
      binding_changed:'执行设置变了，已重新装入对话',
      legacy:'旧会话缺少衔接记录，已重新装入对话',
      uncertain:'上次衔接未确认，已重新装入对话',
    };
    details.push(Object.hasOwn(reset,context.reset_reason) ? reset[context.reset_reason] : '已重新装入对话');
  } else if (context.continuity === 'api_history') {
    if (context.history_enabled !== false) details.push('已装入这次对话');
  } else {
    return result;
  }
  const selected = count(context.selected_count), omitted = count(context.omitted_count), truncated = count(context.truncated_count);
  if (context.history_enabled === false) details.push('本轮未补充聊天记录');
  else details.push(selected ? `补充 ${selected} 轮聊天` : '本轮没有新增聊天需要补充');
  if (omitted) details.push(`略去 ${omitted} 轮`);
  if (truncated) details.push(`${truncated} 轮过长内容已截短`);
  result.context = details.join(' · ');
  return result;
}
