// Telegram decisions are durable before acknowledgement; only approved hashes publish.
const PREFIX = 'content-factory/production/v2/';
const LEDGER = PREFIX + 'ledger.json';
const LOCK = PREFIX + 'lock.json';
const DECISIONS = PREFIX + 'review-decisions/';
const PLATFORMS = ['youtube', 'tiktok', 'instagram'];
const CAPTION = 'Find Leela on Telegram: @leela_ru_bot';
const stamp = () => new Date().toISOString();
const read = async (bucket, key) => { const o = await bucket.get(key); return o ? o.json() : null; };
const write = (bucket, key, value) => bucket.put(key, JSON.stringify(value));

export async function acquireLease(bucket) {
  for (let attempt=0; attempt<3; attempt++) {
    const old=await bucket.get(LOCK);
    if (old) {
      const value=await old.json();
      const expires=value.expires_at ?? old.uploaded.getTime()/1000+(value.worker?180:21600);
      if (!Number.isFinite(expires) || expires>Date.now()/1000) return null;
    }
    const value={owner:crypto.randomUUID(),kind:'worker',created_at:Date.now()/1000,expires_at:Date.now()/1000+180};
    const result=await bucket.put(LOCK,JSON.stringify(value),{onlyIf:old?{etagMatches:old.etag}:{etagDoesNotMatch:'*'}});
    if (result) return {value,etag:result.etag};
  }
  return null;
}
export async function renewLease(bucket,lease) {
  if (lease.value.expires_at<=Date.now()/1000) throw new Error('Lease expired');
  const value={...lease.value,expires_at:Date.now()/1000+180};
  const result=await bucket.put(LOCK,JSON.stringify(value),{onlyIf:{etagMatches:lease.etag}});
  if (!result) throw new Error('Lease lost');
  Object.assign(lease,{value,etag:result.etag});
}
export async function releaseLease(bucket,lease) {
  // CAS tombstone: never unconditionally delete another owner's lease.
  await bucket.put(LOCK,JSON.stringify({...lease.value,expires_at:0,released:true}),{onlyIf:{etagMatches:lease.etag}});
}

export async function cleanupVideo(env,job,save) {
  if (job.media_deleted_at || job.status!=='ready' || job.review?.decision!=='approved') return false;
  if (PLATFORMS.some(p=>job.deliveries[p]?.status!=='sent' || !job.deliveries[p].post_id ||
      !Number.isFinite(Date.parse(job.deliveries[p].sent_at)))) return false;
  const latest=Math.max(...PLATFORMS.map(p=>Date.parse(job.deliveries[p].sent_at)));
  if (latest>Date.now() || Date.now()<latest+3*86400000) return false;
  if (!/^daily-[a-z][a-z0-9-]*-\d{8}-[A-Za-z0-9-]+$/.test(job.id) ||
      job.video_key!=='content-factory/v1/'+job.id+'/final.mp4' ||
      (job.public_key && !/^videos\/[a-f0-9]{32}\.mp4$/.test(job.public_key))) throw new Error('Cleanup key validation failed');
  job.cleanup_started_at ??= stamp(); await save();
  if (job.public_key) await env.PUBLIC.delete(job.public_key);
  const prefix='content-factory/v1/'+job.id+'/';
  await env.PRIVATE.delete(['final.mp4','intro.mp4','outro.mp4'].map(name=>prefix+name));
  job.media_deleted_at=stamp(); job.cleanup_after=new Date(latest+3*86400000).toISOString(); await save();
  return true;
}

export async function maintainPublication(env,c,character,save) {
  // One job per hour: at most 72 status requests/day, independent of ledger size.
  if (Date.parse(character.next_publication_check_at || '1970-01-01')>Date.now()) return;
  character.next_publication_check_at=new Date(Date.now()+3600000).toISOString(); await save();
  const job=character.jobs.find(j=>j.status==='ready' && !j.media_deleted_at && j.review?.decision==='approved' &&
    PLATFORMS.every(p=>['scheduled','sent'].includes(j.deliveries[p]?.status) && j.deliveries[p].post_id) &&
    PLATFORMS.some(p=>j.deliveries[p].status!=='sent' || !j.deliveries[p].sent_at) &&
    PLATFORMS.every(p=>Date.parse(j.deliveries[p].due_at || j.deliveries[p].target_due_at)<=Date.now()) &&
    Date.parse(j.next_publication_check_at || '1970-01-01')<=Date.now());
  if (!job) return;
  job.next_publication_check_at=new Date(Date.now()+86400000).toISOString(); await save();
  try {
    for (const p of PLATFORMS) {
      const d=job.deliveries[p]; if(d.status==='sent' && d.sent_at) continue;
      const data=await graph(c,`query($input: PostInput!) { post(input:$input) { id status channelId sentAt } }`,{input:{id:d.post_id}});
      const post=data.post;
      if (!post || post.id!==d.post_id || post.channelId!==c.channels[p]) throw new Error('Publication receipt mismatch');
      if (post.status==='sent') {
        const sent=Date.parse(post.sentAt);
        if (!Number.isFinite(sent) || sent>Date.now()) throw new Error('Publication timestamp missing');
        Object.assign(d,{status:'sent',sent_at:new Date(sent).toISOString(),confirmed_at:stamp()}); await save();
      }
    }
    delete job.publication_error;
  } catch { job.publication_error='PublicationCheckFailed'; }
  await save();
}

