# Tooling: status, bank and the local web view

The client writes two kinds of file for tools to read, and `tools/gui/` is a small web page over them.
None of it changes how scripts run, and nothing here talks to the network beyond `127.0.0.1`.

## Status file

Every client process writes `status/<name>.json` every five minutes, and at once when an account logs
in, logs out or changes script. It holds each account's state and every skill's level, base level and
XP, so a tool doesn't have to parse logs or progress reports.

| Key | Meaning |
|---|---|
| `scripting.statusDirectory` | The folder. Default `status`; empty turns the file off |
| `scripting.statusName` | The file's name. Default is the stem of `client.logFile` (`rs2004-headless-w1.log` → `rs2004-headless-w1.json`), or `client` without one |

```json
{
  "world": "w1.example",
  "writtenAt": "2026-10-26T15:20:00Z",
  "kind": "period",
  "accounts": [
    {
      "name": "s18", "username": "s18", "script": "bots/thiever.py",
      "state": "ingame", "status": "stealing", "placed": true, "loginCount": 3,
      "tile": {"x": 3223, "z": 3218, "level": 0},
      "hp": 31, "maxHp": 31, "run": 100,
      "skills": {"attack": {"level": 31, "base": 31, "xp": 14848}}
    }
  ]
}
```

`kind` is `period` for the five-minute write and `event` for the lifecycle ones. Rates should use only
`period` writes: two a few seconds apart would extrapolate nonsense.

## Bank log

Each time a bank opens, the account writes what it holds, by item id, name and count:

- `bank/<account>.jsonl` — one record per open
- `bank/<account>.json` — the latest

| Key | Meaning |
|---|---|
| `scripting.bankLogDirectory` | The folder. Default `bank`; empty turns it off |

```json
{"time": "2026-10-26T15:30:43Z", "account": "s18", "world": "w1.example",
 "items": [{"id": 995, "name": "Coins", "qty": 109}, {"id": 1891, "name": "Cake", "qty": 55}]}
```

It's open-only by design: deposits and withdrawals made while the bank stays open aren't re-logged, so
these are "as last seen with the bank open".

`tools/bank_report.py` sums the latest snapshot per account:

```sh
python3 tools/bank_report.py            # bank/totals.json and bank/totals.log
python3 tools/bank_report.py --watch    # every 60 seconds
```

## The local web view

`tools/gui/server.py` is one page for watching a fleet. **It is never started by the client**: run it
yourself, and Ctrl+C ends it. It binds to `127.0.0.1` only.

```sh
python3 tools/gui/server.py                                # view only
python3 tools/gui/server.py --clients client_w1.jsonc client_w2.jsonc
```

- **Without `--clients`** the page is read-only: skills, xp/hr and the bank tab, with Start and Stop
  disabled. It starts no process at all.
- **With `--clients`** Start writes the chosen script into every account file the named configs load,
  then runs `Rs2004Headless <config> --raw-console` for each, one process per config. Stop ends them
  gracefully, then forcefully after a timeout.

The Fleet tab shows each account's level, base, XP and XP/hr for the chosen skill, plus state and tile.
XP/hr comes from the status snapshots: it needs two periodic writes at least four minutes apart, and
starts again after a relogin. The Bank tab sums the latest bank snapshot per account.

There is no authentication: the page runs commands on the machine it's started on, so keep it on
`127.0.0.1` and don't leave it running unattended.
