# Fleet Migration Guide: Optiplex 7050

**Generated:** 2026-04-06
**Source machine:** Current rig (Windows 11 Home 10.0.26200)
**Target machine:** Dell Optiplex 7050

---

## Inventory Summary

| Category | Size | Notes |
|---|---|---|
| Total D:\ drive | 23 GB | Includes non-fleet files |
| CommandCenter | 426 MB | Includes 130 MB logs, 109 MB brainiac, 182 MB audio |
| TrekBot | 109 MB | Only git repo besides CC |
| HiveMind | 579 MB | Largest non-CC bot (no git) |
| Oracle | 126 MB | No git |
| Ollama models | 18 GB | 5 models (gemma2:2b, phi4-mini, mistral-nemo, qwen3.5:4b, deepseek-r1:7b) |
| HuggingFace models | 1.3 GB | D:\models\huggingface |
| All other bots | ~40 MB combined | 14 bots, no git repos |
| Configs + Icons | ~525 KB | D:\configs (9 YAML strategy files), D:\icons (25 bot icons) |

**GPU:** Tesla P4, 7680 MiB, driver 538.95
**Python:** 3.14.3 with 182 packages installed
**Ollama:** v0.20.0

---

## Step 1: Install Prerequisites

### 1a. Python 3.14.x

Download from python.org. During install, check "Add to PATH".

```
python --version
# must be 3.14.x
```

### 1b. Git for Windows

Install Git for Windows (includes Git Bash). The fleet is launched from Git Bash.

### 1c. pip packages

Export current packages on the source machine:

```bash
pip freeze > D:/CommandCenter/requirements_full.txt
```

On the new machine, install the fleet-critical subset first:

```bash
pip install requests ccxt hmmlearn numpy pandas scipy scikit-learn TA-Lib psutil plotly pyTelegramBotAPI flask loguru torch transformers
```

Then install the full list:

```bash
pip install -r requirements_full.txt
```

**Note:** `TA-Lib` requires a C library. On Windows, install via the unofficial wheel:
```
pip install TA-Lib
```
If that fails, download the `.whl` from https://github.com/cgohlke/talib-build/releases.

**Note:** `cupy-cuda12x` and `torch` require CUDA. Ensure the Tesla P4 drivers are installed on the Optiplex before installing these.

### 1d. Ollama

Download from https://ollama.com. Install, then pull the 5 models:

```bash
ollama pull gemma2:2b
ollama pull phi4-mini
ollama pull mistral-nemo
ollama pull qwen3.5:4b
ollama pull deepseek-r1:7b
```

Total download: ~19 GB.

**Important:** Set the `OLLAMA_MODELS` environment variable if you want models stored on D:\ (as they are now):
```
OLLAMA_MODELS=D:\ollama\models
```
Set this in Windows System Environment Variables before pulling models.

### 1e. NVIDIA Driver

Install the latest NVIDIA driver for the Tesla P4. Current driver is 538.95. The Optiplex 7050 needs:
- PCIe slot with sufficient power for the Tesla P4
- NVIDIA driver installed
- CUDA toolkit (if not bundled with PyTorch)

Verify:
```bash
nvidia-smi
```

---

## Step 2: Clone Git Repos

Only 2 directories have git repos. Everything else must be copied manually.

### CommandCenter (the main repo)

```bash
cd D:\
git clone https://github.com/Rors78/Oracle.git CommandCenter
```

**Warning:** The remote is named `Oracle.git` but it clones into `CommandCenter` locally. Use the directory name override as shown.

After cloning, check out the correct branch:
```bash
cd D:\CommandCenter
git log --oneline -5
# verify: 0b930c0 is the latest commit
```

### TrekBot (separate repo, no remote configured)

TrekBot has **no remote**. It must be copied manually (see Step 3). If you want to add a remote first:

```bash
cd D:\TrekBot
git remote -v
# (empty — no remote)
```