export function authorize(q, config, state, receipt) {
  const match = /^cf:([ar]):([A-Za-z0-9][A-Za-z0-9_-]{0,54})$/.exec(q?.data || '');
  const m = q?.message;
  if (!match || String(q.from?.id) !== config.chat || String(m?.chat?.id) !== config.chat || m.chat.type !== 'private') return null;
  const jobs = Object.values(state.characters).flatMap(c => c.jobs).filter(j => j.id === match[2]);
  if (jobs.length !== 1) return null;
  const job = jobs[0];
  if (!['ready', 'rejected'].includes(job.status) || !job.sha256 || receipt?.status !== 'sent' ||
      receipt.chat_id !== config.chat || receipt.bot_id !== config.token.split(':')[0] ||
      receipt.message_id !== m.message_id || receipt.video_sha256 !== job.sha256) return null;
  return {job, action:match[1], receipt};
}

export function decide(job, decision) {
  if (job.review?.decision || decision.sha256 !== job.sha256) return false;
  job.telegram_review = {sha256:job.sha256, sent_at:decision.decided_at};
  job.review = {decision:decision.decision, decided_at:decision.decided_at, message_id:decision.message_id};
  if (decision.decision === 'approved') job.review.approved_sha256 = job.sha256;
  else {
    job.status = 'rejected';
    for (const d of Object.values(job.deliveries)) if (d.status === 'waiting') d.status = 'rejected';
  }
  return true;
}

export function label(job) {
  if (job.review?.decision === 'rejected') return '❌ Отклонено — не публикуется';
  const statuses = Object.values(job.deliveries).map(d => d.status);
  if (statuses.every(s => ['scheduled','sent'].includes(s))) return '✅ Отправлено в Buffer';
  if (statuses.some(s => ['unknown','error'].includes(s))) return '⚠️ Нужна проверка отправки';
  if (statuses.includes('expired')) return '⚠️ Срок публикации прошёл — нужен перенос';
  if (statuses.includes('slot_conflict')) return '⚠️ Слот занят — нужна проверка';
  if (job.review?.delivery_error) return '⚠️ Одобрено — ошибка отправки';
  return '✅ Одобрено — ожидает отправки';
}

