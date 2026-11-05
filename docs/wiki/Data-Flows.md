# Data Flows

This page follows every kind of data through DYODE v1, end to end: from the
moment the input box starts, through the one-way link, to the moment the
data is usable on the output box. It also shows what happens when something
is lost or rejected, and what each side writes to its log when that happens.

Everything here is taken from the code in `DYODE_v1_full/`. Names in
`code` are config keys, functions or log events you can search for.

- [Overall flow and start-up](#overall-flow-and-start-up)
- [Folder transfer flow](#folder-transfer-flow)
- [Modbus mirroring flow](#modbus-mirroring-flow)
- [Log streaming flow](#log-streaming-flow)
- [Screen sharing flow](#screen-sharing-flow)
- [Error and loss handling](#error-and-loss-handling)

## Overall flow and start-up

Both boxes read the **same `config.yaml`**. The input box runs
`dyode_in.py`, the output box runs `dyode_out.py`. Each starts one process
per module, and every module on the input box sends UDP to `dyode_out.ip`
on the module's own `port`.

```mermaid
flowchart LR
    subgraph IN["Input box (dyode_in.py)"]
        direction TB
        FIN["folder:<br/>udp-sender"]
        MIN["modbus:<br/>PLC poller"]
        LIN["logs:<br/>syslog + tail"]
        SIN["screen:<br/>newest JPEG"]
        ARP["arp-keeper"]
    end

    DIODE{{"Data diode<br/>UDP, one way"}}

    subgraph OUT["Output box (dyode_out.py)"]
        direction TB
        FOUT["folder:<br/>udp-receiver + verify"]
        MOUT["modbus:<br/>read-only Modbus server"]
        LOUT["logs:<br/>files / SIEM"]
        SOUT["screen:<br/>HTTP MJPEG"]
    end

    FIN ==>|"udpcast, port"| DIODE
    MIN ==>|"DYO1 + DYA1, port"| DIODE
    LIN ==>|"DYO1 + DYA1, port"| DIODE
    SIN ==>|"DYO1 + DYA1, port"| DIODE
    DIODE ==> FOUT
    DIODE ==> MOUT
    DIODE ==> LOUT
    DIODE ==> SOUT
```

Folder modules use udpcast. The other three use DYODE's own framing: each
message is signed (`DYA1`: sender id, counter, HMAC-SHA256) and then split
into `DYO1` datagrams of at most 1400 bytes with a CRC32 of the whole
message. See [Architecture](Architecture#two-transports).

### Start-up and config loading

```mermaid
flowchart TB
    A["systemd starts dyode_in.py / dyode_out.py"] --> B["find_config:<br/>-c path, else ./config.yaml,<br/>else beside the script"]
    B --> C["load_config: validate and normalize<br/>modules, network, logging, auth"]
    C -- "ConfigError" --> X1["log 'configuration error: ...'<br/>exit 1"]
    C --> D{"Which side?"}
    D -- input --> E{"dyode_out.mac set?"}
    E -- yes --> F["ip neigh replace ... nud permanent"]
    F -- fails --> X2["'cannot set the static ARP entry ...<br/>refusing to start', exit 1<br/>(systemd retries)"]
    F -- ok --> G["Give each folder module without<br/>'bitrate' a share of 8 Mbit/s"]
    E -- "no: warning,<br/>no ARP keeper" --> G
    G --> H["supervise: one process per module<br/>+ arp-keeper"]
    D -- output --> H2["supervise: one process per module"]
    H --> M["Each module: require_auth(cfg)"]
    H2 --> M
    M -- "dyode.key missing and<br/>auth.required true" --> X3["'configuration error: no message<br/>authentication key ...', module exits"]
    M --> RUN["Module runs forever"]
    RUN -- "crash" --> R["supervisor logs<br/>'module X exited (code N)'<br/>and restarts it (5 s guard)"]
    R --> RUN
```

What `load_config` does:

- Lower-cases each module's `type` and checks it is `folder`, `modbus`,
  `logs` or `screen`; `port` is required for every module.
- Builds a `network` section from `dyode_in` / `dyode_out`, with defaults
  `10.0.1.1` / `10.0.1.2`, `eth0` / `eth1`, and `arp_interval` 60 s.
- For `folder` modules, fills in defaults: `fec` `8x16/64` (or off for
  `none`, `off`, `false`, `no`, `0`, `disabled`), `file_gap` 0.5 s,
  `keep_sent_hours` 24, `batch_timeout` 300 s, `autostart` 5,
  `start_timeout` 300 s. A `staging` folder must be absolute and must not be
  inside `out`.
- For `modbus`, parses `registers` / `coils` ranges (end excluded) and
  checks `unmapped` is `exception` or `zero`.
- For `logs`, `logstream.normalize_props` checks sources and destinations
  and raises `reorder_window` to at least the time all `copies` need.
- Resolves `auth.key_file` relative to the config file's folder.

Any validation error stops the program before a single module starts. The
key file is checked later, by each module as it starts.

## Folder transfer flow

### Input side: watching and batching

```mermaid
flowchart TB
    START["run_folder_input:<br/>create 'in', start ChangeWatcher,<br/>temporary workdir"] --> SCAN["scan_ready_files('in', settle)"]
    SCAN --> SKIP["Skip: symlinks, non-regular files,<br/>folders starting with .dyode"]
    SCAN --> READY{"Any file unchanged<br/>for 'settle' seconds?"}
    READY -- "no" --> PRUNE["prune_sent: delete .dyode_sent<br/>files older than keep_sent_hours"]
    PRUNE --> WAIT["Wait for inotify event,<br/>up to 'settle' s (files still changing)<br/>or 'rescan' s (idle)"]
    WAIT --> SCAN
    READY -- "yes" --> OPEN["Open up to 256 files with<br/>O_NOFOLLOW on every path part"]
    OPEN -- "not a regular file any more" --> SKIPLOG["warning 'skipped ...'<br/>event file_skipped"]
    OPEN --> MAN["build_manifest: SHA-256 and size<br/>of each OPEN file, random batch id,<br/>HMAC 'mac' over canonical JSON"]
    MAN --> SEND["send manifest, then each file<br/>(see sequence below)"]
    SEND --> SCAN
```

Without `inotify_simple` installed, the watcher logs a warning and polls
instead. Files left over in `in` are picked up at start-up.

The manifest is JSON:
`{"magic": "dyode-manifest-v1", "batch": <hex id>, "files": [{"path", "size", "sha256"}, ...], "mac": <hex HMAC>}`.
The HMAC covers every field except `mac`, serialized with sorted keys.

### udpcast invocation

Each file, and the manifest itself, is one separate udpcast transfer.
Arguments are passed as a list, never through a shell.

Sender (input box), per file:

```
udp-sender --async --nokbd [--fec <fec>] --max-bitrate <bitrate>m
           --mcast-rdv-addr <out_ip> --mcast-data-addr <out_ip>
           --portbase <port> --autostart <autostart>
           --interface <in_interface> -f /proc/self/fd/<N>
```

Receiver (output box), per transfer:

```
udp-receiver --nosync --nokbd --mcast-rdv-addr <in_ip>
             --interface <out_interface> --portbase <port>
             [--start-timeout <start_timeout>] -f <staging>/<tmpfile>
```

| Option | Where it comes from | What it does here |
|---|---|---|
| `--async` | fixed | Send without waiting for receiver replies, which can never come |
| `--fec` | `fec`, default `8x16/64`; left out when `fec: none` | Adds repair data so lost packets can be rebuilt without asking |
| `--max-bitrate` | `bitrate` in Mbit/s; default is 8 Mbit/s shared equally between folder modules | Caps the send rate |
| `--autostart` | `autostart`, default 5 | Hello retransmissions before data starts. On a diode this is the only grace period the receiver gets, and it is paid on every file |
| `--start-timeout` | `start_timeout`, default 300; `0` leaves it out | Receiver gives up if no sender appears; DYODE restarts it at once |
| `-f /proc/self/fd/N` | the descriptor that was hashed | What is sent is exactly what was hashed, even if the path changed since |
| `--nokbd` | fixed | No "press any key" prompt under systemd |

### One batch, over time

```mermaid
sequenceDiagram
    autonumber
    participant W as Input: folder module
    participant S as udp-sender
    participant D as Diode
    participant R as udp-receiver
    participant V as Output: verify thread
    participant O as out folder

    Note over R: Always listening, writing to a new<br/>temp file in the staging folder
    W->>W: scan, open, hash, sign manifest
    W->>S: send manifest_<batch>.json
    S->>D: hello x autostart, then data + FEC
    D->>R: manifest transfer
    R-->>V: queue blob (max 4 queued)
    Note over R: restarted immediately
    V->>V: read_manifest: magic, fields, HMAC, not replayed
    V->>V: open batch: expected hashes and sizes
    W->>W: log "sending manifest", delete manifest file
    loop each file in the batch
        W->>S: send /proc/self/fd/N
        S->>D: hello x autostart, then data + FEC
        D->>R: file transfer
        R-->>V: queue blob
        W->>W: move file to .dyode_sent, event file_sent
        W->>W: sleep file_gap
        V->>V: size matches an expected file? then SHA-256
        V->>O: hash matches: atomic move to out/path
        V->>V: event file_stored
    end
    W->>W: event batch_sent (files, bytes, duration_s, mbps)
    V->>V: all expected files stored: event batch_received (complete)
```

If `udp-sender` exits non-zero for a file, the input box logs
`aborting batch; N file(s) left for the next batch`. Those files were never
moved out of `in`, so the next scan sends them again in a new batch.

### Output side: receive and verify

`udp-receiver` runs in a loop and never waits for verification: a finished
transfer is put on a queue (at most 4 waiting) and the receiver is started
again straight away, because the sender cannot be asked to pause. A
separate verify thread does the rest.

```mermaid
flowchart TB
    RX["udp-receiver into<br/>staging/tmpXXXX"] --> OUTC{"Outcome"}
    OUTC -- "ok" --> Q["Queue blob for verify thread"]
    Q --> RX
    OUTC -- "idle: nothing received,<br/>ran ~start_timeout" --> DEL1["delete temp file,<br/>restart at once"] --> RX
    OUTC -- "error" --> DEL2["delete temp file, log error,<br/>sleep 1 s"] --> RX

    Q --> MANQ{"Valid manifest?<br/>magic, fields, under 16 MB"}
    MANQ -- "yes, bad HMAC" --> FORGED["warning 'not signed with this box's key'<br/>event manifest_rejected bad_signature"]
    MANQ -- "yes, batch id seen before" --> REPLAY["warning 'replay refused'<br/>event manifest_rejected replayed"]
    MANQ -- "yes" --> NEWB["Open batch, event manifest_received.<br/>Same path in older open batches is dropped;<br/>keep at most 4 open batches"]
    MANQ -- "no: it is a file" --> ANY{"Any open batch?"}
    ANY -- "no" --> ORPH["warning 'no manifest pending; discarded'<br/>event file_orphaned"]
    ANY -- "yes" --> SZ{"Size matches any<br/>expected file?"}
    SZ -- "no" --> REJ["error 'matching no expected file; discarded'<br/>event file_rejected no_matching_file"]
    SZ -- "yes" --> H["SHA-256 of blob"]
    H --> HM{"Hash claims an<br/>outstanding entry?"}
    HM -- "no" --> REJ
    HM -- "yes" --> SAFE{"safe_join: path stays<br/>inside 'out'?"}
    SAFE -- "no" --> UNS["event file_rejected unsafe_path"]
    SAFE -- "yes" --> PL["place_file: open each folder O_NOFOLLOW,<br/>create missing ones, rename into place"]
    PL -- "a folder is a symlink" --> UND["event file_rejected unsafe_destination"]
    PL -- "ok" --> STORED["info 'file X available at ...'<br/>event file_stored"]
```

Points worth knowing:

- **Files are matched by content, not by order.** A blob's own SHA-256
  decides which manifest entry it is. A lost transfer costs only that file;
  it cannot shift the rest of the batch.
- **Unlisted files are discarded.** A blob whose size and hash match no
  outstanding entry in any open batch is deleted. This is also what makes an
  unauthenticated `udp-receiver` safe: a file only lands in `out` if a
  signed manifest names its SHA-256.
- **Staging is outside `out`.** By default it is `<out>.incoming`, a sibling
  on the same filesystem, so the final rename is atomic and nothing serving
  `out` ever sees a partial file. Leftovers in staging are deleted at start.
- **Replay protection** only applies when authentication is on, and it is
  in memory (the last 1024 batch ids).

### Batch lifetime on the output box

```mermaid
stateDiagram-v2
    [*] --> Open: signed manifest received
    Open --> Open: file stored / rejected
    Open --> Complete: no files outstanding
    Open --> Superseded: a newer manifest re-lists all outstanding paths
    Open --> Timeout: batch_timeout with no data landing in staging
    Open --> Evicted: a 5th batch opens
    Open --> Shutdown: close_all
    Complete --> [*]
    Superseded --> [*]
    Timeout --> [*]
    Evicted --> [*]
    Shutdown --> [*]
```

When a batch closes for any reason other than `complete`, each missing file
is named: `file <path> never arrived (batch <id>, <reason>)`, with a
`file_missing` event, then `batch_received` with the counts. Data still
arriving in staging keeps every open batch alive (since 1.4.3), so a long
single transfer is not timed out under itself.

### Retry and recovery on the input box

`udp-sender` exiting 0 only means "transmitted". Sent files are therefore
moved to `in/.dyode_sent/<path>` and kept for `keep_sent_hours` (default
24) rather than deleted. To re-send a file reported as `never arrived`, copy
it from `.dyode_sent` back into `in`. With `keep_sent_hours: 0` sent files
are deleted.

### Receive tuning (writeback)

Large, fast transfers need the receiving box to keep reading the socket
without pauses. `install.sh` writes `/etc/sysctl.d/90-dyode-receive.conf` on
receiving boxes:

```mermaid
flowchart LR
    NET["Packets at up to<br/>'bitrate'"] --> BUF["Socket buffer<br/>rmem_default 8 MB"]
    BUF --> UR["udp-receiver"]
    UR --> PC["Page cache<br/>(dirty data)"]
    PC -->|"writeback starts at 64 MB<br/>(vm.dirty_background_bytes)"| DISK["Staging disk<br/>(maybe LUKS)"]
    PC -.->|"at vm.dirty_bytes<br/>(20% RAM, 128 MB-2 GB)<br/>writer is paused"| UR
```

Without it, Linux wrote ~30 s of data in one burst, `udp-receiver` stalled,
its socket buffer overflowed, and udpcast aborted the file. When a transfer
stalls, DYODE reads the kernel's `Udp RcvbufErrors` counter before and after
and says which case it was: a receive-buffer overflow on this box (apply
`sudo ./install.sh --tuning-only`), or packets lost before the box (NIC,
cabling, diode). See [CAVEATS](https://github.com/nc-intezra/dyode_python3/blob/master/CAVEATS.md).

## Modbus mirroring flow

```mermaid
sequenceDiagram
    autonumber
    participant PLC
    participant P as Input: PlcPoller
    participant D as Diode
    participant U as Output: UDP listener
    participant M as ModbusMirror
    participant C as SCADA client

    loop every 'interval' (default 1 s)
        P->>PLC: read holding registers and coils (Modbus TCP, unit)
        PLC-->>P: values
        P->>P: JSON {module, seq, time, registers, coils}
        P->>D: sign (DYA1), split into DYO1 datagrams
        D->>U: datagrams
        U->>U: reassemble, CRC32, verify HMAC and counter
        U->>M: apply: write values into datastore
    end
    C->>M: read registers / coils
    M-->>C: last values (exception 02 outside configured ranges)
    C->>M: write
    M-->>C: exception 01 (mirror is read-only)
```

```mermaid
flowchart TB
    POLL["poll()"] -- "connect or read fails" --> PF["close connection;<br/>first failure: warning,<br/>repeats: debug"] --> NEXT["sleep until next interval"]
    POLL -- "ok" --> SEND["udp_send to out_ip:port"] --> REC["first success after failure:<br/>info 'recovered'"] --> NEXT
    NEXT --> POLL

    RX["Datagram on output"] --> RA{"Complete, CRC ok?"}
    RA -- "no" --> DROP1["held or dropped silently"]
    RA -- "yes" --> AU{"Signed, not replayed?"}
    AU -- "no" --> DROP2["warning 'N message(s) rejected'<br/>(at most once a minute)"]
    AU -- "yes" --> JS{"Valid JSON and values?"}
    JS -- "no" --> DROP3["error 'undecodable' or<br/>'rejected malformed message'"]
    JS -- "yes" --> AP["write to datastore;<br/>seq gap: debug only"]
    AP --> FRESH["last_update = now"]
    STALE["watch_staleness, every 1 s"] -- "no update for stale_after (10 s)" --> SW["warning 'no update for Ns,<br/>serving stale values'"]
    FRESH -.-> RES["next update after stale:<br/>info 'updates resumed'"]
```

The server keeps answering with the last values it received while updates
are missing; clients cannot tell. The log warning is the only signal. Values
whose start address falls outside the output box's datastore are ignored
with a warning (`Do both config.yaml files match?`). See
[Modbus Mirroring](Modbus-Mirroring).

## Log streaming flow

```mermaid
flowchart LR
    subgraph SRC["Input box sources"]
        U["syslog UDP<br/>listen_udp"]
        T["syslog TCP<br/>listen_tcp"]
        F["tail -F files<br/>tail"]
    end
    U --> AL{"allow_from?"}
    T --> AL
    AL --> BUF["Bounded buffer<br/>buffer_lines"]
    F --> BUF
    BUF -- "full" --> DR["line dropped, counted"]
    BUF --> BATCH["every batch_interval:<br/>batch up to batch_bytes,<br/>DYLG + zlib(JSON), seq += 1"]
    BATCH --> TB["Token bucket<br/>max_kbps"]
    TB --> TX["send now"]
    TB --> REP["repeat after delay<br/>(copies - 1 times)<br/>skipped if no bandwidth"]
    TX ==> DIODE{{"Diode"}}
    REP ==> DIODE
    DIODE ==> RX["Reassemble, verify,<br/>decode batch"]
    RX --> ORD["LogReceiver:<br/>drop duplicates,<br/>reorder by seq"]
    ORD --> SINK["received.log<br/>or per-source files"]
    ORD --> FWD["forward udp:// / tcp://<br/>to SIEM"]
```

```mermaid
sequenceDiagram
    autonumber
    participant S as Input: LogSender
    participant D as Diode
    participant R as Output: LogReceiver
    participant K as Sinks

    S->>D: batch seq 7 (copy 1)
    D->>R: seq 7
    R->>K: write lines of 7
    S->>D: batch seq 8 (copy 1)
    Note over D: seq 8 copy 1 lost
    S->>D: batch seq 7 (copy 2)
    D->>R: seq 7 again: duplicate, ignored
    S->>D: batch seq 9 (copy 1)
    D->>R: seq 9: held, gap timer starts
    S->>D: batch seq 8 (copy 2)
    D->>R: seq 8
    R->>K: write 8, then 9
    Note over S,R: If seq 8 never comes within reorder_window:<br/>warning "1 log batch(es) lost in the diode (sequence 8-8)",<br/>event logs_gap, then 9 is written
    Note over S: Nothing to send for 'heartbeat' s:<br/>empty batch keeps seq moving
```

- Each batch carries the sender's boot id, its sequence number, and how
  many lines the input box has dropped so far. A new boot id means the
  input side restarted (`logs_sender_restart`); a rise in the dropped count
  is reported on the output box as `input side dropped N line(s)`.
- With no batch or heartbeat for `stale_after` (default 3 x `heartbeat`),
  the output box logs `no log batches or heartbeats for Ns` (`logs_stale`),
  and `log stream resumed` (`logs_resumed`) when data returns.
- Both sides write `logs_stats` events every 60 s.

See [Log Streaming](Log-Streaming).

## Screen sharing flow

```mermaid
sequenceDiagram
    autonumber
    participant SH as screenshot.ps1 (or any writer)
    participant I as Input: screen module
    participant D as Diode
    participant O as Output: receiver thread
    participant FS as FrameStore
    participant B as Browser

    SH->>I: write JPEG into 'in' folder
    loop on change, or every 1 s
        I->>I: newest .jpg/.jpeg (no symlinks)
        I->>I: skip if same mtime and size, or not starting FF D8
        I->>D: sign and send whole JPEG (DYO1 datagrams)
        I->>I: sleep 1 / max_fps
    end
    D->>O: datagrams (socket stays bound)
    O->>O: reassemble, CRC32, verify HMAC
    O->>FS: put frame (if it starts FF D8)
    B->>FS: GET /screen.mjpg (at most max_streams viewers)
    FS-->>B: each new frame as multipart JPEG
    B->>FS: GET /screen.jpg
    FS-->>B: latest frame, or 503 "no frame received yet"
```

Only the newest frame is kept; there is no frame history or sequence
number. A frame with any lost datagram is simply dropped and the next one
replaces it. The viewer page listens on `http_bind`, by default the first
address that is not on the diode NIC. See [Screen Sharing](Screen-Sharing).

## Error and loss handling

A diode has no return path, so nothing is ever re-requested. Each flow
either repairs loss in advance (FEC, duplicate copies) or reports it.

```mermaid
flowchart TB
    LOSS["Packet lost or corrupted in the link"] --> T{"Which flow?"}
    T -- "folder" --> FEC{"FEC can rebuild it?"}
    FEC -- "yes" --> OK1["file arrives intact"]
    FEC -- "no" --> AB["udp-receiver aborts the transfer:<br/>'failed ... a transfer started and then stopped'"]
    AB --> MISS["batch closes later:<br/>'file X never arrived', file_missing"]
    MISS --> MAN["operator copies file from<br/>.dyode_sent back to 'in'"]
    T -- "logs" --> CP{"Another copy arrives?"}
    CP -- "yes" --> OK2["batch delivered in order"]
    CP -- "no" --> GAP["'N log batch(es) lost', logs_gap"]
    T -- "modbus" --> NX["message dropped;<br/>next poll replaces it"]
    NX -.-> ST["only if it lasts stale_after:<br/>'serving stale values'"]
    T -- "screen" --> NF["frame dropped;<br/>next frame replaces it"]
```

### What each side logs

| Situation | Input box | Output box |
|---|---|---|
| Config invalid | `configuration error: ...`, exit 1 | same |
| Key file missing (`auth.required`) | `configuration error: no message authentication key ...`, module exits and is restarted | same |
| Static ARP cannot be set | at start: `refusing to start`; later: `static ARP entry is NOT in place` | nothing (it simply receives nothing) |
| Module crash | `module X exited (code N)`, restarted | same |
| udpcast missing | `udp-sender not found: install udpcast` | `udp-receiver not found: install udpcast` |
| Folder: file swapped for a link after the scan | `skipped ...`, `file_skipped` | - |
| Folder: `udp-sender` fails on manifest | `udp-sender failed (exit N)`, `batch_failed` | - |
| Folder: `udp-sender` fails on a file | `aborting batch; N file(s) left for the next batch` | batch later closes with `file_missing` |
| Folder: quiet link | - | once at info: `udp-receiver idled out ... normal`, then debug |
| Folder: receiver exits early | - | `udp-receiver failed ... exited before its start timeout; check the interface, port and permissions` |
| Folder: transfer stalls | - | `udp-receiver failed ... a transfer started and then stopped` plus the receive-buffer hint |
| Folder: forged / replayed manifest | - | `manifest_rejected` (`bad_signature` / `replayed`) |
| Folder: unlisted or damaged file | - | `matching no expected file; discarded`, `file_rejected` |
| Folder: file with no open batch | - | `no manifest pending; discarded`, `file_orphaned` |
| Folder: unsafe path or symlinked folder | - | `file_rejected` (`unsafe_path` / `unsafe_destination`) |
| Folder: files never arrived | - | `file X never arrived`, `file_missing`, `batch_received` |
| Modbus: PLC unreachable | `module X: ...` once, `recovered` later | `no update for Ns, serving stale values` |
| Modbus: client write | - | `Modbus client write refused` (at most once a minute) |
| Logs: buffer full | `buffer full, N line(s) dropped so far` | `input side dropped N line(s)`, `logs_dropped_at_source` |
| Logs: batches lost | - | `N log batch(es) lost in the diode`, `logs_gap` |
| Logs: silence | - | `no log batches or heartbeats`, `logs_stale` |
| Any signed module: unsigned or replayed message | - | `N message(s) rejected: not signed with this box's key, or replayed` |
| Screen: too many viewers | - | `viewer X refused: N streams already open` |

Lines are written to `logging.dir`/`human` (and stderr); the JSON events
(`file_sent`, `batch_received`, `logs_gap`, ...) go to `logging.json`. See
[Operations and Monitoring](Operations-and-Monitoring).