**Recommendation:** Before migration, push TrekBot to a private GitHub repo:
```bash
cd D:\TrekBot
gh repo create Rors78/TrekBot --private --source=. --push
```

---

## Step 3: Copy Non-Git Directories

These 15 bot directories have **no git repos** and must be copied manually via USB drive, network share, or robocopy:

```
D:\Aegis\              (53 KB)
D:\Arbitrageur\        (2.4 MB)  -- has arbitrageur_state.json
D:\Chronos\            (149 KB)
D:\Contrarian\         (14 MB)
D:\Gridzilla\          (1.6 MB)  -- has gridzilla_state.json
D:\HiveMind\           (579 MB)
D:\NexusBrain\         (713 KB)
D:\Nexus\              (189 KB)
D:\Oracle\             (126 MB)
D:\PhiTex\             (61 KB)
D:\Rubberband\         (1.1 MB)
D:\Sentinel\           (45 KB)
D:\Trinity\            (206 KB)
D:\TurtleSue\          (1.3 MB)  -- has bot_state.json, blocked_coins.json
D:\Whale Watcher\      (19 MB)
```

Plus supporting directories:

```
D:\configs\            (17 KB)   -- 9 YAML strategy files
D:\icons\              (508 KB)  -- 25 bot .ico/.png files
D:\models\             (1.3 GB)  -- HuggingFace models
```

### Robocopy command (run from source machine with D:\ mapped to target):

```cmd
robocopy D:\Aegis \\OPTIPLEX\D$\Aegis /E /Z
robocopy D:\Arbitrageur \\OPTIPLEX\D$\Arbitrageur /E /Z
robocopy D:\Chronos \\OPTIPLEX\D$\Chronos /E /Z
robocopy D:\Contrarian \\OPTIPLEX\D$\Contrarian /E /Z
robocopy D:\Gridzilla \\OPTIPLEX\D$\Gridzilla /E /Z
robocopy D:\HiveMind \\OPTIPLEX\D$\HiveMind /E /Z
robocopy D:\NexusBrain \\OPTIPLEX\D$\NexusBrain /E /Z
robocopy D:\Nexus \\OPTIPLEX\D$\Nexus /E /Z
robocopy D:\Oracle \\OPTIPLEX\D$\Oracle /E /Z
robocopy D:\PhiTex \\OPTIPLEX\D$\PhiTex /E /Z
robocopy D:\Rubberband \\OPTIPLEX\D$\Rubberband /E /Z
robocopy D:\Sentinel \\OPTIPLEX\D$\Sentinel /E /Z
robocopy D:\Trinity \\OPTIPLEX\D$\Trinity /E /Z
robocopy D:\TrekBot \\OPTIPLEX\D$\TrekBot /E /Z
robocopy D:\TurtleSue \\OPTIPLEX\D$\TurtleSue /E /Z
robocopy "D:\Whale Watcher" "\\OPTIPLEX\D$\Whale Watcher" /E /Z
robocopy D:\configs \\OPTIPLEX\D$\configs /E /Z
robocopy D:\icons \\OPTIPLEX\D$\icons /E /Z
robocopy D:\models \\OPTIPLEX\D$\models /E /Z
```

---

## Step 4: Copy Non-Git Files in CommandCenter

After cloning CommandCenter from GitHub, these files are NOT in the repo and must be copied manually:

### Critical State Files
```
D:\CommandCenter\portfolio.json          (54 KB) -- live portfolio state
D:\CommandCenter\aggregator_state.json   (136 bytes)
D:\CommandCenter\notifier_config.json    -- notifier settings
```

### Logs and Brainiac Data
```
D:\CommandCenter\logs\                   (130 MB) -- snapshots, events, daily, journals, evolution, ultron, weekly
D:\CommandCenter\brainiac\               (109 MB) -- depth, trades, correlations, funding, metrics
```