async function api(url, data, headers = {}) {
  const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json',...headers},
    body:JSON.stringify(data), redirect:'manual', signal:AbortSignal.timeout(20000)});
  if (!response.ok) throw new Error('ProviderHTTP_' + response.status);
  return response.json();
}
async function telegram(c, method, data) {
  const r = await api('https://api.telegram.org/bot'+c.token+'/'+method, data);
  if (!r.ok) throw new Error('TelegramAPI_' + r.error_code);
  return r.result;
}
async function keyboard(c, job, text) {
  await telegram(c, 'editMessageReplyMarkup', {chat_id:c.chat, message_id:job.review.message_id,
    reply_markup:{inline_keyboard:[[{text,callback_data:'cf:done:'+job.id}]]}});
}
async function graph(c, query, variables) {
  const r = await api('https://api.buffer.com', {query,variables}, {Authorization:'Bearer '+c.buffer_key});
  if (r.errors || !r.data) throw new Error('Buffer GraphQL failure');
  return r.data;
}
async function preflight(c) {
  const data = await graph(c, `query($input: ChannelsInput!) { channels(input:$input) {
    id service timezone organizationId isDisconnected isLocked isQueuePaused postingSchedule { day paused times }
  } }`, {input:{organizationId:c.organization_id}});
  for (const p of PLATFORMS) {
    const ch = data.channels.find(ch => ch.id === c.channels[p]);
    if (!ch || ch.service !== p || ch.organizationId !== c.organization_id || ch.timezone !== 'America/New_York' ||
        ch.isDisconnected || ch.isLocked || ch.isQueuePaused || ch.postingSchedule.length !== 7 ||
        new Set(ch.postingSchedule.map(d=>d.day)).size !== 7 ||
        ch.postingSchedule.some(d=>d.paused || d.times.length !== 2 || new Set(d.times).size !== 2)) throw new Error('Channel validation failed');
  }
}
async function queue(c, channel) {
  const result = []; let after = null;
  for (let pageNo = 0; pageNo < 20; pageNo++) {
    const data = await graph(c, `query($input: PostsInput!, $after: String) {
      posts(first:100, after:$after, input:$input) { edges { node { id channelId status dueAt } }
        pageInfo { hasNextPage endCursor } }
    }`, {input:{organizationId:c.organization_id,filter:{channelIds:[channel],status:['scheduled','sending','error','needs_approval','draft']}},after});
    result.push(...data.posts.edges.map(e=>e.node));
    if (!data.posts.pageInfo.hasNextPage) return result;
    const cursor = data.posts.pageInfo.endCursor;
    if (!cursor || cursor === after) throw new Error('Queue pagination failed');
    after = cursor;
  }
  throw new Error('Queue pagination budget exceeded');
}
async function publicVideo(env, c, job, save) {
  if (!job.public_key) { job.public_key = 'videos/'+crypto.randomUUID().replaceAll('-','')+'.mp4'; await save(); }
  if (!job.media_ready) {
    if (job.video_key !== 'content-factory/v1/'+job.id+'/final.mp4') throw new Error('Unexpected video key');
    const source = await env.PRIVATE.get(job.video_key);
    if (!source || source.size <= 0 || source.size > 50000000) throw new Error('Video unavailable');
    await env.PUBLIC.put(job.public_key, source.body, {httpMetadata:{contentType:'video/mp4',cacheControl:'public, max-age=31536000, immutable'},customMetadata:{sha256:job.sha256}});
    const response = await fetch(c.media_base+'/'+job.public_key,{method:'HEAD',redirect:'manual',signal:AbortSignal.timeout(15000)});
    if (!response.ok || !response.headers.get('content-type')?.includes('video/mp4')) throw new Error('Public video unavailable');
    job.media_ready = true; await save();
  }
  return c.media_base+'/'+job.public_key;
}
export async function deliver(env, c, job, save) {
  if (job.status !== 'ready' || job.review?.decision !== 'approved' || job.review.approved_sha256 !== job.sha256 || job.telegram_review?.sha256 !== job.sha256) return;
  if (Object.values(job.deliveries).some(d=>['unknown','error'].includes(d.status))) return;
  for (const platform of PLATFORMS) {
    const d = job.deliveries[platform]; if (d.status !== 'waiting') continue;
    const due = Date.parse(d.target_due_at);
    if (!Number.isFinite(due)) throw new Error('Missing slot');
    if (due <= Date.now()) { d.status = 'expired'; await save(); continue; }
    const remote = await queue(c,c.channels[platform]);
    if (remote.length >= 10) continue;
    if (remote.some(p=>Date.parse(p.dueAt) === due)) { d.status = 'slot_conflict'; await save(); continue; }
    const url = await publicVideo(env,c,job,save);
    const metadata = {youtube:{title:job.spec.phrase.hook.slice(0,100),categoryId:'22',privacy:'public',madeForKids:false,isAiGenerated:true},
      instagram:{type:'reel',shouldShareToFeed:true,isAiGenerated:true},tiktok:{isAiGenerated:true}};
    Object.assign(d,{status:'unknown',attempted_at:stamp(),channel_id:c.channels[platform]}); await save();
    const data = await graph(c, `mutation($input: CreatePostInput!) { createPost(input:$input) {
      ... on PostActionSuccess { post { id dueAt } } ... on MutationError { message } } }`,
      {input:{text:CAPTION,channelId:c.channels[platform],schedulingType:'automatic',mode:'customScheduled',dueAt:d.target_due_at,
        needsApproval:false,aiAssisted:true,assets:[{video:{url}}],metadata:{[platform]:metadata[platform]}}});
    const post = data.createPost.post;
    if (!post?.id) throw new Error('Buffer creation unconfirmed');
    Object.assign(d,{status:'scheduled',post_id:post.id,due_at:post.dueAt}); await save();
  }
}

