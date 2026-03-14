# CryptoBot Pro — Cloud Deployment Guide

Full algo trading system. Deploy once, runs 24/7 from any browser anywhere.

---

## FASTEST DEPLOY: Railway (Free, 5 minutes)

Railway gives you a free server that runs 24/7.

### Step 1 — Push to GitHub
1. Create a free account at github.com
2. Create a new repository called `cryptobot`
3. Upload all these files to it

### Step 2 — Deploy on Railway
1. Go to railway.app — sign up free with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your `cryptobot` repository
4. Railway auto-detects Python and deploys

### Step 3 — Set environment variables
In Railway dashboard → your project → **Variables** tab, add:
```
ADMIN_PASSWORD = your_secret_password_here
SECRET_KEY     = any_random_string_here
DATA_DIR       = /tmp
```

### Step 4 — Access your app
Railway gives you a URL like `https://cryptobot-production.up.railway.app`
Open it from any browser, anywhere, anytime.

---

## ALTERNATIVE: Render (Free)

1. Go to render.com → New → Web Service
2. Connect your GitHub repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
5. Add environment variables (same as above)

---

## ALTERNATIVE: AWS (Cheapest paid option ~$3.50/month)

### Using EC2 t3.micro
```bash
# 1. Launch EC2 t3.micro with Ubuntu 22.04
# 2. SSH into it and run:

sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/YOUR_USERNAME/cryptobot.git
cd cryptobot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Set environment variables
export ADMIN_PASSWORD="your_password"
export SECRET_KEY="random_string"
export DATA_DIR="/home/ubuntu/data"
mkdir -p /home/ubuntu/data

# Run with screen so it stays on after you close SSH
sudo apt install screen
screen -S bot
gunicorn app:app --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 120

# Press Ctrl+A then D to detach (bot keeps running)
```

Open port 8000 in your EC2 Security Group inbound rules.
Access at: http://YOUR_EC2_IP:8000

---

## LOCAL (your laptop)

```bash
pip install -r requirements.txt
python app.py
# Open: http://localhost:8000
```

---

## HOW TO USE

### First time setup
1. Open the URL in your browser
2. Enter your ADMIN_PASSWORD (default: admin123 — change it!)
3. Paste your Binance API Key and Secret in the left sidebar
4. Choose Testnet (safe) or Live
5. Click "Connect & Test" — you'll see your balance
6. Adjust settings (RR ratio, risk %, symbol, interval)
7. Click "Save settings"
8. Click "Start bot" — it runs 24/7

### Tweakable settings
| Setting | Default | What it does |
|---|---|---|
| Symbol | BTCUSDT | Which crypto to trade |
| Interval | 5m | Candle timeframe |
| Capital | $20 | Your trading capital |
| Risk/trade | 2% | Max $ risked per trade ($0.40 on $20) |
| RR target | 1:3 | Take profit = 3× the risk |
| Tiny candle % | 0.05% | Threshold to flag tiny candles |
| EMA fast/slow | 9/21 | Trend detection periods |
| RSI period | 14 | Momentum indicator |
| Direction | both | Long only / Short only / Both |
| Max trades/day | 10 | Safety cap |

---

## STRATEGY LOGIC

The bot uses a multi-factor scoring system:

1. **Tiny candle detection** — flags candles with H-L range < threshold%
2. **EMA trend** — EMA9 vs EMA21 vs EMA50 alignment (+2 score)
3. **Higher timeframe (1h)** — confirms direction on 1h chart (+1)
4. **RSI** — oversold=long signal, overbought=short signal (+2)
5. **Price vs EMA** — above/below EMA9 (+1)
6. **Volume** — confirms with above-average volume (+1)
7. **Support/Resistance** — proximity to key levels (+1)

A trade is only taken when score ≥ 5 out of 12. This filters weak signals.

Entry: current price
SL: previous candle low (long) or high (short) + ATR buffer
TP: entry ± (candle range × RR multiplier)

---

## IMPORTANT WARNINGS

⚠ Past performance does not guarantee future results
⚠ Crypto trading carries significant risk of loss
⚠ Always test on TESTNET before using real money
⚠ Never invest more than you can afford to lose completely
⚠ The bot can and will have losing trades — that is normal
⚠ Change your ADMIN_PASSWORD before deploying to cloud