### Audio Files (dashboard ambient sounds)
```
D:\CommandCenter\audio\earth_chorus.mp3  (41 MB)
D:\CommandCenter\audio\jupiter.mp3       (35 MB)
D:\CommandCenter\audio\saturn.mp3        (39 MB)
D:\CommandCenter\audio\solar_wind.wav    (67 MB)
```

### Other
```
D:\CommandCenter\audits\                 -- audit report JSONs
D:\CommandCenter\indicators.py           -- not in git
D:\CommandCenter\kraken_ohlc.py          -- not in git
```

### Robocopy for CC non-git files:
```cmd
robocopy D:\CommandCenter\logs \\OPTIPLEX\D$\CommandCenter\logs /E /Z
robocopy D:\CommandCenter\brainiac \\OPTIPLEX\D$\CommandCenter\brainiac /E /Z
robocopy D:\CommandCenter\audio \\OPTIPLEX\D$\CommandCenter\audio /E /Z
robocopy D:\CommandCenter\audits \\OPTIPLEX\D$\CommandCenter\audits /E /Z
copy D:\CommandCenter\portfolio.json \\OPTIPLEX\D$\CommandCenter\
copy D:\CommandCenter\aggregator_state.json \\OPTIPLEX\D$\CommandCenter\
copy D:\CommandCenter\notifier_config.json \\OPTIPLEX\D$\CommandCenter\
copy D:\CommandCenter\indicators.py \\OPTIPLEX\D$\CommandCenter\
copy D:\CommandCenter\kraken_ohlc.py \\OPTIPLEX\D$\CommandCenter\
```

---

## Step 5: Drive Letter Configuration

**The fleet is designed to be drive-letter-agnostic.** `fleet_config.py` derives everything from its own location:

```python
CC_DIR = os.path.dirname(os.path.abspath(__file__))        # D:\CommandCenter
DATA_DRIVE = os.path.splitdrive(CC_DIR)[0] + os.sep        # D:\
```

All bot directories are built as `os.path.join(DATA_DRIVE, "BotName")`.

**If CommandCenter is on D:\ on the new machine:** Nothing to change. It just works.

**If CommandCenter is on a different drive (e.g., E:\):** Still nothing to change in code -- `DATA_DRIVE` will auto-detect `E:\`. Just make sure ALL bot directories are on the same drive as CommandCenter.

**Rule:** All 17 bot directories + configs + models + icons must be on the same drive as `D:\CommandCenter`. The code does not support split-drive layouts.

---

## Step 6: Kraken API Keys

API keys are loaded from **Windows environment variables**, not files. No `.env` files exist.

### Required environment variables:

| Variable | Used By | Purpose |
|---|---|---|
| `KRAKEN_API_KEY` | TrekBot, Gridzilla | Kraken trading API key |
| `KRAKEN_API_SECRET` | Gridzilla | Kraken trading API secret |

**TrekBot** checks `os.environ.get('KRAKEN_API_KEY')`. If missing, it runs in **PAPER MODE** (simulated trades).

**Gridzilla** reads both `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` from environment.

### How to set on new machine:

1. Open Windows Settings > System > About > Advanced system settings > Environment Variables
2. Under **System variables**, add:
   - `KRAKEN_API_KEY` = your Kraken API key
   - `KRAKEN_API_SECRET` = your Kraken API secret
3. Restart any open terminals for the change to take effect

### Verify:
```bash
echo $KRAKEN_API_KEY
# should print your key (not empty)
```

**Other bots** (TurtleSue, NexusBrain, Rubberband, Arbitrageur, Contrarian) do not appear to use direct Kraken API keys -- they trade through the central portfolio manager in CommandCenter which proxies market data. Verify by checking each bot's source after migration.

---

## Step 7: Telegram Notifier

The notifier (`notifier.py`) uses two environment variables:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot API token |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID for alerts |

These must be set as Windows system environment variables (same process as Kraken keys above).

The notifier will **refuse to start** if these are not set:
```
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set. Exiting.
```

Config file `notifier_config.json` is already copied in Step 4. No changes needed -- all ports/intervals are correct.

### Verify:
```bash
python notifier.py
# Should print startup message and send a Telegram test
```

---

## Step 8: Ollama Environment Variable

One critical environment variable for Ollama:

| Variable | Current Value | Purpose |
|---|---|---|
| `OLLAMA_MODELS` | `D:\ollama\models` | Where Ollama stores model files |

Set this in Windows System Environment Variables before pulling models. If not set, Ollama defaults to `C:\Users\<user>\.ollama\models` which may fill up the C: drive (models total ~19 GB).

The inference server (`inference_server.py`) searches for models in this priority order:
1. `gemma2:2b` (1.6 GB, fastest)
2. `phi4-mini` (2.5 GB)
3. `mistral-nemo` (7.1 GB)
4. `qwen3.5:4b` (3.4 GB)
5. `deepseek-r1:7b` (4.7 GB)
6. First available

At minimum, pull `gemma2:2b` for the fleet to function with AI features.

---

## Step 9: First Launch and Verification

### 9a. Pre-flight checklist

Run these checks before launching:

```bash
# Python works
python --version

