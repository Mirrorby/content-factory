"""One-off Fast recovery for the missing October 12 Maya video."""
import json
from production import Ledger, render, send_for_review
from review_telegram import read_json
from daily_production import retryable_audio_failure
def main():
    ledger=Ledger();store=ledger.store
    store.put('lock.json',b'{}',IfNoneMatch='*')
    try:
        state=ledger.read();save=lambda:ledger.save(state)
        char=state['characters']['maya']
        new_id='daily-maya-20261012-01-fast'
        job=next((j for j in char['jobs'] if j['id']==new_id),None)
        if not job:
            old=next(j for j in char['jobs'] if j['id']=='daily-maya-20261012-01-a2')
            prefix='content-factory/v1/'+old['id']+'/'
            if old['status']!='rendering' or any(d['status']!='waiting' for d in old['deliveries'].values()):
                raise RuntimeError('Existing video is not eligible for recovery.')
            if not retryable_audio_failure(read_json(store,prefix+'state.json') or {},
                                           read_json(store,prefix+'intro.mp4.response.json') or {}):
                raise RuntimeError('Provider has not confirmed an uncharged audio failure.')
            job=json.loads(json.dumps(old))
            job.update(id=new_id,status='planned',replaces=old['id'],fast_recovery=True)
            job.pop('generation_error',None)
            job['spec']['actor_model']='veo-3.1-fast-generate-preview'
            job['spec']['audio_recovery']=True
            job['spec']['actor_asset']=None
            old['status']='generation_failed'
            char.setdefault('archive',[]).append(old)
            char['jobs'][char['jobs'].index(old)]=job
            save()
        if job['status']=='ready':send_for_review(job,save)
        else:render(ledger,{},job,save)
        print('Missing daily video delivered for Telegram approval.')
    finally:
        store.client.delete_object(Bucket=store.bucket,Key=store.prefix+'lock.json')
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Fast recovery stopped: '+(str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__))
        raise SystemExit(1)
