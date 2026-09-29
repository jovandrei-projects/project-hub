# project-hub - workplan

One phase at a time, in order. Mark items `[x]` as they land.

## Decisions already made

| Question | Answer |
|---|---|
| How is a service "running" decided? | TCP connect to `127.0.0.1:<port>` - honest, survives hub restarts, no PID bookkeeping. |
| How does stop find the process? | `netstat -ano` listener PIDs on the port; images outside `KILLABLE_IMAGES` are refused, not killed. |
| Why don't services show console windows? | `CREATE_NO_WINDOW` keeps them alive without clutter; stdout/stderr is appended to `logs/<id>.log` and tailed from the UI. |
| Where does the service list live? | `services.json` - one file, edited when a project is added or removed. |
| Which port? | 8760, per the root port registry in `C:\Projects\AGENTS.md`. |

## Phase 1 - working index + launcher

- [x] `hub.py` serves a status page on 127.0.0.1:8760 (`GET /`, `GET /api/status`, `GET /api/ping`)
- [x] Per-service and all-service start / stop / restart over POST (header-guarded)
- [x] Status states: up / starting / down, with listener PID + image and up-since
- [x] `services.json` covers the four serving projects + self row; CLI-only projects listed as tools
- [x] `start-hub.bat` double-click entry; second launch reuses the running hub instead of double-binding
- [x] Verified end to end: stop-all stopped 4/4, start-all brought 4/4 back to HTTP 200

## Phase 2 - optional polish (pick up only if wanted)

- [x] Log tail in the UI: per-row Log button tails `logs/<id>.log`; VocalCoach's own `.runtime` log is found through `log_instance` in services.json
- [ ] Remember "was started by hub" so stop could warn before killing a user-started instance
- [x] Run at login: `start-hub-hidden.vbs` via `Startup\project-hub.lnk` runs `pythonw hub.py --no-browser --start-all`; hub logs to `logs/hub.log` under pythonw
- [x] Stop-hub control: POST `/api/shutdown` + **Stop hub** button in the header (services keep running)
- [x] `services.json` hot-reload: mtime checked in `snapshot()`, so adding a service needs no hub restart (verified 2026-09-29: edit and revert both appeared on the next `/api/status` poll)

---

## Explicitly not doing

- No LAN exposure, no auth beyond the loopback + header guard - the rule in `C:\Projects\AGENTS.md` is loopback only.
- No start/stop for CLI-only tools (`chrome-bookmarks`, `nirvana-concert-images`) - they are listed, not managed.
