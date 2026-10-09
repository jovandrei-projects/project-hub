# project-hub

Index page + launcher for the local services: live status,
start/stop/restart, links, and a Logs & Disk panel.

## Run

```
python hub.py     # or start-hub.bat
```

Then open http://127.0.0.1:8760

## Test

```
python run_tests.py          # full suite
python run_tests.py --quick  # skips slow/browser tests
```

See `AGENTS.md` for project rules and `../TESTING.md` for the shared
test policy.
