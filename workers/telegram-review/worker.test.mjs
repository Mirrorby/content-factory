import {test} from 'node:test';
import assert from 'node:assert/strict';
import {authorize,decide,label,deliver,acquireLease,renewLease,releaseLease,cleanupVideo,maintainPublication} from './worker.mjs';
const job = () => ({id:'daily-test-01',status:'ready',sha256:'hash',telegram_review:{sha256:'hash'},spec:{phrase:{hook:'Why?'}},
  deliveries:Object.fromEntries(['youtube','tiktok','instagram'].map(p=>[p,{status:'waiting',target_due_at:'2030-01-01T12:00:00Z'}]))});
const config={chat:'123',token:'456:secret',channels:{youtube:'y',tiktok:'t',instagram:'i'},organization_id:'org',buffer_key:'test'};
const receipt={status:'sent',chat_id:'123',bot_id:'456',message_id:7,video_sha256:'hash'};
const query={data:'cf:a:daily-test-01',from:{id:123},message:{message_id:7,chat:{id:123,type:'private'}}};
test('Owner, original message and exact hash are required',()=>{
  const state={characters:{maya:{jobs:[job()]}}};
  assert.ok(authorize(query,config,state,receipt));
  assert.equal(authorize({...query,from:{id:999}},config,state,receipt),null);
  assert.equal(authorize(query,config,state,{...receipt,message_id:8}),null);
  assert.equal(authorize(query,config,state,{...receipt,video_sha256:'different'}),null);
});

test('Expired leases recover atomically; old owner cannot release new owner',async()=>{
  let stored={value:{worker:true},etag:'old',uploaded:new Date(0)};let serial=0;
  const bucket={get:async()=>stored?{...stored,json:async()=>stored.value}:null,
    put:async(_key,body,options)=>{
      if(options.onlyIf.etagMatches!==undefined && options.onlyIf.etagMatches!==stored?.etag)return null;
      if(options.onlyIf.etagDoesNotMatch==='*' && stored)return null;
      stored={value:JSON.parse(body),etag:'v'+(++serial),uploaded:new Date()}; return {etag:stored.etag};
    }};
  const first=await acquireLease(bucket);assert.ok(first);
  assert.equal(await acquireLease(bucket),null);
  await renewLease(bucket,first);await releaseLease(bucket,first);
  const second=await acquireLease(bucket);assert.ok(second);
  await releaseLease(bucket,first);
  assert.equal(stored.value.owner,second.value.owner);
  await assert.rejects(renewLease(bucket,first),/Lease lost/);
});

test('Cleanup waits for all three sent receipts and 72 hours after last actual publication',async()=>{
  const j=job();j.id='daily-maya-20261001-01';j.video_key='content-factory/v1/'+j.id+'/final.mp4';
  j.public_key='videos/'+'a'.repeat(32)+'.mp4';
  decide(j,{decision:'approved',sha256:'hash',message_id:7,decided_at:'now'});
  for(const p of ['youtube','tiktok','instagram'])Object.assign(j.deliveries[p],{status:'sent',post_id:p,sent_at:new Date(Date.now()-4*86400000).toISOString()});
  const deleted=[];const env={PRIVATE:{delete:async keys=>deleted.push(...keys)},PUBLIC:{delete:async key=>deleted.push(key)}};
  j.deliveries.instagram.status='scheduled';assert.equal(await cleanupVideo(env,j,async()=>{}),false);
  j.deliveries.instagram.status='sent';j.deliveries.instagram.sent_at=new Date(Date.now()-2*86400000).toISOString();
  assert.equal(await cleanupVideo(env,j,async()=>{}),false);assert.equal(deleted.length,0);
  j.deliveries.instagram.sent_at=new Date(Date.now()-4*86400000).toISOString();
  assert.equal(await cleanupVideo(env,j,async()=>{}),true);assert.equal(deleted.length,4);
  assert.ok(deleted.every(k=>!k.includes('/assets/')));
  assert.equal(await cleanupVideo(env,j,async()=>{}),false);assert.equal(deleted.length,4);
});

test('Publication confirmation validates channel and uses Buffer sentAt',async()=>{
  const j=job();decide(j,{decision:'approved',sha256:'hash',message_id:7,decided_at:'now'});
  for(const p of ['youtube','tiktok','instagram'])Object.assign(j.deliveries[p],{status:'scheduled',post_id:config.channels[p],due_at:'2020-01-01T00:00:00Z'});
  const c={jobs:[j]};const original=globalThis.fetch;let calls=0;
  globalThis.fetch=async(_url,init)=>{
    calls++;const id=JSON.parse(init.body).variables.input.id;
    return Response.json({data:{post:{id,channelId:id,status:'sent',sentAt:'2020-01-01T01:00:00Z'}}});
  };
  try{
    await maintainPublication({},config,c,async()=>{});
    assert.equal(calls,3);assert.ok(Object.values(j.deliveries).every(d=>d.status==='sent' && d.sent_at==='2020-01-01T01:00:00.000Z'));
    await maintainPublication({},config,c,async()=>{});assert.equal(calls,3);
  }finally{globalThis.fetch=original;}
});
test('First decision wins; reject cannot be reversed',()=>{
  const j=job(); const d={decision:'rejected',sha256:'hash',message_id:7,decided_at:'now'};
  assert.equal(decide(j,d),true);
  assert.equal(decide(j,{...d,decision:'approved'}),false);
  assert.equal(j.status,'rejected'); assert.match(label(j),/Отклонено/);
});
test('Unapproved or uncertain deliveries never call Buffer',async()=>{
  const j=job(); const original=globalThis.fetch; let calls=0;
  globalThis.fetch=async()=>{calls++;throw Error('Unexpected');};
  try {
    await deliver({},config,j,async()=>{});
    decide(j,{decision:'approved',sha256:'hash',message_id:7,decided_at:'now'});
    j.deliveries.youtube.status='unknown';
    await deliver({},config,j,async()=>{});
    assert.equal(calls,0);
  } finally {globalThis.fetch=original;}
});
test('Three platforms scheduled once; unknown persisted before each mutation',async()=>{
  const j=job();j.media_ready=true;j.public_key='videos/test.mp4';
  decide(j,{decision:'approved',sha256:'hash',message_id:7,decided_at:'now'});
  const original=globalThis.fetch; const mutations=[]; const saved=[];
  globalThis.fetch=async(_url,init)=>{
    assert.equal(init.redirect,'manual', 'Workers supports manual redirects; HTTP failures still stop delivery');
    const body=JSON.parse(init.body);
    if(body.query.startsWith('query'))return Response.json({data:{posts:{edges:[],pageInfo:{hasNextPage:false}}}});
    const input=body.variables.input; const platform=Object.keys(input.metadata)[0];
    assert.equal(j.deliveries[platform].status,'unknown');
    assert.equal(saved.at(-1)[platform].status,'unknown');
    assert.equal(input.text,'Find Leela on Telegram: @leela_ru_bot');
    assert.equal(input.mode,'customScheduled'); mutations.push(input.channelId);
    return Response.json({data:{createPost:{post:{id:'post-'+platform,dueAt:input.dueAt}}}});
  };
  try {
    const save=async()=>saved.push(structuredClone(j.deliveries));
    await deliver({}, {...config,media_base:'https://example.com'},j,save);
    await deliver({},config,j,save);
    assert.equal(mutations.length,3); assert.equal(new Set(mutations).size,3);
    assert.equal(label(j),'✅ Отправлено в Buffer');
  } finally {globalThis.fetch=original;}
});
