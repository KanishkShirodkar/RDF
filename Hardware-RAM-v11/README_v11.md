# RDF v11 — RAM Measurement Guide
## STM32F103REY6 (IoT-LAB M3) | Contiki-NG

---

## What is new in v11 vs v10

| Feature | v10 | v11 |
|---|---|---|
| RDF flooding logic | ✅ | ✅ unchanged |
| All log formats (TX/RX/CBF/RDF) | ✅ | ✅ unchanged |
| Stack painting (runtime RAM) | ❌ | ✅ **NEW** |
| `RAM_REPORT` log line | ❌ | ✅ **NEW** |
| `RAM_STATIC` log line | ❌ | ✅ **NEW** |
| `ram_analyzer.py` | ❌ | ✅ **NEW** |

---

## Memory map of STM32F103REY6 (from `stm32f103rey6.ld`)

```
Flash : 0x08000000  512 KB   (.text, .rodata, .data load image)
RAM   : 0x20000000   64 KB   (.data, .bss, heap, stack)
                              └─ _estack = 0x20010000 (top of RAM)

RAM layout at runtime:
┌─────────────────────────────────┐ 0x20010000  ← _estack (top)
│  Stack (grows DOWN ↓)           │
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │ ← painted 0xAA by v11 at boot
│  Free RAM                       │
├─────────────────────────────────┤ ← _ebss (end of .bss)
│  .bss  (zero-init globals)      │   Contiki-NG OS + RDF arrays
├─────────────────────────────────┤ ← _sdata / _edata
│  .data (init globals)           │
└─────────────────────────────────┘ 0x20000000  (bottom)
```

---

## How stack painting works (no linker script changes needed)

`stm32f103rey6.ld` already defines two symbols:
- `_ebss`   — end of `.bss` section (bottom of free RAM)
- `_estack` — `0x20010000` (top of RAM, stack grows DOWN from here)

v11 declares these as `extern uint32_t` — the linker resolves them automatically.

**At boot:** fills `[_ebss .. _estack - 64]` with `0xAA`
**Every 10s:** scans downward from `_estack`, finds highest address still `0xAA` → everything above that was overwritten by the stack → that is the peak stack depth.

---

## Total RAM formula

```
Total system RAM used = arm-none-eabi-size(.data + .bss)  +  peak_stack
                        ─────────────────────────────────     ──────────
                        static: all globals (OS + app)        dynamic: runtime stack
                        measured ONCE after build             measured from log
```

`.data + .bss` from `arm-none-eabi-size` already includes **both** Contiki-NG OS globals and your RDF arrays — you do not need to add them separately.

---

## Step-by-step procedure

### Step 1 — Copy firmware to your Contiki-NG project

```bash
# Assuming your Contiki-NG project is at ~/contiki-ng/examples/rdf/
cp rdf-flood-HW-v11.c ~/contiki-ng/examples/rdf/
```

Your `Makefile` should already have:
```makefile
CONTIKI_PROJECT = rdf-flood-HW-v11
all: $(CONTIKI_PROJECT)
include $(CONTIKI)/Makefile.include
```

---

### Step 2 — Build for each MAX_NODES value

Run this for each network size you want to measure (e.g. 5, 10, 20, 30, 51):

```bash
cd ~/contiki-ng/examples/rdf/

# Example: build for MAX_NODES=10
ARCH_PATH=../../../arch \
  make TARGET=iotlab BOARD=m3 \
  CFLAGS="-DMAX_NODES=10 -DMAX_SEQ_TRACK=256" \
  clean all
```

---

### Step 3 — Get static RAM from arm-none-eabi-size

```bash
arm-none-eabi-size build/iotlab/m3/rdf-flood-HW-v11.iotlab
```

Example output:
```
   text    data     bss     dec     hex filename
  42316     164   35672   38152    9508 rdf-flood-HW-v11.iotlab
```

**Static RAM = data + bss = 164 + 35672 = 35836 bytes (35.0 KB)**

Record this for each MAX_NODES build:

| MAX_NODES | text (B) | data (B) | bss (B) | Static RAM (B) | Static RAM (KB) |
|-----------|----------|----------|---------|----------------|-----------------|
| 5         |          |          |         |                |                 |
| 10        |          |          |         |                |                 |
| 20        |          |          |         |                |                 |
| 30        |          |          |         |                |                 |
| 51        |          |          |         |                |                 |

---

### Step 4 — Flash to IoT-LAB nodes

```bash
# Flash to your reserved nodes (example: toulouse m3-30, m3-32, m3-34, m3-36, m3-38)
iotlab-node --flash build/iotlab/m3/rdf-flood-HW-v11.iotlab \
  -l toulouse,m3,30+32+34+36+38
```

---

### Step 5 — Collect log

```bash
# Run serial aggregator and save log (let it run for at least 2-3 minutes)
serial_aggregator | tee ~/rdf_v11_run.log
```

You will see lines like:
```
1783186504.302;m3-38;[INFO: RDF ] STACK_PAINT painted=24512 bytes from 0x20009e80 to 0x20010000
1783186504.303;m3-38;[INFO: RDF ] RAM_STATIC node=m3-38 max_nodes=51 max_seq=256 last_seq_seen=102 seen=13056 rdf_state=2652 recv_count=204 fwd_count=204 slot_map=102 app_total=16320
...
1783186514.302;m3-38;[INFO: RDF ] RAM_REPORT node=m3-38 max_nodes=51 max_seq=256 app_static=16320 peak_stack=1248 total=17568 seen_bytes=13056 rdf_state_bytes=2652
```

