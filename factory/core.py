#!/usr/bin/env python3
"""Generation engine. Run through run.py for durable R2 checkpoints."""

import base64
import fcntl
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
def getpass(_prompt):
    return os.environ.get('GEMINI_API_KEY', '').strip()
from pathlib import Path

REPO = Path(os.environ.get('ENGINE_DIR', 'vendor/MoneyPrinterTurbo')).resolve()
EXPECTED_COMMIT = '8e259e9f072c9e08464f040cd658d4eb046a57d0'
JOB_ID = os.environ.get('JOB_ID', 'pilot-001')
MODEL = 'gemini-3.1-flash-lite-image'
MAX_REQUESTS = 3  # Общий лимит POST для этого задания, включая неудачные.
SCRIPT = (
    'Иногда перед нами несколько путей. '
    'Чтобы сделать следующий шаг, полезно остановиться '
    'и спросить себя: что для меня сейчас действительно важно?'
)
STYLE = (
    'Create one photorealistic cinematic vertical image, 9:16. '
    'Quiet forest, soft warm morning light, muted green and golden colors, '
    'natural proportions, calm thoughtful atmosphere, no fantasy effects. '
    'Keep the lower quarter visually simple and uncluttered for subtitles. '
    'No text, letters, logos, watermark graphics or collage. '
)
SCENES = [
    STYLE + 'Wide view of a person from behind standing at a fork in a forest path. '
    'Two paths are clearly visible leading in different directions. '
    'The person is small and in the middle distance.',
    STYLE + 'Close view of a single hiking boot resting on a forest path, '
    'as if its wearer has paused before taking a step. '
    'Ground-level camera, gentle depth of field, no visible face.',
    STYLE + 'An empty forest path leading towards a sunlit clearing. '
    'Eye-level view, simple composition, inviting natural light, no people.',
]
OLD_IMAGE = REPO / 'storage/local_videos/gemini-test-1790526786.png'
OLD_SETTINGS = REPO / 'storage/tasks/24189096-7d95-46f6-ac50-7eb1f6be5a80/script.json'


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def save_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def image_blocks(value):
    if isinstance(value, dict):
        if value.get('type') == 'image' and value.get('data'):
            yield value['data']
        for child in value.values():
            yield from image_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from image_blocks(child)


def write_png(raw, path):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as picture:
        picture.load()
        if min(picture.size) < 256:
            raise RuntimeError('API вернул слишком маленькое изображение.')
        output = io.BytesIO()
        picture.convert('RGB').save(output, format='PNG')
    atomic_bytes(path, output.getvalue())


def probe(path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_streams', '-show_format',
         '-of', 'json', str(path)], capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError('Готовое видео не читается ffprobe.')
    data = json.loads(result.stdout)
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    duration = float(data.get('format', {}).get('duration', 0))
    if not video or not audio or duration < 5:
        raise RuntimeError('В результате нет видео, звука или достаточной длительности.')
    if (video.get('width'), video.get('height')) != (1080, 1920):
        raise RuntimeError('Неожиданный размер готового ролика.')
    return {'duration': duration, 'width': video['width'], 'height': video['height']}


WORKER = r'''
import json, sys, shutil, uuid
from pathlib import Path
from app.utils import utils, file_security
from app.models.schema import VideoParams
from app.services import task
from app.config import config

mode, settings_file, task_id, result_file = sys.argv[1:]
params = VideoParams(**json.loads(Path(settings_file).read_text()))
# Публикация в этой версии явно выключена только в текущем процессе.
config.app['upload_post_auto_upload'] = False
assert task.upload_post.upload_post_service.auto_upload is False
if mode == 'check':
    print('Параметры MoneyPrinterTurbo проверены.', flush=True)
else:
    # MPT accepts local materials only inside its dedicated local_videos root.
    # Keep durable source paths/state unchanged; stage copies for this render.
    allowed_root = Path(utils.storage_dir("local_videos", create=True)).resolve()
    render_dir = allowed_root / str(uuid.UUID(task_id))
    render_dir.mkdir(parents=True, exist_ok=True)
    for index, material in enumerate(params.video_materials or [], start=1):
        source = Path(material.url)
        target = render_dir / f"scene-{index:02}{source.suffix.lower()}"
        shutil.copyfile(source, target)
        material.url = file_security.resolve_path_within_directory(
            str(allowed_root), str(target)
        )
    print('Материалы скопированы в папку монтажа.', flush=True)
    result = task.start(task_id, params, stop_at='video')
    if not isinstance(result, dict) or not result.get('videos'):
        raise RuntimeError('MoneyPrinterTurbo не вернул готовый ролик. См. журнал.')
    target = Path(result_file)
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    temp.replace(target)
    print('MPT_RESULT_OK', flush=True)
'''