export async function drain(env) {
  const c = JSON.parse(env.CONFIG);
  const lease = await acquireLease(env.PRIVATE);
  if (!lease) return;
  try {
    const state = await read(env.PRIVATE,LEDGER); if (!state) return;
    const character = state.characters.maya; if (!character) return;
    const save = async () => {await renewLease(env.PRIVATE,lease); await write(env.PRIVATE,LEDGER,state);};
    if (JSON.stringify(Object.entries(character.channels).sort()) !== JSON.stringify(Object.entries(c.channels).sort())) throw new Error('Channel mapping changed');
    let checkedChannels = false;
    let attemptedDelivery=false;
    const started=Date.now();
    for (const job of character.jobs) {
      if (Date.now()-started>20000) break;
      // Closed jobs no longer consume a decision-object read every minute.
      const decision = !job.review?.decision && ['ready','rejected'].includes(job.status)
        ? await read(env.PRIVATE,DECISIONS+job.id+'.json') : null;
      if (decision && decide(job,decision)) await save();
      if (!job.review?.decision) continue;
      if (!attemptedDelivery && job.review.decision === 'approved' && Object.values(job.deliveries).some(d=>d.status === 'waiting') &&
          !Object.values(job.deliveries).some(d=>['unknown','error'].includes(d.status)) &&
          (job.review.delivery_error === 'WorkerDeliveryError' && job.review.delivery_diagnostics_version !== 3 || Date.parse(job.review.next_delivery_check_at || '1970-01-01') <= Date.now())) {
        job.review.delivery_diagnostics_version=3;
        attemptedDelivery=true;
        job.review.next_delivery_check_at = new Date(Date.now()+6*3600000).toISOString(); await save();
        try { if (!checkedChannels) { await preflight(c); checkedChannels=true; } await deliver(env,c,job,save); delete job.review.delivery_error; }
        catch (e) { job.review.delivery_diagnostic={name:e.name,message:String(e.message).replaceAll(c.token,'[secret]').replaceAll(c.buffer_key,'[secret]').replace(/https?:\/\/[^\s]+/g,'[url]').slice(0,300)}; job.review.delivery_error=/^(ProviderHTTP_\d+|Buffer GraphQL failure|Channel validation failed|Public video unavailable|Video unavailable)$/.test(e.message) ? e.message : 'WorkerDeliveryError'; }
        await save();
      }
      const text = label(job);
      if (job.review.displayed_label !== text) {
        try { await keyboard(c,job,text); job.review.displayed_label=text; await save(); } catch (e) { job.review.label_error=/^TelegramAPI_\d+$/.test(e.message)?e.message:'LabelUpdateError'; await save(); }
      }
      await cleanupVideo(env,job,save);
    }
    if (!attemptedDelivery && Date.now()-started<10000) await maintainPublication(env,c,character,save);
  } finally { await releaseLease(env.PRIVATE,lease); }
}

export default {
  async fetch(request,env) {
    const path = new URL(request.url).pathname;
    if (path === '/health' && request.method === 'GET') return Response.json({ok:true});
    try {
      const c = JSON.parse(env.CONFIG);
      const a = new TextEncoder().encode(request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '');
      const b = new TextEncoder().encode(c.webhook_secret);
      if (a.length !== b.length || !crypto.subtle.timingSafeEqual(a,b)) return new Response('Forbidden',{status:403});
      if (path === '/drain' && request.method === 'POST') { await drain(env); return Response.json({ok:true}); }
      if (path !== '/telegram' || request.method !== 'POST') return new Response('Not found',{status:404});
      if (Number(request.headers.get('content-length')) > 65536) return new Response('Too large',{status:413});
      const reader=request.body.getReader(); const chunks=[]; let size=0;
      while (true) { const {done,value}=await reader.read(); if(done)break; size+=value.byteLength; if(size>65536){await reader.cancel();return new Response('Too large',{status:413});} chunks.push(value); }
      const bytes=new Uint8Array(size); let offset=0; for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
      const update=JSON.parse(new TextDecoder().decode(bytes)); const q=update.callback_query;
      if (!q) return Response.json({ok:true});
      const state=await read(env.PRIVATE,LEDGER);
      const id=/^cf:[ar]:([A-Za-z0-9][A-Za-z0-9_-]{0,54})$/.exec(q.data || '')?.[1];
      const receipt=id?await read(env.PRIVATE,'content-factory/v1/'+id+'/telegram-delivery.json'):null;
      const auth=state?authorize(q,c,state,receipt):null;
      if (!auth) return Response.json({ok:true});
      const {job,action}=auth;
      const decision={decision:action==='a'?'approved':'rejected',sha256:job.sha256,decided_at:stamp(),message_id:receipt.message_id};
      await env.PRIVATE.put(DECISIONS+job.id+'.json',JSON.stringify(decision),{onlyIf:{etagDoesNotMatch:'*'}});
      const saved=job.review?.decision?job.review:await read(env.PRIVATE,DECISIONS+job.id+'.json');
      const text=saved.decision==='rejected'?'❌ Отклонено — не публикуется':job.review?.decision?label(job):'✅ Одобрено — ожидает отправки';
      try { await telegram(c,'answerCallbackQuery',{callback_query_id:q.id,text:'Решение сохранено'}); } catch { /* decision already durable */ }
      try { await keyboard(c,{id:job.id,review:{message_id:receipt.message_id}},text); } catch { /* cron repairs */ }
      return Response.json({ok:true});
    } catch { return new Response('Temporarily unavailable',{status:503}); }
  },
  async scheduled(_event,env,ctx) { ctx.waitUntil(drain(env).catch(()=>console.log('Review drain postponed'))); }
};
