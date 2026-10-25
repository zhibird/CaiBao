// The public receipt records this API tool loop. It is not a projection of
// preferences, streamed tool arguments, private history, or external task success.
const COUNTERS = Object.freeze([
  'tool_budget', 'tool_calls_used', 'tool_calls_succeeded', 'tool_calls_failed',
  'tool_calls_rejected', 'tool_calls_skipped', 'pending_tool_calls', 'omitted_tool_calls',
]);
const ENDINGS = Object.freeze({
  in_progress:'仍在处理中，工具结果可能还未返回。',
  model_finished:'模型已结束本轮回复。',
  tool_budget:'已达到本轮工具调用上限。',
  duplicate_guard:'重复调用保护已停止后续步骤。',
  iteration_limit:'已达到本轮处理轮数上限。',
  waiting_confirmation:'正在等待确认，对应操作尚未执行。',
  cancelled:'已停止后续步骤；已开始的操作可能仍会结束。',
  interrupted:'运行已中断；已开始的操作结果仍需核对。',
  execution_error:'本轮执行遇到问题；已开始的操作结果仍需核对。',
  invalid_checkpoint:'执行记录未通过校验，已停止继续处理。',
});
const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : null;
const count = value => Number.isSafeInteger(value) && value >= 0;

export function receiptSummary(run) {
  const receipt = record(run?.execution_receipt);
  if (!receipt || receipt.version !== 1 || receipt.source !== 'api_tool_loop'
      || !COUNTERS.every(key => Object.hasOwn(receipt,key) && count(receipt[key]))) {
    return {
      recorded:false, usage:'本次未记录 API 工具回执。', outcomes:'', pending:'', ending:'',
      note:'无法据此核对工具预算与调用结果。', needsReview:false,
    };
  }
  const failed = receipt.tool_calls_failed, rejected = receipt.tool_calls_rejected;
  const omitted = receipt.omitted_tool_calls, pending = receipt.pending_tool_calls;
  const incomplete = receipt.incomplete === true;
  // A reservation is not proof that a tool has (or has not) started. Only an
  // approval wait permits saying that its pending operations are unexecuted.
  const terminal = ['completed','partial_success','failed','cancelled','interrupted'].includes(run?.status);
  const stopReason = ['cancelled','interrupted'].includes(run?.status) ? run.status
    : run?.status === 'failed' && ['in_progress','waiting_confirmation'].includes(receipt.stop_reason) ? 'execution_error'
    : receipt.stop_reason;
  const uncertain = terminal || ['cancelled','interrupted','execution_error','invalid_checkpoint'].includes(stopReason);
  const waiting = !uncertain && (run?.status === 'requires_confirmation' || stopReason === 'waiting_confirmation');
  const inProgress = !uncertain && !waiting && (['running','queued'].includes(run?.status) || stopReason === 'in_progress');
  const pendingLabel = waiting ? '待确认' : inProgress ? '结果待返回' : '结果未确认';
  const notes = [];
  if (failed) notes.push('有工具返回失败');
  if (rejected) notes.push('有调用被拒绝');
  if (omitted) notes.push('有调用尚未执行');
  if (notes.length) notes.push('请核对回复及实际结果');
  else if (incomplete && !pending) notes.push('执行步骤尚未完整返回，请核对回复及实际结果');
  if (receipt.tool_calls_skipped) notes.push('预演只返回计划，未执行对应操作');
  if (pending) notes.push(waiting ? '待确认的操作尚未执行'
    : inProgress ? '已预约的调用结果仍待返回'
    : '部分调用结果未确认；已开始的操作可能仍会结束，请核对实际结果');
  if (!notes.length) notes.push('回执记录工具返回，外部结果请结合回复核对');
  return {
    recorded:true,
    usage:`工具调用 ${receipt.tool_calls_used} / ${receipt.tool_budget} 次`,
    outcomes:`返回成功 ${receipt.tool_calls_succeeded} · 返回失败 ${failed} · 拒绝 ${rejected} · 预演 ${receipt.tool_calls_skipped}`,
    pending:`${pendingLabel} ${pending} · 未执行 ${omitted}`,
    ending:typeof stopReason === 'string' && Object.hasOwn(ENDINGS,stopReason) ? ENDINGS[stopReason] : '结束原因未记录。',
    note:notes.join('；') + '。',
    needsReview:failed > 0 || rejected > 0 || omitted > 0 || incomplete,
  };
}
