# Changelog

Every push to `main` that passes the tests publishes `ghcr.io/sergiolin5/ashur-core`
and `ashur-node` as `:latest`, as `:<core version>` and as `:<commit>`. ZimaOS runs
`:latest`. To go back to an earlier version, set the image to that version's tag
(for example `ashur-core:0.5.0`); data in `/DATA/AppData/ashur` is not touched.

| Version | Date | What changed |
| --- | --- | --- |
| 0.13.0 | 2026-10-30 | Chat-only keys for Sergio himself (Estado → Invitados → "Es para mí"), for the iPhone Shortcut: they chat as Sergio, with his memories and thread, but reach nothing else and can be revoked on their own; in the browser they open the chat view |
| 0.12.3 | 2026-10-30 | Estado → Actualización also says which version of node code the core ships and which nodes are behind; nodes need no button (they update themselves; zima-server goes with the core) |
| 0.12.2 | 2026-10-30 | Updater pulls the exact image with `docker pull` before `compose up` (on ZimaOS `compose pull` returned without fetching it, so the first button update failed with "No such image" and was rolled back correctly); the panel's update button no longer overflows. ashur-updater must be moved to 0.12.2 by hand once |
| 0.12.1 | 2026-10-30 | `ashur` shows the updater: installed or not, which versions it sees, the last update. First release meant to be installed with the panel button |
| 0.12.0 | 2026-10-30 | "Actualizar" button (Estado → Actualización), tailnet only: the core checks the newest published version on GitHub; a new `ashur-updater` container (the only piece with Docker access, listening on 127.0.0.1 only, key in `updater.key`) changes the version in the compose files ZimaOS keeps for ashur-core and ashur-node, applies them, and puts the old version back if the new core does not answer within 2 min. The public door refuses `/api/update` |
| 0.11.3 | 2026-10-30 | Nodes retry, every 5 min, a module whose self-test failed at start (at logon the agent can start before Ollama) and announce it when it works. Node code changed: nodes update themselves |
| 0.11.2 | 2026-10-30 | Panel header reads "Núcleo v0.11.2  ·  4 dispositivos conectados" so the version and the count do not run together |
| 0.11.1 | 2026-10-30 | Panel: device details and activity lines wrap instead of being cut off |
| 0.11.0 | 2026-10-30 | Nodes say why they are paused or light ("con batería al 22%", "otros programas usan el 85%…") in the panel, `ashur dispositivos` and the activity log; Ollama's own CPU no longer counts as Sergio's load (it paused the Mac while answering); devices show a readable version ("v0.11.0", or "v0.10.0 → v0.11.0 pendiente") instead of "al día". Node code changed: nodes update themselves |
| 0.10.0 | 2026-10-30 | Guests: Sergio creates chat-only keys in the panel (Estado → Invitados), each with a monthly cap (US$1 by default) and 30 messages an hour; a guest signs in on the same panel and sees only their own chat; Ashur gets none of Sergio's memories, summaries, tools or devices for them; device names and API errors are never shown to guests; the session log records guests only by number; "Revocar" ends access |
| 0.9.1 | 2026-10-30 | Panel: recent activity, memories and conversation summaries show the newest 5 with "Ver todos (N)" / "Ver menos"; the choice survives the 3 s refresh |
| 0.9.0 | 2026-10-30 | Conversation memory: once 20 older messages leave the recent window they are summarized (Claude if it has credit, otherwise the local model; retried later if neither can) and every summary rides in the prompt; Claude can search all past messages; summaries listed in the panel with "Olvidar". Devices say "código al día" or "por actualizar" in the panel and in `ashur dispositivos` instead of a hash |
| 0.8.5 | 2026-10-30 | Import memories from the panel: paste one per line, review, then save; lines that look like keys or passwords are refused; duplicates skipped |
| 0.8.4 | 2026-10-30 | Panel data never waits for the inventory count: it is recounted in the background, one count at a time, on its own connection; database in WAL mode so long reads don't block heartbeats |
| 0.8.3 | 2026-10-30 | Panel data no longer recounts the whole inventory every 3 s (cached 60 s); `ashur probar` times the panel's data request and each query behind it |
| 0.8.2 | 2026-10-30 | `ASHUR_PUBLIC_BIND`: the public door can listen on 127.0.0.1 only. ZimaOS compose switched to host networking (8711 tailnet, 8712 local for Funnel) after Docker's port forwarding stopped passing outside traffic |
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
