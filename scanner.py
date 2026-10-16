#!/usr/bin/env python3
"""
Rugscaningbot v2: free pump.fun scanner with a Telegram menu.
READ-ONLY: never touches a wallet or private key.
Usage:
  python scanner.py                 # run the bot
  python scanner.py --test <MINT>   # run the checks on one coin now
"""
import asyncio
import html
import json
import logging
import os
import signal
import sys
import time
import uuid

import aiohttp
import websockets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _f(name, default):
    return float(os.getenv(name, default))


# ---------- config ----------
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
HELIUS_KEY = os.getenv("HELIUS_API_KEY", "")

WAIT_SECONDS = int(_f("WAIT_SECONDS", 600))
CHECK_MIGRATIONS = os.getenv("CHECK_MIGRATIONS", "1") == "1"

MAX_INSIDERS_PCT = _f("MAX_INSIDERS_PCT", 20)
MAX_TOP10_PCT = _f("MAX_TOP10_PCT", 30)
MAX_DEV_COINS = int(_
