import os
import time
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "")
PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "25000"))
MAX_POSITION_SOL = float(os.getenv("MAX_POSITION_SOL", "0.05"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "12.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "25.0"))

app = FastAPI(title="Solana Trend Bot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE: Dict[str, Any] = {
    "running": False,
    "paper_trade": PAPER_TRADE,
    "strategy": "Breakout",
    "risk_level": "Balanced",
    "last_slot": None,
    "last_check": None,
    "paper_balance_sol": 1.0,
    "open_positions": [],
    "trades": [],
    "logs": [],
}

lock = threading.Lock()


class StrategyUpdate(BaseModel):
    strategy: str
    risk_level: str


def add_log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    STATE["logs"].insert(0, f"[{ts}] {message}")
    STATE["logs"] = STATE["logs"][:50]


def fetch_latest_slot() -> Optional[int]:
    if not SOLANA_RPC_URL:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
    with httpx.Client(timeout=8.0) as client:
        resp = client.post(SOLANA_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result")


def evaluate_trend_candidates() -> List[Dict[str, Any]]:
    return [
        {
            "symbol": "SAMPLE1",
            "price_usd": 0.0042,
            "liquidity_usd": 68000,
            "breakout_pct": 14.5,
            "volume_spike_x": 2.4,
        },
        {
            "symbol": "SAMPLE2",
            "price_usd": 0.0011,
            "liquidity_usd": 12000,
            "breakout_pct": 19.0,
            "volume_spike_x": 3.1,
        },
    ]


def step_bot_cycle() -> None:
    with lock:
        STATE["last_check"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            slot = fetch_latest_slot()
            if slot is not None:
                STATE["last_slot"] = slot
        except Exception as exc:
            add_log(f"RPC check warning: {exc}")

        if not STATE["running"]:
            return

        for token in evaluate_trend_candidates():
            already_open = any(p["symbol"] == token["symbol"] for p in STATE["open_positions"])
            if already_open:
                continue

            if (
                token["liquidity_usd"] >= MIN_LIQUIDITY_USD
                and token["breakout_pct"] >= 10.0
                and token["volume_spike_x"] >= 1.5
            ):
                size_sol = min(MAX_POSITION_SOL, STATE["paper_balance_sol"] * 0.1)
                if size_sol <= 0:
                    continue

                STATE["paper_balance_sol"] = round(STATE["paper_balance_sol"] - size_sol, 4)
                pos = {
                    "symbol": token["symbol"],
                    "entry_price_usd": token["price_usd"],
                    "size_sol": size_sol,
                    "stop_loss_pct": STOP_LOSS_PCT,
                    "take_profit_pct": TAKE_PROFIT_PCT,
                    "mode": "PAPER" if STATE["paper_trade"] else "LIVE_DISABLED",
                }
                STATE["open_positions"].append(pos)
                add_log(
                    f"[PAPER ENTRY] {token['symbol']} | size={size_sol} SOL | "
                    f"strategy={STATE['strategy']} ({STATE['risk_level']})"
                )


def worker_loop() -> None:
    while True:
        step_bot_cycle()
        time.sleep(max(5, POLL_INTERVAL_SECONDS))


@app.on_event("startup")
def on_startup() -> None:
    add_log("Backend started in safe paper-trade default mode.")
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()


@app.get("/api/state")
def get_state() -> Dict[str, Any]:
    with lock:
        return dict(STATE)


@app.post("/api/session/toggle")
def toggle_session() -> Dict[str, Any]:
    with lock:
        STATE["running"] = not STATE["running"]
        status = "RUNNING" if STATE["running"] else "STOPPED"
        add_log(f"Session {status}.")
        return {"running": STATE["running"], "logs": STATE["logs"][:10]}


@app.post("/api/strategy")
def update_strategy(payload: StrategyUpdate) -> Dict[str, Any]:
    with lock:
        STATE["strategy"] = payload.strategy
        STATE["risk_level"] = payload.risk_level
        add_log(f"Updated strategy -> {payload.strategy} | risk -> {payload.risk_level}")
        return {"strategy": STATE["strategy"], "risk_level": STATE["risk_level"]}
