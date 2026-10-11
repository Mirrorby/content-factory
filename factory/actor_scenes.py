"""Scene snapshots and one checkpointed identity-based first frame per video."""
import base64
import copy
import json

import requests
from factory import core
from render_gameplay import read_remote

IMAGE_MODEL = 'gemini-3.1-flash-lite-image'


def select_scene(character, state):
    scenes = character.get('scenes', [])
    if not scenes:
        return None
    ids = [s['id'] for s in scenes]
    if len(ids) < 2 or len(ids) != len(set(ids)):
        raise RuntimeError('Scene pool requires at least two distinct IDs.')
    if any(not all(s.get(k) for k in ('setting', 'outfit', 'framing', 'reaction')) for s in scenes):
        raise RuntimeError('Every scene requires setting, outfit, framing and reaction.')
    usage = state.setdefault('scene_usage', {})
    scene = min((s for s in scenes if s['id'] != state.get('last_scene')),
                key=lambda s: (usage.get(s['id'], 0), ids.index(s['id'])))
    usage[scene['id']] = usage.get(scene['id'], 0) + 1
    state['last_scene'] = scene['id']
    return copy.deepcopy(scene)


def scene_description(scene):
    return ' '.join(scene[k] for k in ('setting', 'outfit', 'framing'))


def video_prompt(scene):
    return ('One continuous eight-second vertical smartphone video of the same fictional adult woman '
            'shown in the supplied first frame. Keep her face, age, hair, outfit and setting consistent '
            'with that first frame. ' + scene_description(scene) + ' ' + scene['reaction'] + ' '
            'During the first three seconds she says only "No way!" in natural American English, '
            'with an expressive warm voice synchronized to her lips. Finish the complete phrase '
            'naturally, then she smiles and glances at her phone. Quiet natural ambience. '
            'One shot, no additional words, narration, music, text or logos.')


def scene_reference(store, job, state, reference, scene, key):
    """Persist before POST; an uncertain request is never sent twice."""
    target = job / 'scene-reference.png'
    item = state.setdefault('scene_image', {'status': 'new', 'model': IMAGE_MODEL})
    def save():
        store.put('state.json', json.dumps(state).encode())
    if item['status'] == 'ready':
        store.client.download_file(store.bucket, store.prefix + target.name, str(target))
        if core.digest(target) != item['sha256']:
            raise RuntimeError('Scene reference checksum mismatch.')
        return target
    response = read_remote(store, 'scene-reference.response.json')
    if response is None:
        if item['status'] != 'new':
            raise RuntimeError('Previous scene image result unresolved; no new paid request sent.')
        prompt = ('Create a new photorealistic 9:16 portrait photograph of the exact same fictional '
                  'adult woman from the input portrait. Use the input for identity only: preserve '
                  'facial proportions, eyes, nose, lips, skin tone, age and dark brown hair. '
                  'Replace the original room, outfit and framing with this scene: '
                  + scene_description(scene) + ' Her face is clearly visible, mouth relaxed and closed. '
                  'Natural skin texture, realistic anatomy, one woman, no other foreground people. '
                  'Fill the entire vertical frame without borders, text, logos or collage.')
        item['status'] = 'pending'
        save()
        response_http = requests.post('https://generativelanguage.googleapis.com/v1beta/interactions',
            headers={'x-goog-api-key': key}, json={'model': item['model'], 'input': [
                {'type': 'text', 'text': prompt}, {'type': 'image', 'mime_type': 'image/png',
                 'data': base64.b64encode(reference.read_bytes()).decode()}],
                'response_format': {'type': 'image', 'aspect_ratio': '9:16', 'image_size': '1K'}},
            timeout=(30, 240), allow_redirects=False)
        if response_http.status_code != 200:
            item.update(status='http_error', http_status=response_http.status_code)
            save()
            raise RuntimeError('Scene image API failed; no automatic paid retry.')
        response = response_http.json()
        store.put('scene-reference.response.json', json.dumps(response).encode())
    encoded = response.get('output_image', {}).get('data') or next(core.image_blocks(response), None)
    if not encoded:
        item['status'] = 'no_image'
        save()
        raise RuntimeError('Scene image missing; response saved privately.')
    core.write_png(base64.b64decode(encoded, validate=True), target)
    from PIL import Image
    with Image.open(target) as image:
        width, height = image.size
    if abs(width / height - 9 / 16) > .03:
        item['status'] = 'invalid_framing'
        save()
        raise RuntimeError('Scene image must be portrait 9:16; no video request sent.')
    store.upload_file(target)
    item.update(status='ready', sha256=core.digest(target))
    save()
    return target