def run_worker(job, mode, task_id):
    log_path = job / ('preflight.log' if mode == 'check' else 'render.log')
    command = [str(REPO / '.venv/bin/python'), '-u', '-c', WORKER, mode,
               str(job / 'params.json'), task_id, str(job / 'result.json')]
    started = time.monotonic()
    with log_path.open('a', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=REPO, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                try:
                    code = process.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    print(f'  {mode}: прошло {time.monotonic() - started:.0f} сек.', flush=True)
                    limit = 120 if mode == 'check' else 7200
                    if time.monotonic() - started >= limit:
                        raise RuntimeError('Превышен лимит времени процесса.')
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
    if code:
        print(log_path.read_text(encoding='utf-8', errors='replace')[-3500:])
        raise RuntimeError(f'Ошибка MoneyPrinterTurbo. Журнал: {log_path}')


def get_settings(paths):
    # Переносим только оформление из рабочего теста, остальные поля задаём явно.
    settings = {}
    if OLD_SETTINGS.is_file():
        old = read_json(OLD_SETTINGS).get('params', {})
        for key in ('font_name', 'font_size', 'text_fore_color',
                    'text_background_color', 'stroke_color', 'stroke_width',
                    'subtitle_position', 'custom_position',
                    'subtitle_display_mode', 'subtitle_animation',
                    'rounded_subtitle_background', 'n_threads'):
            if key in old:
                settings[key] = old[key]
    else:
        font = REPO / 'resource/fonts/DejaVuSans-Bold.ttf'
        if not font.is_file():
            raise RuntimeError('Не найдены прежние настройки и шрифт теста. Пришли этот вывод.')
        settings.update(font_name=font.name, font_size=60,
                        text_fore_color='#FFFFFF', stroke_color='#000000', stroke_width=2)
    settings.update(
        video_subject='Технический тест: три сцены о выборе пути',
        video_script=SCRIPT, video_source='local', video_terms='',
        video_materials=[{'provider': 'local', 'url': str(p)} for p in paths],
        video_count=1, video_aspect='9:16', video_fit_mode='cover',
        video_concat_mode='sequential', video_transition_mode=None,
        video_clip_duration=4, video_clip_speed=1.0,
        match_materials_to_script=False, custom_audio_file='',
        voice_name='ru-RU-SvetlanaNeural', voice_rate=1.0, voice_volume=1.0,
        subtitle_enabled=True, bgm_type='', bgm_file='', bgm_volume=0,
    )
    return settings


def prepare_scene(index, job, state, key_holder):
    """Один POST максимум на сцену. Неопределённые запросы не повторяются."""
    import requests
    from PIL import Image
    entry = state['scenes'][index]
    target = job / f'scene-{index + 1:02}.png'
    response_path = job / f'scene-{index + 1:02}-response.json'
    state_path = job / 'state.json'

    if entry['status'] == 'ready':
        if not target.is_file() or digest(target) != entry['sha256']:
            raise RuntimeError(f'Кадр {index + 1} потерян или изменён. Автоперегенерация выключена.')
        with Image.open(target) as picture:
            picture.verify()
        print(f'Кадр {index + 1}/3: использую сохранённый.', flush=True)
        return

    if response_path.is_file():
        # Восстановление после остановки между сохранением ответа и картинки.
        response = read_json(response_path)
    elif entry['status'] != 'new':
        raise RuntimeError(
            f'Кадр {index + 1}: предыдущий запрос имеет статус {entry["status"]}. '
            'Новый платный запрос не отправлен. Пришли этот вывод для проверки.'
        )
    elif index == 0 and OLD_IMAGE.is_file():
        write_png(OLD_IMAGE.read_bytes(), target)
        entry.update(status='ready', sha256=digest(target), source=str(OLD_IMAGE))
        save_json(state_path, state)
        print('Кадр 1/3: взят из прошлого теста, без запроса Gemini.', flush=True)
        return
    else:
        if state['requests_sent'] >= MAX_REQUESTS:
            raise RuntimeError('Достигнут лимит платных запросов этого задания.')
        if not key_holder:
            api_key = getpass('Gemini API-ключ (не сохраняется в файлы): ').strip()
            if not api_key:
                raise RuntimeError('Ключ не введён.')
            key_holder.append(api_key)
        # Сначала фиксируем факт попытки. Даже после аварии запрос не повторится.
        entry.update(status='pending', started_at=time.time())
        state['requests_sent'] += 1
        save_json(state_path, state)
        print(f'Кадр {index + 1}/3: запрос Gemini...', flush=True)
        try:
            response_http = requests.post(
                'https://generativelanguage.googleapis.com/v1beta/interactions',
                headers={'x-goog-api-key': key_holder[0]},
                json={'model': MODEL, 'input': SCENES[index],
                      'response_format': {'type': 'image', 'aspect_ratio': '9:16',
                                          'image_size': '1K'}},
                timeout=(30, 240), allow_redirects=False,
            )
        except requests.RequestException:
            entry['status'] = 'unknown'
            save_json(state_path, state)
            raise RuntimeError('Ответ Gemini не получен. Запрос не будет повторён автоматически.') from None
        if response_http.status_code != 200:
            entry.update(status='http_error', http_status=response_http.status_code)
            save_json(state_path, state)
            raise RuntimeError(f'Gemini HTTP {response_http.status_code}. Автоповтора нет.')
        # Сохраняем полный ответ до разбора; ключ в JSON запроса не передавался.
        atomic_bytes(response_path, response_http.content)
        response = read_json(response_path)

    encoded = next(image_blocks(response), None)
    if not encoded:
        entry.update(status='no_image', interaction_id=response.get('id'))
        save_json(state_path, state)
        raise RuntimeError('В ответе Gemini нет изображения. Ответ сохранён; новый запрос не отправлен.')
    write_png(base64.b64decode(encoded, validate=True), target)
    entry.update(status='ready', sha256=digest(target), interaction_id=response.get('id'),
                 usage=response.get('usage'), source=MODEL)
    save_json(state_path, state)
    print(f'Кадр {index + 1}/3: сохранён.', flush=True)


def main():
    if not (REPO / '.venv/bin/python').is_file():
        raise RuntimeError('Не найдена среда MoneyPrinterTurbo. Проверь установку зависимостей.')
    for program in ('git', 'ffmpeg', 'ffprobe'):
        if not shutil.which(program):
            raise RuntimeError(f'Не найден {program}. Платные запросы не выполнялись.')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    if commit != EXPECTED_COMMIT:
        raise RuntimeError(f'Версия MoneyPrinterTurbo изменилась: {commit}. Сначала проверим совместимость.')
    import requests  # Проверка зависимостей до первого запроса.
    from PIL import Image

    job = REPO / 'storage/content_factory' / JOB_ID
    job.mkdir(parents=True, exist_ok=True)
    # flock освобождается ОС при завершении процесса; оставшийся файл не блокирует рестарт.
    with (job / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Это задание уже выполняется. Дождись первого запуска.') from None
        paths = [job / f'scene-{i + 1:02}.png' for i in range(3)]
        params_path = job / 'params.json'
        settings = read_json(params_path) if params_path.is_file() else get_settings(paths)
        settings['video_materials'] = [{'provider': 'local', 'url': str(p)} for p in paths]
        spec = {'model': MODEL, 'script': SCRIPT, 'scenes': SCENES, 'settings': settings,
                'max_requests': MAX_REQUESTS, 'commit': EXPECTED_COMMIT}
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).replace(str(REPO), '<ENGINE>').encode()).hexdigest()
        state_path = job / 'state.json'
        if state_path.exists():
            state = read_json(state_path)
            if state['fingerprint'] != fingerprint:
                raise RuntimeError('Настройки этого задания изменились. Не смешиваем их с оплаченными кадрами.')
        else:
            state = {'fingerprint': fingerprint, 'status': 'new', 'requests_sent': 0,
                     'scenes': [{'status': 'new'} for _ in SCENES], 'render_attempts': []}
            save_json(params_path, settings)
            save_json(job / 'spec.json', spec)
            save_json(state_path, state)

        final = job / 'final.mp4'
        if state['status'] == 'ready':
            if not final.is_file() or digest(final) != state['video_sha256']:
                raise RuntimeError('Готовый ролик потерян или изменён. Пришли этот вывод.')
            probe(final)
            print(f'Уже готово, повторных запросов нет.\nВидео: {final}')
            return

        save_json(params_path, settings)
        print('1/3 Проверяю MoneyPrinterTurbo...', flush=True)
        run_worker(job, 'check', 'preflight')
        print('2/3 Подготавливаю три кадра. Всего не более трёх запросов Gemini.', flush=True)
        key_holder = []
        try:
            for index in range(3):
                prepare_scene(index, job, state, key_holder)
        finally:
            key_holder.clear()

        result_path = job / 'result.json'
        if not result_path.is_file():
            task_id = str(uuid.uuid4())
            state['status'] = 'rendering'
            state['render_attempts'].append(task_id)
            save_json(state_path, state)
            print('3/3 Озвучка, три сцены и субтитры. Сборка может занять несколько минут.', flush=True)
            run_worker(job, 'render', task_id)
        result = read_json(result_path)
        source_video = Path(result['videos'][0])
        info = probe(source_video)
        subtitle = Path(result['subtitle_path']) if result.get('subtitle_path') else None
        if not subtitle or not subtitle.is_file() or not subtitle.stat().st_size:
            raise RuntimeError('Нет файла субтитров. Требуется проверка журнала.')
        temp = final.with_suffix('.tmp.mp4')
        shutil.copyfile(source_video, temp)
        os.replace(temp, final)
        shutil.copyfile(subtitle, job / 'subtitle.srt')
        state.update(status='ready', video_sha256=digest(final), video_info=info)
        save_json(state_path, state)
        print(f'\nГОТОВО\nВидео: {final}\nДлительность: {info["duration"]:.2f} сек.\n'
              f'Запросов Gemini за всё задание: {state["requests_sent"]}\n'
              f'Прогресс и материалы: {job}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nОстановлено. Сохранённые кадры остаются; запросы с неизвестным результатом не повторяются.')
    except Exception as error:
        print(f'\nОСТАНОВЛЕНО: {error}', flush=True)
        print('Скопируй этот вывод в чат. Не удаляй state.json и не меняй JOB_ID для повторной попытки.')
        raise SystemExit(1)
