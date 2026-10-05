# Halal news watcher

Always-on service that pushes phone alerts (via ntfy) within ~1 minute when a
halal stock gets takeover news, big headlines, or moves 8%+.

Runs weekdays 06:00-22:30 UK. Halal list = SP Funds SPUS Shariah holdings
(refreshed daily) + your own stocks, minus BDS boycott targets.

Env: NTFY_TOPIC (required), MOVE_PCT (default 8).
Status: GET / . Test push: GET /test?topic=<NTFY_TOPIC>
