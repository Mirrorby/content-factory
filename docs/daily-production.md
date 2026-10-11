# Daily production

Production reserves two distinct Maya jobs for tomorrow by the Europe/Minsk calendar, using America/New_York publishing slots.
Generation/recovery checks are scheduled at minute 17 of each hour from 08:00 through 15:00 UTC (11:17 through 18:17 Minsk). GitHub cron can delay or drop runs; these are scheduled attempts, not a delivery-time guarantee. All attempts share the same two daily job IDs; completed media and Telegram receipts are reused.
The first pair can be started via workflow_dispatch with execute=true.
A rerun on the same Minsk date resumes the same two jobs without a new reservation.
Each job has its own photo-based actor generation, phrase and frozen per-channel
Buffer timestamp taken from that day’s two configured slots.

Every finished MP4 is sent to the owner’s private Telegram chat with approve/reject
buttons. The five-minute review workflow checks the owner, message and video hash.
Only approved files are copied to public media and scheduled with customScheduled.
Caption: Find Leela on Telegram: @leela_ru_bot

Late approvals are marked expired, occupied slots require inspection, full queues
are retried later, and unknown POST outcomes are never blindly retried. Rejected
videos do not publish and are not automatically replaced. Stored videos are retained.

The daily ledger is private R2 content-factory/production/v2/ledger.json.
The previous v1 ledger is preserved and never refilled by this daily flow.
Channel discovery remains in the private v1 namespace.
Required Maya PNG: content-factory/assets/leela/maya-reference.png in the private bucket.
Set repository variable PRODUCTION_ENABLED=false to pause scheduled generation.
No catch-up bulk generation is performed for missed calendar days.
