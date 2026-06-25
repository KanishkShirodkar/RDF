# FIT IoT-LAB M3 Node — Contiki-NG Flooding App Deployment

Deploying a Contiki-NG UDP flooding application (`udp-client.c`) on physical M3 nodes at FIT IoT-LAB Strasbourg.

---

## Prerequisites

- FIT IoT-LAB account at [iot-lab.info](https://www.iot-lab.info)
- SSH key registered in the IoT-LAB web portal (Account → SSH Keys)
- Contiki-NG installed locally at `/home/kanishk/contiki-ng`

---

## Step 1 — Generate and Register SSH Key (One-Time)

```bash
# On local machine
ssh-keygen -t rsa
cat ~/.ssh/id_rsa.pub
# Paste output into iot-lab.info → Account → SSH Keys
```

---

## Step 2 — Upload Project Files to Strasbourg

```bash
# On local machine — create remote folder and upload both files
ssh shirodka@strasbourg.iot-lab.info "mkdir -p ~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB"

scp /home/kanishk/contiki-ng/examples/FITIOTLAB/udp-client.c \
    shirodka@strasbourg.iot-lab.info:~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB/

scp /home/kanishk/contiki-ng/examples/FITIOTLAB/Makefile \
    shirodka@strasbourg.iot-lab.info:~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB/
```

**Makefile contents:**
```makefile
CONTIKI_PROJECT = udp-client
all: $(CONTIKI_PROJECT)
CONTIKI = ../..
CFLAGS += -Wno-error
CFLAGS += -Wno-implicit-function-declaration
include $(CONTIKI)/Makefile.include
```

---

## Step 3 — SSH into Strasbourg

```bash
ssh shirodka@strasbourg.iot-lab.info
```

---

## Step 4 — Clone IoT-LAB Contiki-NG Arch Drivers (One-Time)

```bash
# On Strasbourg — only needed once
cd ~
git clone https://github.com/iot-lab/iot-lab-contiki-ng.git
cd iot-lab-contiki-ng
git submodule update --init
```

---

## Step 5 — Compile for M3 Board

```bash
cd ~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB

# Save target (once)
ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3 savetarget

# Compile
ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3
```

**Verify firmware was created:**
```bash
ls build/iotlab/m3/
# Expected: udp-client.iotlab
```

---

## Step 6 — Authenticate CLI Tools (One-Time)

```bash
iotlab-auth -u shirodka
# Enter your iot-lab.info password when prompted
```

---

## Step 7 — Reserve M3 Nodes

**Option A — Via web portal:**  
Go to `iot-lab.info` → New Experiment → Site: Strasbourg → Board: M3 → Quantity: 5

**Option B — Via CLI:**
```bash
iotlab-experiment submit -n flooding-test -d 60 -l strasbourg,m3,5
iotlab-experiment wait
```

**Check assigned node IDs:**
```bash
iotlab-experiment get -ni
```

---

## Step 8 — Flash Firmware onto All Nodes

```bash
iotlab-node --flash ~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB/build/iotlab/m3/udp-client.iotlab
```

---

## Step 9 — Read Live Serial Output

```bash
# Live output on screen
serial_aggregator

# Save to file for analysis
serial_aggregator > ~/FITIOTLAB_results.log
# Press Ctrl+C to stop
```

---

## Re-deploy After Code Changes

```bash
# 1. Upload updated file from local machine
scp /home/kanishk/contiki-ng/examples/FITIOTLAB/udp-client.c \
    shirodka@strasbourg.iot-lab.info:~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB/

# 2. On Strasbourg — clean, recompile, reflash
cd ~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB
ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3 clean
ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3
iotlab-node --flash ~/iot-lab-contiki-ng/contiki-ng/examples/FITIOTLAB/build/iotlab/m3/udp-client.iotlab
serial_aggregator
```

---

## Key Fixes Applied to udp-client.c

| Issue | Fix |
|---|---|
| `random.h` not found | Changed to `lib/random.h` |
| Missing includes | Added `<inttypes.h>` and `<string.h>` |
| HARD FAULT crash | Used `src % MAX_NODES` as array index instead of raw `src` |
| All packets dropped | Removed `valid_src()` check that rejected large hardware node IDs |
| RAM overflow | Reduced `MAX_NODES` to 50, `MAX_SEQ_TRACK` to 16 |
| Packets stop at seq=16 | Pre-clear next `seen[]` slot after marking current one |
| Bogus delay values | Guard `now > msg.origin_time` before computing delay |

---

## Important Notes

- M3 node IDs on real hardware are large 16-bit MAC-derived numbers (e.g. `37767`), not small integers like in Cooja
- M3 has only **16 KB RAM** — keep static arrays small
- `--update` flag is deprecated; use `--flash` instead
- Strasbourg has 105 M3 nodes; free accounts can reserve up to 100




//////////////////////////////////////////////////////////////////////////////////////////////
# RDF Flooding — IoT-LAB M3 Hardware Run Instructions


## Step 1 — SSH to the IoT-LAB Frontend

IoT-LAB has multiple sites. Pick one with M3 nodes available (Grenoble, Saclay, Lille, Strasbourg, etc.).

```bash
ssh <your_login>@grenoble.iot-lab.info
# or: saclay / lille / strasbourg / paris / lyon
```

Check available M3 nodes at your site:
```bash
iotlab-status --nodes --site grenoble | grep m3 | head -20
```


## Step 7 — Collect Serial Logs

IoT-LAB provides a **serial aggregator** that collects UART output from all
nodes and prefixes each line with the node ID and timestamp.

### Method A: IoT-LAB Serial Aggregator (recommended)

```bash
# Start the serial aggregator — runs in foreground, Ctrl+C to stop
# Logs are prefixed: <timestamp>;<node_id>;<log_line>
serial_aggregator -i $EXP_ID 2>&1 | tee rdf_log_$(date +%Y%m%d_%H%M%S).txt

# Let it run for at least 120 seconds (> 2× Tam = 90 s)
# Recommended: 300 seconds for a full KPI evaluation run
```

### Method C: Background collection

```bash
# Run aggregator in background for 5 minutes, then stop
timeout 300 serial_aggregator -i $EXP_ID > rdf_log.txt 2>&1 &
echo "Collecting logs for 300 seconds..."
wait
echo "Done. Log saved to rdf_log.txt"
```

---

## Step 8 — Download Logs to Local Machine

From your **local machine**:

```bash
scp <your_login>@grenoble.iot-lab.info:~/rdf-flood/rdf_log_*.txt ./local path
```

---


## Step 10 — Stop the Experiment

```bash
iotlab-experiment stop -i $EXP_ID
```

---




## Quick Reference — Key Log Lines

| Log tag | Meaning |
|---|---|
| `INIT_ID` | Node boot: MAC → node_id mapping |
| `POS` | Grid coordinates assigned |
| `TX` | Node transmitted its own packet |
| `TX_ORIGIN` | Source TX with timestamp (denominator for P_D) |
| `RX` | First reception of a new (src, seq) |
| `ARRIVE` | Every reception (new + duplicate) |
| `FWD` | Node forwarded a packet |
| `CBF suppress` | Overhearing suppression fired |
| `AOI_OK` | Packet received within Taoi threshold |
| `EXCESS` | Packet received with AoI > Taoi (P_EX event) |
| `PDR` | Running per-source packet delivery ratio |
| `DISSEM` | Dissemination snapshot for post-processing |
| `STATS` | Periodic summary (every 10 s) |