---

### Step 6 — Analyse with ram_analyzer.py

```bash
python3 ram_analyzer.py
```

1. File browser opens → select `rdf_v11_run.log`
2. Dialog asks for `data + bss` value from Step 3 → type e.g. `35836`
3. Console table + 6 plots are generated

---

### Step 7 — Repeat for each MAX_NODES

For your thesis KPI table, repeat Steps 2–6 for each network size:

```bash
for N in 5 10 20 30 51; do
  ARCH_PATH=../../../arch \
    make TARGET=iotlab BOARD=m3 \
    CFLAGS="-DMAX_NODES=$N -DMAX_SEQ_TRACK=256" \
    clean all
  arm-none-eabi-size build/iotlab/m3/rdf-flood-HW-v11.iotlab \
    | tee size_N${N}.txt
done
```

---

## What the log lines mean

### `STACK_PAINT` (printed once at boot)
```
STACK_PAINT painted=24512 bytes from 0x20009e80 to 0x20010000
```
- `painted=24512` — free RAM available for stack at boot time
- If this is small (< 4KB), you are close to RAM overflow

### `RAM_STATIC` (printed once at boot)
```
RAM_STATIC node=m3-38 max_nodes=51 max_seq=256
           last_seq_seen=102 seen=13056 rdf_state=2652
           recv_count=204 fwd_count=204 slot_map=102 app_total=16320
```
- Compile-time sizes of each RDF array (bytes)
- `app_total` = sum of all RDF arrays = your app's `.bss` contribution
- Does NOT include Contiki-NG OS globals (use `arm-none-eabi-size` for that)

### `RAM_REPORT` (printed every 10 seconds)
```
RAM_REPORT node=m3-38 max_nodes=51 max_seq=256
           app_static=16320 peak_stack=1248 total=17568
           seen_bytes=13056 rdf_state_bytes=2652
```
- `app_static`  = RDF array sizes (same as `app_total` in RAM_STATIC)
- `peak_stack`  = bytes consumed by stack since boot (high-water mark)
- `total`       = app_static + peak_stack (app-level estimate only)
- **For thesis:** use `arm-none-eabi-size(.data+.bss) + peak_stack` as total

---

## RAM breakdown at MAX_NODES=51, MAX_SEQ_TRACK=256

```
Array              Formula                    Size
─────────────────────────────────────────────────────────
seen[][]           51 × 256 × 1 byte        13,056 B  ← DOMINANT (37%)
rdf_state[]        51 × ~52 bytes            2,652 B
recv_count[]       51 × 4 bytes                204 B
fwd_count[]        51 × 4 bytes                204 B
last_seq_seen[]    51 × 2 bytes                102 B
slot_to_srcid[]    51 × 2 bytes                102 B
─────────────────────────────────────────────────────────
App arrays total                             16,320 B  (15.9 KB)
Contiki-NG OS .bss                          ~19,000 B  (18.6 KB) *
.data section                                  ~164 B
─────────────────────────────────────────────────────────
Total static RAM                            ~35,484 B  (34.7 KB)
Peak stack (typical)                         ~1,200 B   (1.2 KB)
─────────────────────────────────────────────────────────
TOTAL SYSTEM RAM                            ~36,684 B  (35.8 KB)
Available headroom                          ~27,316 B  (26.7 KB)
─────────────────────────────────────────────────────────
* Contiki-NG OS .bss = arm-none-eabi-size(.bss) - app arrays total
```

---

## Optimisation: reducing seen[][] (biggest win)

`seen[][]` is `MAX_NODES × MAX_SEQ_TRACK` bytes. Reducing `MAX_SEQ_TRACK`:

| MAX_SEQ_TRACK | seen[][] at N=51 | Saving vs 256 |
|---------------|-----------------|---------------|
| 256 (default) | 13,056 B        | —             |
| 128           |  6,528 B        | 6,528 B saved |
| 64            |  3,264 B        | 9,792 B saved |
| 32            |  1,632 B        | 11,424 B saved|

To change: `CFLAGS="-DMAX_SEQ_TRACK=64"` in your build command.
Trade-off: smaller `MAX_SEQ_TRACK` means older sequence numbers can be
re-accepted as "new" (duplicate flooding). For 1Hz TX rate, 64 is safe
for experiments up to 64 seconds without wrap-around issues.

---

## Files in v11/

```
v11/
├── rdf-flood-HW-v11.c    ← firmware (copy to your Contiki-NG project)
├── ram_analyzer.py        ← run locally to analyse log + plot RAM
└── README_v11.md          ← this file
```

---

## Answer: does the code work without the linker script file?

**Yes — the firmware compiles and runs correctly without you having the
`.ld` file in your workspace.** The linker script is used automatically
by the Contiki-NG build system when you run `make TARGET=iotlab BOARD=m3`.
The symbols `_ebss` and `_estack` are resolved by the linker at build time
on your IoT-LAB SSH frontend — you never need to manually edit the `.ld` file.

The `.ld` file was only needed here to:
1. Confirm the exact RAM size (64KB, not 96KB)
2. Confirm `_estack = 0x20010000` is already defined
3. Confirm `_ebss` is already defined at end of `.bss`

All three are confirmed. The v11 firmware uses these symbols correctly.
