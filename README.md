# Robot semi-automatique : news + Grok + Telegram + TradingView

Ce robot ne trade pas.
Il surveille les news, peut demander une analyse à Grok, et t'envoie des alertes Telegram.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Telegram
1. Ouvre Telegram
2. Cherche BotFather
3. Lance `/newbot`
4. Copie le token

Pour le chat id:
- envoie un message à ton bot
- ouvre `https://api.telegram.org/botTON_TOKEN/getUpdates`
- récupère `chat.id`

## Grok / xAI
Dans `.env`:
```env
ENABLE_GROK=true
XAI_API_KEY=ta_cle_api_xai
XAI_MODEL=grok-3-mini
XAI_BASE_URL=https://api.x.ai/v1
```

## Test
```bash
python3 bot.py --test
```

## Lancer
```bash
python3 bot.py
```