# Key packages installed
python -c "import requests, ccxt, numpy, pandas, hmmlearn, talib; print('OK')"

# GPU visible
nvidia-smi

# Ollama running
ollama list

# Kraken keys set
echo $KRAKEN_API_KEY | head -c 5
echo $KRAKEN_API_SECRET | head -c 5

# Telegram keys set
echo $TELEGRAM_BOT_TOKEN | head -c 10
echo $TELEGRAM_CHAT_ID

# All bot dirs exist
for dir in TurtleSue Sentinel Trinity HiveMind NexusBrain TrekBot Oracle "Whale Watcher" Gridzilla PhiTex Aegis Nexus Rubberband Contrarian Arbitrageur Chronos; do
    [ -d "D:/$dir" ] && echo "OK: $dir" || echo "MISSING: $dir"
done

# Portfolio state exists
[ -f D:/CommandCenter/portfolio.json ] && echo "OK: portfolio.json" || echo "MISSING: portfolio.json"
```

### 9b. Launch the fleet

```bash
cd D:\CommandCenter
python launch_fleet.py
```

This launches in two phases:
- **Phase 1:** TurtleSue, Trinity, HiveMind, NexusBrain, Oracle, Deep Blue, Gridzilla, TrekBot (core bots)
- **Phase 2 (after CC binds port 9000, ~15s):** Sentinel, PHITEX, AEGIS, Inference, NEXUS, Rubberband, Contrarian, Arbitrageur, TrekBot SHORT, Chronos

### 9c. Verification

```bash
# Dashboard loads
curl -s http://localhost:9000/ | head -1
# should return HTML

# All bots reporting
curl -s http://localhost:9000/api/master | python -c "
import sys, json
d = json.load(sys.stdin)
bots = d.get('bots', {})
print(f'Bots reporting: {len(bots)}/17')
for name, info in sorted(bots.items()):
    status = 'ALIVE' if info.get('pnl') is not None or info.get('equity') else 'DOWN'
    print(f'  {name:20s} {status}')
"

# Portfolio working
curl -s http://localhost:9000/api/portfolio/available
# should return a number

# Event bus active
curl -s "http://localhost:9000/api/events/recent?n=5" | python -m json.tool | head -20

# Inference server
curl -s http://localhost:9001/health
# should return {"status": "running", "model": "gemma2:2b", ...}

