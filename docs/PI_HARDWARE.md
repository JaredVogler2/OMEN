# One Raspberry Pi for STONKS (APEX) and OMEN: shopping list and step-by-step plan

This page covers the Raspberry Pi 5 that runs both projects 24/7: STONKS2026's APEX worker and Discord bot, and
the OMEN Kalshi scanner. It builds on the STONKS beginner guides in `STONKS2026/raspberry-pi/` (01–08). Where a
step is the same, this page points there instead of repeating it.

## 1. What the Pi has to run

| Service | Repository | Memory cap (systemd `MemoryMax`) | Measured / expected use |
|---|---|---|---|
| `apex-worker` | STONKS2026 | 1,200 MB | market data, features, signal policy, model sync |
| `apex-discord-bot` | STONKS2026 | 600 MB | Discord Gateway bot |
| `omen` | OMEN | 900 MB | **467 MB peak** during an hourly full sweep of 169,934 markets. Lower between sweeps. |
| Raspberry Pi OS Lite + journald + SSH | — | — | about 300–500 MB |

OMEN numbers were measured on 2026-10-23:

- A full sweep (all ~170k open markets) used 12.6 CPU-seconds and peaked at 467 MB.
- A 72-hour "hot" sweep used about 9 CPU-seconds.
- These were measured on an x86 build machine. Expect roughly 2–3× the CPU time on a Pi 5, which is still well
  under 10% of one core on average at the default cadence.
- Bandwidth is about 2–3 GB/day of compressed JSON.

**Verdict:**

* **Memory.** The caps add up to about 2.7 GB, plus the OS. A **4 GB Pi 5 runs both**, with roughly 0.5–1 GB
  left for disk cache, updates (`uv sync`), backups and future growth (for example local model inference in
  APEX). An **8 GB Pi 5 gives comfortable headroom** and is the right board if you are buying now. 16 GB
  ($305 list) is not needed.
* **Storage is the real risk, not RAM.** Three services write SQLite databases and logs around the clock, and a
  microSD card wears out under that. An **SSD is the one purchase that matters for both projects**.
* **Cooling and power.** Keep the official case with its fan and the official 27 W supply. Both are already
  required by the STONKS list.

## 2. Which list is yours?

| You have… | Buy | Approx. cost |
|---|---|---|
| Nothing yet | **List A**: Pi 5 **8 GB** + SSD build | about $280–320 |
| A Pi 5 **4 GB** (the STONKS list) | **List B**: an SSD only. Upgrade the board to 8 GB later only if §6 shows memory pressure. | about $60–90 |
| A Pi 5 8 GB or 16 GB | **List B**: an SSD only | about $60–90 |

Prices are April 2026 list prices after Raspberry Pi's memory-driven increases: Pi 5 4 GB $110, 8 GB $175,
16 GB $305. Retailers vary, and SSD prices have also risen. These are planning figures, not quotes: check the
checkout total.

## 3. List A: new build (8 GB + SSD)

