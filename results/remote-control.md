# NVFBC remote-control interface: verification

Evidence that the `nvfbc_*` options added to `remote.c` in `fcfaf8f` are wired
end to end — that queries report real state and that setters actually take
effect rather than flipping a variable the driver never reads.

Run with `remote-check.sh`, against the live service on `:1`, 2026-08-15.

## Before (live binary `0462aaf8`, no handlers)

```
ans=nvfbc:N/A
ans=nvfbc_push:N/A
ans=nvfbc_cursor:N/A
ans=nvfbc_diffmap:N/A
ans=nvfbc_direct:N/A
```

The query mechanism itself worked; the variables were simply unknown to it.

## After (live binary `0ba5a9e2`)

```
aro=nvfbc:1            capture active (read-only by design)
ans=nvfbc_push:1
ans=nvfbc_cursor:0
ans=nvfbc_diffmap:1
ans=nvfbc_direct:0
```

Every value matches the deployed command line
(`-nvfbc -nvfbc_nocursor -nvfbc_push`), which is the point: the interface
reports actual state, not compiled-in defaults.

## Setter

```
-R nvfbc_nopush  ->  nvfbc_push:0   (changed)
                     18:53:13 NVFBC: capture session restarted
                              (cursor=0, diffmap=1, push=0, direct=0)
-R nvfbc_push    ->  nvfbc_push:1   (restored)
```

**The log line is the load-bearing evidence.** `bPushModel` is fixed when the
capture session is created, so a setter that only assigned the global would
have flipped the query result to 0 while the driver carried on generating
frames exactly as before — reporting a change that had not happened. The
session genuinely tore down and rebuilt.

Two live session restarts, `could not be restarted` count: 0. Capture healthy
afterwards (10-12 new fps, ~3 grabs per frame on an idle desktop), server pid
unchanged, VNC session uninterrupted.

## Constraint worth remembering

Remote commands are broadcast through a single `X11VNC_REMOTE` root-window
property with **no per-instance targeting**. With two x11vnc processes on a
display, a query is a race between their answers and a setter hits both.
`remote-check.sh` refuses to run unless exactly one is present — that guard
immediately caught a test instance left running from an earlier experiment,
which had been competing for the same property unnoticed.

## What is not settable

`nvfbc` itself is read-only. Disabling capture at runtime would leave x11vnc on
the `XGetSubImage` path, because the MIT-SHM polling images are never created
when NVFBC comes up (`using_shm = 0` at init) and re-creating them means
rebuilding the framebuffer under a live session. Refusing beats silently
degrading to the slowest read path.
