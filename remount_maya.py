"""Remount the reviewed Maya clip without another provider request."""
import copy
import json
from production import Ledger, render, send_for_review
from review_telegram import Telegram, read_json

def main():
    ledger = Ledger()
    store = ledger.store
    store.put('lock.json', b'{}', IfNoneMatch='*')
    try:
        state = ledger.read()
        save = lambda: ledger.save(state)
        character = state['characters']['maya']
        old_id = 'daily-maya-20261011-02'
        new_id = old_id + '-r2'
        job = next((j for j in character['jobs'] if j['id'] == new_id), None)
        source = read_json(store, 'content-factory/v1/'+old_id+'/state.json')
        if not source or source['status'] != 'ready':
            raise RuntimeError('Saved actor source is not ready.')
        asset = dict(key='content-factory/v1/'+old_id+'/intro.mp4',
                     sha256=source['clips'][0]['sha256'],
                     reference_sha256=source['reference_sha256'])
        if not job:
            old = next(j for j in character['jobs'] if j['id']==old_id)
            if any(d['status'] not in ('waiting','rejected') for d in old['deliveries'].values()):
                raise RuntimeError('Old video already has a delivery receipt; inspect Buffer first.')
            job = {k:copy.deepcopy(old[k]) for k in ('spec','target_date','created_at')}
            job.update(id=new_id, status='planned', replaces=old_id,
                deliveries={p:dict(status='waiting',target_due_at=d['target_due_at'])
                            for p,d in old['deliveries'].items()})
            old['status']='superseded'
            character.setdefault('archive',[]).append(old)
            character['jobs'][character['jobs'].index(old)]=job
            save()
            receipt=read_json(store,'content-factory/v1/'+old_id+'/telegram-delivery.json')
            if receipt and receipt.get('message_id'):
                Telegram().label(receipt['message_id'], old_id, '♻️ Заменено новой версией')
        if job['status']=='ready':
            send_for_review(job,save)
        else:
            render(ledger, {'actor_asset':asset}, job, save)
        print('Remounted actor first, gameplay last; sent for a new Telegram review.')
    finally:
        store.client.delete_object(Bucket=store.bucket,Key=store.prefix+'lock.json')

if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('Remount stopped: '+(str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__))
        raise SystemExit(1)