| # | Qty | Search for | Exact requirement | Why |
|---|---:|---|---|---|
| 1 | 1 | [Raspberry Pi 5 8GB](https://www.amazon.com/s?k=raspberry+pi+5+8gb+board) | Genuine **Raspberry Pi 5, 8 GB RAM**, board only | Room for STONKS + OMEN + growth |
| 2 | 1 | [Official Raspberry Pi 5 case with fan](https://www.amazon.com/s?k=official+raspberry+pi+5+case+fan) | **Raspberry Pi Case for Raspberry Pi 5** (fan + heatsink included) | Cooling for 24/7 load |
| 3 | 1 | [Official 27W USB-C power supply US](https://www.amazon.com/s?k=official+raspberry+pi+27w+usb+c+power+supply+us) | Official **27 W, 5.1 V 5 A**, US plug | Stable power for the Pi and a USB SSD |
| 4 | 1 | [Samsung T7 500GB USB SSD](https://www.amazon.com/s?k=samsung+t7+500gb+portable+ssd) (or SanDisk Extreme Portable SSD 500 GB) | **USB 3.2 portable SSD**, 250–500 GB, from a major brand | Boot and data drive. Endures years of database writes. |
| 5 | 1 | [SanDisk 32GB microSD A1](https://www.amazon.com/s?k=sandisk+ultra+32gb+microsd+a1) | Any branded microSDHC, 16–64 GB | Rescue/recovery card only. Not the main disk. |
| 6 | 1 if needed | [USB microSD reader](https://www.amazon.com/s?k=usb+micro+sd+card+reader+usb+a+usb+c) | Fits your laptop | Only if your laptop has no card slot |
| 7 | 1 | [Cat6 Ethernet cable 6 ft](https://www.amazon.com/s?k=cat6+ethernet+cable+6ft) | RJ45, long enough to reach the router | Wired is more reliable than Wi-Fi for a 24/7 scanner |

**Why a USB SSD rather than an internal NVMe board?** You write the operating system onto it from your laptop with
Raspberry Pi Imager, exactly like the microSD in STONKS guide 03. No extra board, no case changes, and the Pi 5
boots from USB by itself when no card is inserted. A USB 3 SSD is far faster than SQLite needs.

**Prefer everything inside the case?** Instead of item 4, buy the official
[Raspberry Pi M.2 HAT+ **Compact**](https://www.amazon.com/s?k=raspberry+pi+m.2+hat+compact) together with a
[Raspberry Pi SSD 256GB](https://www.amazon.com/s?k=raspberry+pi+ssd+256gb) (or any **M.2 2230** NVMe SSD). The
Compact is the version designed to fit around the official case's fan. The standard M.2 HAT+ only fits if you
remove the case lid and fan. Setup is in §5, option B.

**Do not buy:** a Pi 4 case (does not fit a Pi 5), a second cooler on top of the case fan, an AI HAT, the standard
M.2 HAT+ if you keep the official case lid, a generic phone charger, or both a kit and the same parts separately.

## 4. List B: you already have a Pi 5

Buy item 4 (USB SSD) and item 5 (rescue microSD) from List A. Keep your board, case, power supply and cable.

Upgrade to an 8 GB board later only if §6 shows sustained memory pressure. Moving is easy: the SSD, case and power
supply all carry over, so you only swap the board.

## 5. Step-by-step plan

Plan about two hours, most of it waiting on downloads. Each phase ends with a check. Don't start the next phase
until that check passes.

### Phase 0: if a Pi is already running STONKS, back it up

```bash
ssh apex@apex-pi.local
cd ~/STONKS2026 && ./scripts/apex-backup.sh --pi        # APEX's own backup (see STONKS docs)
sudo systemctl stop apex-worker apex-discord-bot
tar czf ~/apex-move.tgz -C ~ .config/apex .apex          # settings + local state
```

Copy `~/apex-move.tgz` to your laptop:

```bash
scp apex@apex-pi.local:apex-move.tgz .
```

✅ **Check:** the file exists on your laptop and is not empty.

### Phase 1: assemble

1. Put the board in the official case with the fan connected, as in STONKS guide 02.
2. **USB SSD:** plug it into one of the **blue** (USB 3) ports. Do not insert the microSD yet.
3. **M.2 HAT+ Compact (option B):** power off and unplug first. Fit the HAT and SSD by following the
   [official M.2 HAT+ guide](https://www.raspberrypi.com/documentation/accessories/m2-hat-plus.html), then close
   the case.

✅ **Check:** the fan cable is connected and nothing is loose.

### Phase 2: install Raspberry Pi OS onto the SSD

**Option A (USB SSD, recommended):**

1. Plug the SSD into your **laptop**.
2. In Raspberry Pi Imager, choose:
   - Device: **Raspberry Pi 5**
   - OS: **Raspberry Pi OS Lite (64-bit)**
   - Storage: **the SSD** (check the size; you are about to erase it).
3. In OS customisation, use the same settings as STONKS guide 03, A3:
   - hostname `apex-pi`
   - user `apex`
   - a long, unique password
   - timezone `America/New_York`
   - Wi-Fi (as a backup)
   - **SSH enabled**
4. Write and verify, eject, plug the SSD into a blue port on the Pi, connect Ethernet and power.
5. With no microSD inserted, the Pi 5 boots from USB.

**Option B (NVMe HAT):**

1. Write the same OS to the **microSD** as in STONKS guide 03, boot from it, and SSH in.
2. Update the firmware:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo rpi-eeprom-update        # needs a date of 2023-12-06 or later
   ```
3. Confirm `lsblk` shows `nvme0n1`.
4. Install Raspberry Pi OS onto the NVMe with Raspberry Pi Imager on the Pi, as the official M.2 HAT+ guide
   describes, using the same customisation as option A.
5. Power off, **remove the microSD**, and power on. The Pi boots from NVMe automatically when no card is
   inserted.

✅ **Check:**

- From your laptop, `ssh apex@apex-pi.local` works.
- On the Pi, `findmnt /` shows `/dev/sda2` (USB) or `/dev/nvme0n1p2` (NVMe), **not** `mmcblk0`.

### Phase 3: base setup (once)

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git
curl -LsSf https://astral.sh/uv/install.sh | sh     # the Python tool both projects use
exec $SHELL -l
sudo reboot
```

✅ **Check:**

- `uv --version` prints a version.
- `vcgencmd measure_temp` stays under about 70 °C at idle.
- `free -h` shows about 7.8 GiB total on an 8 GB board, or about 3.9 GiB on a 4 GB board.

### Phase 4: STONKS / APEX

Follow STONKS guides **04** (Discord), **05** (starter bot) and **08** (full APEX with `scripts/apex-install.sh`),
cloning into `~/STONKS2026`. **Moving from an old Pi?** Copy `apex-move.tgz` back
(`scp apex-move.tgz apex@apex-pi.local:`), then run `tar xzf ~/apex-move.tgz -C ~` **before** the installer.

✅ **Check:**

- `systemctl status apex-worker apex-discord-bot` shows both `active (running)`.
- `/apex_health` in Discord answers.

### Phase 5: OMEN

1. In the STONKS Discord server, create `#kalshi-plays`, `#kalshi-status` and, optionally, `#kalshi-research`.
   Add a webhook to each (see `docs/DISCORD.md`).
2. Install OMEN:
   ```bash
   git clone https://github.com/JaredVogler2/OMEN.git ~/OMEN && cd ~/OMEN
   scripts/install-pi.sh                       # creates ~/.config/omen/omen.env (chmod 600) and the service
   nano ~/.config/omen/omen.env                # paste the webhook URLs; optional OMEN_HEALTHCHECK_URL
   scripts/install-pi.sh --with-backup-timer   # re-render, start, nightly ledger backup
   uv run omen --env-file ~/.config/omen/omen.env discord-test
   ```

✅ **Check:**

- `systemctl status omen` shows `active (running)`.
- `#kalshi-status` shows "OMEN scanner started".
- Within 10 minutes, `journalctl -u omen -n 50` shows a `full scan` line.

### Phase 6: confirm the two projects fit together

Run these a day after setup, and again whenever something feels slow:

```bash
free -h                                     # "available" should stay above ~1 GiB
systemctl show -p MemoryCurrent -p MemoryPeak apex-worker apex-discord-bot omen
journalctl -k | grep -i -E "oom|killed process" | tail      # should print nothing
vcgencmd get_throttled                      # throttled=0x0 means power and temperature are fine
df -h /                                     # keep at least 20% free
```

**When to upgrade a 4 GB board to 8 GB:** any of the following shows up on a normal day:

- "available" in `free -h` stays under ~500 MB;
- an OOM kill;
- `MemoryPeak` of a service near its cap.

When that happens, swap the board. The SSD, case and power supply carry over, and nothing has to be reinstalled.

### Phase 7: keep it healthy

* **Backups.** APEX has its nightly backup timer, and OMEN has `omen-backup.timer`. Once a month, copy both backup
  folders to your laptop:
  ```bash
  scp -r apex@apex-pi.local:.omen/backups ./omen-backups
  ```
* **Dead-man's switch.** Create two free [healthchecks.io](https://healthchecks.io) checks, one for OMEN
  (`OMEN_HEALTHCHECK_URL`) and one for APEX if you use one. If the Pi loses power or network, you get an email.
* **Updates.** For each project:
  ```bash
  git pull && ./scripts/<installer>
  ```
  Use `apex-install.sh` for STONKS and `install-pi.sh` for OMEN. Each re-syncs exact dependency versions and
  restarts its services.
* **Rescue card.** Keep the spare microSD written with Raspberry Pi OS Lite. If the SSD ever fails to boot,
  insert the card and power on: the Pi boots from the card, and you can inspect or copy data off the SSD.

## 6. Sources

* Raspberry Pi 5 list prices after the April 2026 increase (4 GB $110, 8 GB $175, 16 GB $305):
  [Liliputing](https://liliputing.com/?p=198888) and the
  [Raspberry Pi announcement](https://www.raspberrypi.com/news/more-memory-driven-price-rises/).
* M.2 HAT+ vs. M.2 HAT+ Compact and official case compatibility, NVMe boot behaviour:
  [Raspberry Pi documentation](https://www.raspberrypi.com/documentation/accessories/m2-hat-plus.html).
* STONKS service memory caps: `STONKS2026/apps/pi-worker/deploy/systemd/*.service.in`.
* OMEN measurements: full and hot live sweeps from the build environment, 2026-10-23 (see `docs/OPERATIONS.md`).
