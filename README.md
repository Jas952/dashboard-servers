# Pearl Fleet Operations Manager

<p align="center">
  <img src="./assets-github/titlebar-main.svg" width="750" /><br>
  <img src="./assets/main.png" width="750" />
</p>

A Python-based automation and monitoring package designed to deploy, manage, and optimize Vast's high-performance GPUs.
The system implements fully autonomous fleet scaling, real-time profitability tracking, and robust recovery mechanisms with minimal manual intervention.

## Core Features

- `core/watchdog.py`: Real-time hashrate and efficiency monitor. Tracks performance metrics dynamically, identifies failing GPUs, and automatically terminates instances falling below the defined profitability threshold.
- `core/autobuy.py`: Programmatic GPU procurement bot. Scans the Vast.ai marketplace to automatically identify and rent instances that meet specific price/performance criteria (e.g., $/100TH).
- `digitalocean/do_gpu_sniper.py`: Dedicated droplet provisioner bypassing web UI bottlenecks to spin up H100/A100 instances globally based on immediate API availability.
- `core/fleet-live.py`: Terminal User Interface built with custom rendering logic to display live metrics, total fleet cost, combined computational power, and active server counts in a unified view.
- `scripts/tg_price_bot.py`: Automated market and operations tracking pushing updates directly to designated Telegram channels.

## Interface Showcase

<p align="center">
  <img src="./assets-github/titlebar-market.svg" width="750" /><br>
  <img src="./assets/market.png" width="750" />
</p>
<br>
<p align="center">
  <img src="./assets-github/titlebar-autobuy.svg" width="750" /><br>
  <img src="./assets/autobuy.png" width="750" />
</p>

## Project Architecture

```text
pearl/
├── core/                   # Main business logic (Auto-rent, Watchdog, TUI)
├── digitalocean/           # DigitalOcean specific API integration and snipers
├── scripts/                # Helper tools and Telegram reporting bots
├── data/                   # JSON data stores (blacklists, states, logs)
└── update_manual.sh        # Deployment utility scripts
```

## Setup & Installation

### Requirements
- Python 3.10+
- Vast.ai CLI (`vastai`)
- SSH Key configured for server access

### Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/pearl-fleet.git
   cd pearl-fleet
   ```

2. **Set up the virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt # Or install dependencies manually (requests, plotext, python-dotenv)
   ```

3. **Configure Environment Variables:**
   Copy the example config and add your private API keys.
   ```bash
   cp .env.example .env
   # Edit .env with your DigitalOcean Token, Telegram Bot Token, and Wallets
   ```

4. **Run the core applications:**
   The suite includes several interactive TUI (Terminal User Interface) dashboards. You can run them directly in your terminal:
   ```bash
   # Run the main fleet live dashboard
   python3 core/fleet-live.py

   # Run the auto-buyer bot
   python3 core/autobuy.py

   # Run the DigitalOcean sniper
   python3 digitalocean/do_gpu_sniper.py
   ```

### Recommended Workflow (cmux)
Since this project consists of multiple monitoring and automation scripts that need to run simultaneously, we highly recommend using a terminal multiplexer to split your screen and run them side-by-side. 

**[cmux (Console Multiplexer)](https://github.com/manaflow-ai/cmux)** is an excellent, lightweight option for this:
1. Install cmux following the instructions on their GitHub.
2. Open your terminal and start a new cmux session.
3. Split your terminal window into multiple panes.
4. Run `python3 core/fleet-live.py` in the main pane to monitor your fleet.
5. Run `python3 core/autobuy.py` in a side pane to automate procurement.

## Security & Best Practices
- **API Keys:** Never hardcode API keys or Wallet Addresses. Always utilize the `.env` file implementation.
- **SSH Access:** Ensure your `~/.ssh/vast_key` is correctly configured and has strict permissions (`chmod 600`).
- **Rate Limits:** The bot implements "Ghost Cooldown" periods (e.g., in DigitalOcean snipers) to respect provider API rate limits (5,000 requests/hour limit).

<p>
  <img src="./assets-github/n1.gif" alt="Project Demo" width="92" height="92" align="left"/>
</p>
<pre hspace="12">
  <img src="./assets-github/contacts/tg.jpg" alt="Telegram" height="14" /> Telegram ······ <a href="https://t.me/Jas953/">t.me/Jas953</a>
  <img src="./assets-github/contacts/lnk.jpg" alt="LinkedIn" height="14" /> LinkedIn ······ <a href="https://www.linkedin.com/in/jas952/">linkedin.com/in/jas952</a>
  <img src="./assets-github/contacts/x.jpg" alt="X" height="14" /> X        ······ <a href="https://x.com/not__jas">x.com/not__jas</a>
</pre>
<br clear="left" />

