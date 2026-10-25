// A model-authored checklist is presentation evidence, never execution proof.
const SOURCES = Object.freeze({
  'api.update_plan':'API 模型', 'codex.todo_list':'Codex',
  'claude.TodoWrite':'Claude Code', 'claude.tasks':'Claude Code',
});
const STATUSES = Object.freeze({pending:'待处理',in_progress:'模型标记进行中',completed:'模型标记完成',cancelled:'模型标记取消'});
const FIELDS = ['version','revision','source','model_reported','steps','explanation','limited'];
const STEP_FIELDS = ['id','content','status'];
const terminal = status => ['completed','partial_success','failed','cancelled','interrupted'].includes(status);
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactFields = (value,fields) => record(value) && Object.keys(value).length === fields.length
  && fields.every(key => Object.hasOwn(value,key));
const boundedText = (value,limit) => typeof value === 'string' && value.length > 0 && [...value].length <= limit && value.trim().length > 0;

export function parseTaskPlan(value) {
  if (!exactFields(value,FIELDS) || value.version !== 1 || !Number.isSafeInteger(value.revision)
      || value.revision < 1 || value.revision > 65 || typeof value.source !== 'string'
      || !Object.hasOwn(SOURCES,value.source) || value.model_reported !== true
      || typeof value.limited !== 'boolean' || value.limited !== (value.revision === 65)
      || !Array.isArray(value.steps) || value.steps.length > 10
      || !(value.explanation === null || typeof value.explanation === 'string' && [...value.explanation].length <= 300)) return null;
  const ids = new Set(),steps = [];
  let working = 0;
  for (const step of value.steps) {
    if (!exactFields(step,STEP_FIELDS) || !boundedText(step.id,64) || ids.has(step.id)
        || !boundedText(step.content,160) || typeof step.status !== 'string' || !Object.hasOwn(STATUSES,step.status)) return null;
    ids.add(step.id);
    if (step.status === 'in_progress' && ++working > 1) return null;
    steps.push(Object.freeze({id:step.id,content:step.content,status:step.status}));
  }
  return Object.freeze({version:1,revision:value.revision,source:value.source,model_reported:true,
    steps:Object.freeze(steps),explanation:value.explanation,limited:value.limited});
}

export function taskPlanSummary(run) {
  const plan = parseTaskPlan(run?.task_plan);
  if (!plan) return null;
  const complete = plan.steps.filter(step=>step.status === 'completed').length;
  return {
    revision:plan.revision,source:SOURCES[plan.source],limited:plan.limited,
    progress:plan.steps.length ? `模型标记完成 ${complete} / ${plan.steps.length} 项` : '模型已清空计划列表',
    explanation:plan.explanation || '',
    note:plan.limited ? '计划更新已达上限，已停止更新；下方保留最后记录。' : '步骤状态由模型报告，请结合执行回执和实际结果核对。',
    steps:plan.steps.map(step=>({id:step.id,content:step.content,status:step.status,label:STATUSES[step.status]})),
  };
}

// One tracker per observed run. Live plans only advance within their source;
// authoritative terminal GET snapshots also erase absent or invalid records.
export class TaskPlanTracker {
  constructor(run) {
    this.runId = run.run_id;
    this.plan = parseTaskPlan(run.task_plan);
    this.locked = terminal(run.status);
  }
  event(runId,value) {
    if (runId !== this.runId || this.locked || this.plan?.limited) return false;
    const next = parseTaskPlan(value);
    if (!next || this.plan && (next.source !== this.plan.source || next.revision <= this.plan.revision)) return false;
    this.plan = next; return true;
  }
  snapshot(run) {
    if (run.run_id !== this.runId) return this.plan;
    const next = parseTaskPlan(run.task_plan);
    if (terminal(run.status)) {
      this.plan = next; this.locked = true; return this.plan;
    }
    if (!this.locked && next && (!this.plan || next.source !== this.plan.source || next.revision >= this.plan.revision)) this.plan = next;
    return this.plan;
  }
  lock() { this.locked = true; }
}
