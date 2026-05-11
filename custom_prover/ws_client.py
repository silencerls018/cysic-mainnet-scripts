"""
Cysic Prover - WebSocket Client

Handles:
- Login (action=1) - REQUIRED for nextStep API auth
- Heartbeat (action=2) every 14s
- Start work notification (action=4)
- Finish work notification (action=5)
- Bid submission (action=6)
- Receive new tasks (respType=5, gzip+base64)
- Receive task updates (respType=3, base64)
"""

import asyncio
import json
import gzip
import base64
import time
import uuid
import logging
from typing import Callable, Optional

import websockets

from signer import CysicSigner

logger = logging.getLogger(__name__)

# Server → Client
RESP_HEARTBEAT_ACK = 0
RESP_HEARTBEAT_CONFIG = 1
RESP_TASK_UPDATE = 3
RESP_NEW_TASK = 5

# Client → Server
ACTION_LOGIN = 1
ACTION_HEARTBEAT = 2
ACTION_START_WORK = 4
ACTION_FINISH_WORK = 5
ACTION_BID = 6


class WSClient:
    def __init__(self, signer: CysicSigner, config: dict,
                 on_new_task=None, on_task_update=None):
        self.signer = signer
        self.config = config
        self.ws_url = config["server"]["ws_endpoint"]
        self.bid_price = config["bid"]
        self.claim_reward_address = config.get("claim_reward_address", "")
        self.on_new_task = on_new_task
        self.on_task_update = on_task_update
        self.ws = None
        self._running = False
        self.heartbeat_interval = 14

    async def connect(self):
        """Connect to WebSocket server"""
        logger.info(f"Connecting to {self.ws_url}")
        self.ws = await websockets.connect(
            self.ws_url,
            ping_interval=30,
            ping_timeout=10,
            max_size=10 * 1024 * 1024,
        )
        self._running = True
        logger.info("WebSocket connected")

        # Step 1: Send LOGIN (action=1) - REQUIRED for nextStep API auth
        await self._send_login()

        # Step 2: Send start work (action=4)
        await self.send_start_work()

        # Start background tasks
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._receive_loop())

    async def disconnect(self):
        self._running = False
        if self.ws:
            await self.ws.close()

    async def send_bid(self, task_hash: str):
        """Send bid for a task (action=6)"""
        data = self.signer.make_bid(task_hash, self.bid_price)
        await self._send({"action": ACTION_BID, "data": data})
        logger.info(f"BID sent: {task_hash[:16]}... price={self.bid_price}")

    async def send_start_work(self):
        """Notify start work (action=4)"""
        data = self.signer.make_work_notify()
        await self._send({"action": ACTION_START_WORK, "data": data})

    async def send_finish_work(self):
        """Notify finish work (action=5)"""
        data = self.signer.make_work_notify()
        await self._send({"action": ACTION_FINISH_WORK, "data": data})

    async def _send_login(self):
        """action=1: Login/register - REQUIRED for nextStep API to work"""
        data = {
            "clientType": 1,
            "clientVersion": "1.1.0",
            "workerAddress": self.signer.eth_address,
            "claimRewardAddress": self.claim_reward_address,
            "supportTaskType": ["venus"],
            "timestamp": int(time.time()),
            "nonce": str(uuid.uuid4()),
        }
        data["sign"] = self.signer.sign_api(data)
        await self._send({"action": ACTION_LOGIN, "data": data})
        logger.info("LOGIN sent (action=1)")

    async def _send_heartbeat(self):
        data = self.signer.make_heartbeat()
        await self._send({"action": ACTION_HEARTBEAT, "data": data})

    async def _send(self, msg: dict):
        if self.ws:
            await self.ws.send(json.dumps(msg))
        else:
            logger.error("WebSocket not connected")

    async def _heartbeat_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if self._running:
                    await self._send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(5)

    async def _receive_loop(self):
        while self._running:
            try:
                msg = await self.ws.recv()
                await self._handle(msg)
            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed:
                logger.warning("WebSocket closed, reconnecting...")
                await self._reconnect()
            except Exception as e:
                logger.error(f"Receive error: {e}")
                await asyncio.sleep(1)

    async def _reconnect(self):
        for i in range(10):
            try:
                await asyncio.sleep(min(5 * (i + 1), 30))
                self.ws = await websockets.connect(
                    self.ws_url, ping_interval=30, max_size=10*1024*1024)
                await self._send_heartbeat()
                logger.info("Reconnected")
                return
            except Exception as e:
                logger.error(f"Reconnect {i+1} failed: {e}")
        self._running = False

    async def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
        except:
            return

        resp_type = msg.get("respType")
        data = msg.get("data")

        if resp_type == RESP_HEARTBEAT_ACK:
            pass  # Normal ACK

        elif resp_type == RESP_HEARTBEAT_CONFIG:
            # Server sends heartbeat config: {"heartbeatDuration":15}
            if data:
                try:
                    config_data = json.loads(base64.b64decode(data).decode())
                    interval = config_data.get("heartbeatDuration", 14)
                    self.heartbeat_interval = interval
                except:
                    pass

        elif resp_type == RESP_TASK_UPDATE:
            if data and self.on_task_update:
                try:
                    decoded = json.loads(base64.b64decode(data).decode())
                    await self.on_task_update(decoded)
                except Exception as e:
                    logger.error(f"Task update decode error: {e}")

        elif resp_type == RESP_NEW_TASK:
            if data and self.on_new_task:
                try:
                    decompressed = gzip.decompress(base64.b64decode(data))
                    task_info = json.loads(decompressed.decode())
                    await self.on_new_task(task_info)
                except Exception as e:
                    logger.error(f"New task decode error: {e}")
