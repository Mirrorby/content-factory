# Подключение YouTube

Код и workflow находятся в этом репозитории. Однократная авторизация выполняется на компьютере владельца канала.

1. Включить YouTube Data API v3 в Google Cloud.
2. Настроить Google Auth Platform, External, добавить свою почту в Test users.
3. Создать OAuth client типа Desktop app и скачать JSON. Не коммитить его.
4. Установить в локальное виртуальное окружение Python пакет google-auth-oauthlib==1.2.2.
5. Выполнить: `python scripts/authorize_youtube.py /path/to/client_secret.json`.
6. В браузере выбрать Google-аккаунт и нужный YouTube-канал, разрешить загрузку видео.
7. Содержимое созданного `~/content-factory-youtube-secret.json` сохранить в GitHub Secret `YOUTUBE_OAUTH_JSON`.
8. Запустить Actions → Publish existing video to YouTube → Run workflow → job_id pilot-001.

Загружается существующий final.mp4 из R2. Генерация и Gemini не запускаются. Тест всегда загружается с доступом «Личное», без уведомления подписчиков. Результат ищи в YouTube Studio; video_id сохраняется в R2 рядом с роликом в youtube-publication.json. В публичные логи токены и URL сессии не выводятся.

При статусе uploaded повторная загрузка пропускается. При pending/uploading повтор блокируется: сначала проверить YouTube Studio и сохранённую сессию. Автоматическое восстановление сессии пока не реализовано. Не удалять запись при неизвестном результате — это может создать дубликат.

OAuth в режиме Testing обычно выдаёт refresh token на 7 дней для этих разрешений. Для постоянной работы потребуется отдельно настроить production OAuth. Это не снимает ограничение YouTube на приватные загрузки из непроверенного API-проекта: публичный автопостинг требует аудита YouTube API либо проверенного сервиса.

Проверены синтаксис и имитационные сценарии: успешная загрузка, повтор после успеха, блокировка при неизвестном результате. Реальный тест требует подключения аккаунта. Instagram и TikTok пока не подключены.

Документация: https://developers.google.com/youtube/v3/guides/auth/installed-apps