# Notifier (run in separate terminal)
python notifier.py
# should send Telegram startup message
```

### 9d. Expected startup time

- Phase 1 bots: ~30 seconds (HiveMind and TrekBot are slow starters)
- Command Center binds: ~15 seconds after Phase 1
- Phase 2 bots: ~30 seconds after CC
- Full fleet alive: **~75 seconds** from launch

---

## Step 10: Bot State Files to Preserve

These files contain live trading state. If you lose them, bots restart with clean state (no open positions, no trade history):

| File | Bot | Contains |
|---|---|---|
| `D:\CommandCenter\portfolio.json` | CC | Central portfolio: reservations, history, capital allocation |
| `D:\CommandCenter\logs\expectancy.json` | CC | Fleet-wide expectancy tracking |
| `D:\CommandCenter\aggregator_state.json` | CC | Aggregator state |
| `D:\Gridzilla\gridzilla_state.json` | Gridzilla | Grid positions, equity, trade history, grid config |
| `D:\Arbitrageur\arbitrageur_state.json` | Arbitrageur | Arbitrage state |
| `D:\TurtleSue\turtle-bot\bot_state.json` | TurtleSue | Bot balance, positions |
| `D:\TurtleSue\turtle-bot\blocked_coins.json` | TurtleSue | Coins blocked from trading |

---

## Step 11: Things You Can Skip

These are **not needed** on the new machine:

| Item | Why |
|---|---|
| `D:\$RECYCLE.BIN\` | Windows recycle bin |
| `D:\System Volume Information\` | Windows system |
| `D:\BTC Reversal Monitor\` | Standalone tool, not part of fleet |
| `D:\DadsMedicalReview\` | Non-fleet project |
| `D:\GracefulTreeSearch\` | Non-fleet project |
| `D:\golden-wars-v8.5\` | Game/unrelated |
| `D:\youtubetemp\` | Temp files |
| `D:\Miner\` | Mining software (separate concern) |
| `D:\Viper\` | Not in fleet registry (not launched by fleet) |
| `D:\Docker Desktop Installer.exe` | Installer (re-download if needed) |
| `D:\OllamaSetup.exe` | Installer (re-download) |
| `D:\output\` | Temp output |
| `D:\CommandCenter\__pycache__\` | Python cache (auto-regenerated) |
| `D:\CommandCenter\pids\` | PID files (auto-created on launch) |

---

## Complete Environment Variables Checklist

Set all of these in Windows System Environment Variables on the new machine:

| Variable | Value | Required? |
|---|---|---|
| `KRAKEN_API_KEY` | Your Kraken API key | Yes (without it, bots run in paper mode) |
| `KRAKEN_API_SECRET` | Your Kraken API secret | Yes (for Gridzilla) |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token | Yes (for notifier) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID | Yes (for notifier) |
| `OLLAMA_MODELS` | `D:\ollama\models` | Recommended (keeps models on D:\) |

---

## Quick Migration Checklist

- [ ] Install Python 3.14.x + add to PATH
- [ ] Install Git for Windows
- [ ] Install NVIDIA Tesla P4 driver
- [ ] Install Ollama v0.20+
- [ ] Set environment variables (KRAKEN_API_KEY, KRAKEN_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OLLAMA_MODELS)
- [ ] Clone CommandCenter: `git clone https://github.com/Rors78/Oracle.git CommandCenter`
- [ ] Push TrekBot to private repo first, then clone (or copy manually)
- [ ] Copy 15 non-git bot directories to D:\
- [ ] Copy D:\configs, D:\icons, D:\models to D:\
- [ ] Copy CommandCenter non-git files: portfolio.json, aggregator_state.json, notifier_config.json, logs/, brainiac/, audio/, audits/, indicators.py, kraken_ohlc.py
- [ ] Copy bot state files: gridzilla_state.json, arbitrageur_state.json, bot_state.json, blocked_coins.json
- [ ] `pip install -r requirements_full.txt`
- [ ] Pull Ollama models (at minimum gemma2:2b)
- [ ] Run pre-flight checklist (Step 9a)
- [ ] Launch fleet: `cd D:\CommandCenter && python launch_fleet.py`
- [ ] Verify dashboard at http://localhost:9000
- [ ] Verify all 17 bots reporting via /api/master
- [ ] Verify Telegram notifier sends startup message
- [ ] Start notifier in separate terminal: `python notifier.py`
