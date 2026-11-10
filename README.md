# Distributed Backup System (SDN) - Mininet + Ryu + Python

```
                 +-- s2 --+
   h1 -- s1 -----+        +---- s4 --+-- h3 (backup server 1)
 (client)        +-- s3 --+          +-- h4 (backup server 2)
```

| File | Role |
|---|---|
| `controller.py` | Ryu app: topology discovery, Dijkstra path selection, flow rules, link-failure reroute |
| `topology.py` | Mininet topology (1 client, 2 servers, 4 switches, redundant core) |
| `client.py` | Chunking, SHA-256 per chunk + whole file, ACK/NACK, reconnect + resume, replication to all servers |
| `server.py` | Verifies chunks, stores them, supports resume, verifies whole file |
| `common.py` | Shared framing/hash helpers |
| `experiments.py` | Automated runs: normal / link failure / checksum corruption -> `results/results.csv` |
| `plot_results.py` | Graphs for the report |

## Step 1 - Install (Ubuntu 20.04/22.04)

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-pip python3-venv iperf3 wireshark
```

Ryu is fussy with new Python/eventlet, so use a venv for **the controller only**:

```bash
python3 -m venv ~/ryu-env && source ~/ryu-env/bin/activate
pip install "setuptools<66" wheel
pip install eventlet==0.30.2 ryu networkx
deactivate
```

Mininet scripts, client and server use the **system** python3 (`sudo python3 ...`), standard library only.
For plots: `pip3 install pandas matplotlib`.

## Step 2 - Start the controller (Terminal 1)

```bash
source ~/ryu-env/bin/activate
cd backup-sdn
PATH_POLICY=hops ryu-manager --observe-links controller.py     # or PATH_POLICY=load
```

## Step 3 - Start the topology (Terminal 2)

```bash
cd backup-sdn
sudo python3 topology.py
```

It waits ~6 s for link discovery, announces the hosts, and runs `pingall`. You should see 0% loss and
`HOST h1 at s1:1`, `LINK UP ...`, `PATH h1<->h3 via [s1, s2, s4]` in the controller terminal.
If not, see Troubleshooting.

## Step 4 - Manual demo (in the Mininet CLI)

```text
mininet> sh dd if=/dev/urandom of=/tmp/test.bin bs=1M count=30
mininet> h3 python3 server.py --port 5000 --dir /tmp/backup_h3 > /tmp/s3.log 2>&1 &
mininet> h4 python3 server.py --port 5000 --dir /tmp/backup_h4 > /tmp/s4.log 2>&1 &
mininet> h1 python3 client.py --servers 10.0.0.3 10.0.0.4 --file /tmp/test.bin &
```

While it runs:

```text
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1        # see installed rules
mininet> link s1 s2 down                                  # kill the active path (or s1 s3)
mininet> link s1 s2 up
```

Watch: controller prints `LINK DOWN` + `REROUTE`, client keeps going (TCP retransmits, or the app
reconnects and resumes), and finally the servers log `whole-file SHA-256 verified OK`.
Check: `sha256sum /tmp/test.bin /tmp/backup_h3/test.bin /tmp/backup_h4/test.bin`

Checksum demo: add `--corrupt-chunks 2,5` to the client -> NACKs and resends appear in the log.

## Step 5 - Automated experiments

Stop the manual topology (`exit`, then `sudo mn -c`), keep the controller running:

```bash
sudo python3 experiments.py --sizes 5 20 --reps 3
python3 plot_results.py
```

Use files large enough that the transfer lasts longer than `--fail-after` (default 1.5 s); with
20 Mbit/s links and 2 replicas, 20 MB takes ~16 s. Run once per policy (`hops`, `load`) and compare.

Metrics (CSV columns):
- **throughput** = file size / transfer time per server (includes any recovery stall)
- **recovery_s** = first ACK after the link is cut minus the cut time (max over servers)
- **max_ack_gap_s** = longest stall between ACKs; **nacks**, **reconnects**, **integrity_ok**

## Design decisions (for your report)

- **Replication** to both servers (simple, true backup); one client thread per server.
- **Chunk size 256 KB**: fine-grained resume and recovery measurement; SHA-256 per chunk + whole file.
- **Stop-and-wait** ACK/NACK, max 5 resends per chunk; server is idempotent (atomic chunk files).
- **Two-level recovery**: controller reroutes within milliseconds of the port-down event; if TCP still
  stalls past the socket timeout (5 s) the client reconnects, asks the server which chunks it has, resumes.
- **Path policy**: `hops` (baseline) vs `load` (port-stats tx rate as Dijkstra weight; decided at flow setup).
- **No flooding**: static ARP + controller-learned hosts avoids broadcast storms on the looped topology.

## Troubleshooting

- `pingall` fails: controller must start **first** and with `--observe-links`; run `sudo mn -c` and retry.
- `ryu-manager` import error (`ALREADY_HANDLED`): `pip install eventlet==0.30.2`.
- No `LINK UP` logs: Open vSwitch not running -> `sudo service openvswitch-switch start`.
- Controller port 6653 busy: `sudo mn -c`, kill old `ryu-manager`.
- Servers overwrite each other: each server needs its own `--dir`.
- Port-down not detected quickly: Ryu falls back to LLDP timeout (a few seconds); recovery is then slower.

## Report checklist

Architecture diagram, topology, protocol (INIT / CHUNK / ACK / NACK / DONE), path-selection policies,
throughput + recovery graphs, flow-table dumps before/after failure, Wireshark screenshot of the reroute,
limitations (stop-and-wait, path chosen per host pair, link-failure on s1 uplinks only in experiments).
