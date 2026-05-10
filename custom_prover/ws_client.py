"""
Cysic Prover WebSocket Client
Handles WebSocket connection for heartbeat and task reception.
"""

import asyncio
import json
import gzip
import base64
import logging
import time
from typing import Callable, Optional

import websockets

from signer import CysicSigner

logger = logging.getLogger(__name__)


# Response types from server
RESP_TYPE_HEARTBEAT_ACK = 0   # Heartbeat acknowledgement
RESP_TYPE_TASK_UPDATE = 3      # Task progress update (base64 JSON)
RESP_TYPE_NEW_TASK = 5         # New task available for bid (gzip + base64)

# WebSocket actions (client -> server)
ACTION_HEARTBEAT = 2           # Heartbeat / registration
ACTION_BID = 3                 # Submit bid (estimated)
ACTION_START_WORK = 4          # Notify start work (estimated)
ACTION_FINISH_WORK = 5         # Notify finish work (estimated)


class CysicWebSocket:
    """WebSocket client for Cysic prover communication."""

    def __init__(self, ws_url: str, signer: CysicSigner,
                 heartbeat_interval: int = 14,
                 on_new_task: Optional[Callable] = None,
                 on_task_update: Optional[Callable] = None):
        """
        Args:
            ws_url: WebSocket server URL
            signer: CysicSigner instance for signing messages
            heartbeat_interval: Seconds between heartbeats (default 14)
            on_new_task: Callback for new task notifications
            on_task_update: Callback for task status updates
        """
        self.ws_url = ws_url
        self.signer = signer
        self.heartbeat_interval = heartbeat_interval
        self.on_new_task = on_new_task
        self.on_task_update = on_task_update
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Establish WebSocket connection."""
        logger.info(f"Connecting to {self.ws_url}")
        self.ws = await websockets.connect(
            self.ws_url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
            max_size=10 * 1024 * 1024,  # 10MB max message size
        )
        self._running = True
        logger.info("WebSocket connected")

        # Send initial heartbeat immediately
        await self._send_heartbeat()

        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self):
        """Close WebSocket connection."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._receive_task:
            self._receive_task.cancel()
        if self.ws:
            await self.ws.close()
            logger.info("WebSocket disconnected")

    async def send_bid(self, task_hash: str, bid_price: str):
        """
        Send bid for a task via WebSocket.
        
        Args:
            task_hash: Task to bid on
            bid_price: Bid price string
        """
        data = self.signer.make_bid_data(task_hash, bid_price)
        msg = json.dumps({"action": ACTION_BID, "data": data})
        await self._send(msg)
        logger.info(f"Sent bid for task {task_hash[:16]}... price={bid_price}")

    async def send_start_work(self, task_hash: str):
        """Notify server that proof generation has started."""
        data = self.signer.make_start_work_data(task_hash)
        msg = json.dumps({"action": ACTION_START_WORK, "data": data})
        await self._send(msg)
        logger.info(f"Sent start_work for task {task_hash[:16]}...")

    async def send_finish_work(self, task_hash: str):
        """Notify server that proof generation is complete."""
        data = self.signer.make_finish_work_data(task_hash)
        msg = json.dumps({"action": ACTION_FINISH_WORK, "data": data})
        await self._send(msg)
        logger.info(f"Sent finish_work for task {task_hash[:16]}...")

    async def _send_heartbeat(self):
        """Send heartbeat message."""
        data = self.signer.make_heartbeat_data()
        msg = json.dumps({"action": ACTION_HEARTBEAT, "data": data})
        await self._send(msg)
        logger.debug("Sent heartbeat")

    async def _send(self, msg: str):
        """Send a message over WebSocket."""
        if self.ws and self.ws.open:
            await self.ws.send(msg)
        else:
            logger.error("WebSocket not connected, cannot send message")

    async def _heartbeat_loop(self):
        """Background heartbeat sender."""
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
        """Background message receiver."""
        while self._running:
            try:
                msg = await self.ws.recv()
                await self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}")
                if self._running:
                    await self._reconnect()
            except Exception as e:
                logger.error(f"Receive error: {e}")
                await asyncio.sleep(1)

    async def _reconnect(self):
        """Attempt to reconnect."""
        for attempt in range(10):
            try:
                logger.info(f"Reconnecting (attempt {attempt + 1})...")
                await asyncio.sleep(min(5 * (attempt + 1), 30))
                self.ws = await websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=10 * 1024 * 1024,
                )
                await self._send_heartbeat()
                logger.info("Reconnected successfully")
                return
            except Exception as e:
                logger.error(f"Reconnect attempt {attempt + 1} failed: {e}")

        logger.critical("Failed to reconnect after 10 attempts")
        self._running = False

    async def _handle_message(self, raw_msg: str):
        """Parse and route incoming messages."""
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON received: {raw_msg[:100]}")
            return

        resp_type = msg.get("respType")
        code = msg.get("code", -1)
        message = msg.get("message", "")
        data = msg.get("data")

        if code != 0:
            logger.warning(f"Server error: code={code}, msg={message}")
            return

        if resp_type == RESP_TYPE_HEARTBEAT_ACK:
            logger.debug(f"Heartbeat ACK: {message}")

        elif resp_type == RESP_TYPE_TASK_UPDATE:
            # Task update: base64 encoded JSON array
            if data:
                try:
                    decoded = base64.b64decode(data).decode('utf-8')
                    tasks = json.loads(decoded)
                    if self.on_task_update:
                        await self.on_task_update(tasks)
                except Exception as e:
                    logger.error(f"Failed to decode task update: {e}")

        elif resp_type == RESP_TYPE_NEW_TASK:
            # New task: gzip + base64 encoded
            if data:
                try:
                    compressed = base64.b64decode(data)
                    decompressed = gzip.decompress(compressed)
                    task_info = json.loads(decompressed.decode('utf-8'))
                    if self.on_new_task:
                        await self.on_new_task(task_info)
                except Exception as e:
                    logger.error(f"Failed to decode new task: {e}")

        else:
            logger.debug(f"Unknown respType={resp_type}: {raw_msg[:200]}")
