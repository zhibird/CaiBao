const blank = () => ({items:[], documents:[], loading:false, loadingMore:false, saving:false,
  hasMore:false, nextOffset:0, markdownEnabled:false, error:null, notice:null, uncertain:null, conflict:null});
const clone = value => structuredClone(value);
const unknownOutcome = error => !error.status || error.status >= 500 || [408, 429].includes(error.status);

// A memory write is always an explicit user action. Reads can refresh after an
// uncertain response; writes never retry, because the server may have saved it.
export class CompanionMemory {
  constructor({request, onChange = () => {}, limit = 50}) {
    Object.assign(this, {request, onChange, limit});
    this.generation = 0; this.readGeneration = 0; this.userId = null; this.conversationId = null;
    this.state = blank();
  }
  publish(patch = {}) {Object.assign(this.state, patch); this.onChange(clone(this.state));}
  setContext(userId, conversationId) {
    if (userId === this.userId && conversationId === this.conversationId) return;
    this.generation++; this.readGeneration++; this.userId = userId; this.conversationId = conversationId;
    this.state = blank(); this.publish();
  }
  dispose() {this.generation++; this.readGeneration++; this.userId = null; this.conversationId = null; this.state = blank(); this.publish();}
  async load({append = false} = {}) {
    if (!this.userId || !this.conversationId || append && (!this.state.hasMore || this.state.loadingMore)) return;
    const generation = this.generation, read = ++this.readGeneration;
    const offset = append ? this.state.nextOffset : 0;
    this.publish({[append ? 'loadingMore' : 'loading']:true, error:null});
    try {
      const page = await this.request(`/companion/memory?conversation_id=${encodeURIComponent(this.conversationId)}&offset=${offset}&limit=${this.limit}`);
      if (generation !== this.generation || read !== this.readGeneration) return;
      const items = new Map((append ? this.state.items : []).map(item => [item.memory_id,item]));
      for (const item of page.items || []) items.set(item.memory_id,item);
      this.publish({items:[...items.values()], documents:page.documents || [], hasMore:Boolean(page.has_more),
        nextOffset:page.next_offset ?? offset + (page.items || []).length, markdownEnabled:page.markdown_enabled === true,
        ...(this.state.uncertain ? {uncertain:{...this.state.uncertain,checked:true}} : {}),
        ...(this.state.conflict ? {conflict:{...this.state.conflict,reviewReady:(page.documents || []).some(doc => doc.kind === this.state.conflict.kind)}} : {})});
      return page;
    } catch (error) {if (generation === this.generation && read === this.readGeneration) this.publish({error:error.message});}
    finally {if (generation === this.generation && read === this.readGeneration) this.publish({loading:false,loadingMore:false});}
  }
  async write(path, method, body, {kind, memoryId, draft} = {}) {
    if (!this.userId || !this.conversationId) throw new Error('请先登录并打开一段对话。');
    if (this.state.saving || this.state.uncertain || this.state.conflict) return {ok:false,blocked:true};
    const generation = this.generation;
    const conversation = this.conversationId;
    this.publish({saving:true,error:null,notice:null});
    try {
      const result = await this.request(path, {method, ...(body ? {body:JSON.stringify({...body,conversation_id:conversation})} : {})});
      if (generation !== this.generation) return {ok:false,stale:true};
      this.publish({notice:method === 'DELETE' ? '已删除这条记忆。聊天和其他背景仍然保留。' : '已保存。后续 API 回答可按记忆开关参考。'});
      await this.load();
      if (generation !== this.generation) return {ok:false,stale:true};
      return {ok:true,item:result};
    } catch (error) {
      if (generation !== this.generation) return {ok:false,stale:true};
      if (error.status === 409 && kind) {
        this.publish({conflict:{kind,draft,reviewReady:false},error:'这份背景在别处更新过。已保留你的草稿，请核对最新正文后再决定。'});
        await this.load();
      } else if (unknownOutcome(error)) {
        this.publish({uncertain:{kind:kind || 'card',method,memoryId:memoryId || null,draft,checked:false},
          error:'保存结果还不能确认。请核对最新列表；彩宝不会自动重复这次操作。'});
        await this.load();
      } else this.publish({error:error.message});
      return {ok:false,error};
    } finally {if (generation === this.generation) this.publish({saving:false});}
  }
  saveCard({memory_id, title, content, category = 'preference', source_message_id, status}) {
    const body = {title,content,category,...(status ? {status} : {})};
    if (!memory_id && source_message_id) body.source_message_id = source_message_id;
    return this.write(`/companion/memory/cards${memory_id ? `/${encodeURIComponent(memory_id)}` : ''}`,
      memory_id ? 'PATCH' : 'POST',body,{memoryId:memory_id,draft:{...body}});
  }
  setStatus(item, status) {
    return this.write(`/companion/memory/cards/${encodeURIComponent(item.memory_id)}`,'PATCH',{status},
      {memoryId:item.memory_id,draft:{status}});
  }
  remove(item) {
    return this.write(`/companion/memory/cards/${encodeURIComponent(item.memory_id)}?conversation_id=${encodeURIComponent(this.conversationId)}`,
      'DELETE',null,{memoryId:item.memory_id,draft:{title:item.title}});
  }
  saveDocument({kind,content,expected_revision}) {
    return this.write(`/companion/memory/documents/${encodeURIComponent(kind)}`,'PUT',{content,expected_revision},
      {kind,draft:{content,expected_revision}});
  }
  reviewedUnknown() {if (!this.state.loading && this.state.uncertain?.checked) this.publish({uncertain:null,error:null,notice:'已核对。请确认草稿，再决定是否保存。'});}
  reviewedConflict() {if (!this.state.loading && this.state.conflict?.reviewReady) this.publish({conflict:null,error:null,notice:'请根据最新正文复核草稿，再明确保存。'});}
  discardConflict() {this.publish({conflict:null,error:null,notice:'已取消这份背景草稿，已保存的正文保持原样。'});}
}
