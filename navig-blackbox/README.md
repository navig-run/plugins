# navig-blackbox

> A free, **standalone** flight-recorder & crash black-box for any Python app — append-only
> event log, `sys.excepthook` crash capture, and sealed **`.navbox`** incident bundles — that
> installs **without** navig, and also backs the **`navig blackbox`** command inside it.

The engine was lifted out of navig-core (`navig/blackbox/`) into this package so there is
**one source of truth** (the `voice/`→navig-audio pattern) — `navig-core/navig/blackbox/*` are
now thin re-export shims. Every navig-internal touchpoint (console, atomic writes, paths,
version, vault encryption) is routed through `navig_blackbox._compat`, which uses navig when
present and falls back cleanly when it isn't.

## Install & use (standalone)

```bash
pip install navig-blackbox
nbb status                       # event count, on-disk size, sealed state, data dir
nbb record error "disk full"     # append an event to the recorder
nbb events -n 100                # render the recent event timeline
nbb bundle -o incident.navbox    # seal recent events + crashes + log tails into a .navbox
nbb inspect incident.navbox      # read a bundle back (manifest + timeline)
nbb crashes                      # list captured crash reports
nbb seal / nbb unseal            # freeze / resume recording for an investigation
# also: navig-blackbox --help  ·  python -m navig_blackbox --help
```

## Use as a library (any Python app)

```python
from navig_blackbox import get_recorder, install_crash_handler, EventType

install_crash_handler()                       # unhandled exceptions → ~/.navig/blackbox/crashes/
get_recorder().record(EventType.COMMAND, {"command": "deploy", "args": "prod"})

from navig_blackbox import create_bundle, write_bundle
write_bundle(create_bundle(since_hours=4), "incident.navbox")   # shareable postmortem archive
```

## Inside navig

The command stays `navig blackbox …` (backed by this engine via the shim); ships as the optional
`navig[blackbox]` extra and registers a free **Blackbox** module tile. Encrypted `.navbox` export
(`--encrypt`) uses the navig vault key when navig is present, and falls back to a plaintext
archive (with a warning) standalone — data is never lost.

Apache-2.0.
