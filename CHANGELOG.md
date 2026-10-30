# Changelog

Every push to `main` that passes the tests publishes `ghcr.io/sergiolin5/ashur-core`
and `ashur-node` as `:latest`, as `:<core version>` and as `:<commit>`. ZimaOS runs
`:latest`. To go back to an earlier version, set the image to that version's tag
(for example `ashur-core:0.5.0`); data in `/DATA/AppData/ashur` is not touched.

| Version | Date | What changed |
| --- | --- | --- |
| 0.8.1 | 2026-10-30 | ZimaOS dashboard can open the panel (framing allowed on the tailnet door only); panel says when the core takes over 10 s; `ashur probar` times health and the panel page |
| 0.8.0 | 2026-10-29 | Readable log: one line at start (or exactly why it can't start), one line per event, no HTTP noise. `ashur` command in the app's terminal |
| 0.7.2 | 2026-10-29 | Sign-in says clearly when the key is wrong, when the public door is locked after failed attempts, or when the core can't be reached |
| 0.7.1 | 2026-10-29 | `/health` says which door a request came through (`tailnet` or `public`); API docs turned off |
| 0.7.0 | 2026-10-28 | Public door for the panel (port 8712, for Tailscale Funnel): panel and /api only, lockout after repeated wrong keys |
| 0.6.0 | 2026-10-28 | Nodes update themselves from the core (`node.boot`); inventory resumes after restarts; heavy tasks pause when the machine is in use; restarted agents get their task requeued |
| 0.5.0 | 2026-10-28 | Local model brain through Ollama on a node (`llm` module); `ASHUR_BRAIN` |
| 0.4.0 | 2026-10-28 | Chat with Claude, persistent memory, monthly budget |
| 0.3.0 | 2026-10-28 | Status page with live progress |
| 0.2.0 | 2026-10-28 | Inventory module, targeted tasks |
| 0.1.0 | 2026-10-28 | Core: register, heartbeat, tasks, offline sweep |
