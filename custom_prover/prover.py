#!/usr/bin/env python3
"""
Custom Cysic Prover Client
==========================

Event-driven prover that submits proofs IMMEDIATELY after generation,
without waiting for heartbeat cycles.

Key advantages over official prover:
- Proof done → 0s → submit (vs 15-30s heartbeat wait)
- No "proofData empty" false triggers
- Clean event-driven architecture
- Configurable delays (all default to 0)

Usage:
    python3 prover.py [--config config.yaml]
"""

import asyncio
import json
import logging
import os
import sys
import time
import hashlib
import base64
import gzip
from pathlib import Path
from typing import Optional

import yaml

from signer import CysicSigner
from ws_client import CysicWebSocket
from api_client import CysicAPIClient
from venus_client import VenusProverClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y/%m/%d %H:%M:%S',
)
logger = logging.getLogger("prover")


class TaskState:
    """Track the state of a task through its lifecycle."""
    
    STEP_NEW = 0
    STEP_BID = 1
    STEP_ACCEPTED = 2
    STEP_PROVING = 3
    STEP_SUBMIT_HASH = 4
    STEP_SUBMIT_DATA = 5
    STEP_DONE = 100
    STEP_FAILED = -1

    def __init__(self, task_hash: str, task_type: str, task_info: dict):
        self.task_hash = task_hash
        self.task_type = task_type
        self.task_info = task_info
        self.step = self.STEP_NEW
        self.proof_hash: Optional[str] = None
        self.proof_data: Optional[str] = None
        self.proof_data_url: Optional[str] = None
        self.created_at = time.time()
        self.proof_done_at: Optional[float] = None
        self.submitted_at: Optional[float] = None

    @property
    def elapsed(self) -> float:
        return time.time() - self.created_at

    def __repr__(self):
        return f"Task({self.task_hash[:16]}... step={self.step} elapsed={self.elapsed:.1f}s)"


class CustomProver:
    """
    Main prover orchestrator.
    
    Lifecycle of a task:
    1. Receive new task via WebSocket (respType=5)
    2. Immediately submit bid
    3. When bid accepted (via task update), accept task
    4. Download input, call venus_prover_server for proof
    5. Immediately submit proof hash via HTTP
    6. Immediately upload proof data via HTTP
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        
        # Components
        self.signer = CysicSigner(
            private_key_hex=self.config["private_key"],
            worker_address=self.config["worker_address"],
        )
        self.ws_client = CysicWebSocket(
            ws_url=self.config["server"]["ws_endpoint"],
            signer=self.signer,
            heartbeat_interval=self.config["timing"]["heartbeat_interval"],
            on_new_task=self._on_new_task,
            on_task_update=self._on_task_update,
        )
        self.api_client = CysicAPIClient(
            base_url=self.config["server"]["api_endpoint"],
        )
        self.venus_client = VenusProverClient(
            grpc_endpoint=self.config["server"]["venus_grpc_endpoint"],
            cache_dir=self.config.get("cache_dir", "./cache"),
        )

        # Task tracking
        self.active_tasks: dict[str, TaskState] = {}
        self.current_proving_task: Optional[str] = None
        self.task_queue: asyncio.Queue = asyncio.Queue()

        # Stats
        self.stats = {
            "tasks_received": 0,
            "tasks_bid": 0,
            "tasks_accepted": 0,
            "tasks_proved": 0,
            "tasks_submitted": 0,
            "tasks_failed": 0,
        }

    def _load_config(self, path: str) -> dict:
        """Load configuration from YAML file."""
        with open(path, 'r') as f:
            config = yaml.safe_load(f)
        
        # Validate required fields
        required = ["private_key", "worker_address", "bid", "server"]
        for field in required:
            if field not in config:
                raise ValueError(f"Missing required config field: {field}")
        
        # Set defaults
        config.setdefault("timing", {})
        config["timing"].setdefault("heartbeat_interval", 14)
        config["timing"].setdefault("bid_delay", 0)
        config["timing"].setdefault("accept_delay", 0)
        config["timing"].setdefault("submit_hash_delay", 0)
        config["timing"].setdefault("submit_data_delay", 0)
        config.setdefault("cache_dir", "./cache")
        config.setdefault("log_level", "INFO")

        return config

    async def start(self):
        """Start the prover client."""
        logger.info("=" * 60)
        logger.info("  Custom Cysic Prover Client")
        logger.info(f"  Worker: {self.config['worker_address']}")
        logger.info(f"  Bid: {self.config['bid']}")
        logger.info(f"  Delays: bid={self.config['timing']['bid_delay']}s "
                    f"accept={self.config['timing']['accept_delay']}s "
                    f"hash={self.config['timing']['submit_hash_delay']}s "
                    f"data={self.config['timing']['submit_data_delay']}s")
        logger.info("=" * 60)

        # Initialize
        await self.api_client.start()
        await self.ws_client.connect()

        # Start task processor
        processor = asyncio.create_task(self._task_processor())

        try:
            # Keep running
            while True:
                await asyncio.sleep(60)
                self._log_stats()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
        finally:
            await self.ws_client.disconnect()
            await self.api_client.close()
            processor.cancel()

    async def _on_new_task(self, task_info: dict):
        """
        Handle new task notification from WebSocket (respType=5).
        
        Task info is a gzip+base64 decoded dict containing:
        - task_hash
        - task_type (e.g. "venus")
        - task_difficulty
        - task_ddl (deadline timestamp)
        - task_max_bid
        - task_bid_deadline
        """
        # task_info might be a dict or extracted from the decoded data
        # From log: "new task need bid, task hash: xxx, task type: venus, ..."
        
        task_hash = task_info.get("task_hash") or task_info.get("taskHash")
        task_type = task_info.get("task_type") or task_info.get("taskType", "venus")

        if not task_hash:
            logger.warning(f"New task without hash: {task_info}")
            return

        if task_hash in self.active_tasks:
            logger.debug(f"Task {task_hash[:16]}... already tracked")
            return

        self.stats["tasks_received"] += 1
        logger.info(f"NEW TASK: {task_hash[:16]}... type={task_type} "
                    f"difficulty={task_info.get('task_difficulty')} "
                    f"max_bid={task_info.get('task_max_bid')}")

        # Create task state
        task = TaskState(task_hash, task_type, task_info)
        self.active_tasks[task_hash] = task

        # Immediately bid (with configurable delay)
        bid_delay = self.config["timing"]["bid_delay"]
        if bid_delay > 0:
            await asyncio.sleep(bid_delay)

        await self._submit_bid(task)

    async def _on_task_update(self, tasks: list):
        """
        Handle task update from WebSocket (respType=3).
        
        This is the base64 decoded task list containing:
        - task_hash
        - task_proof_data_index (JSON string with BlockHeight + S3Url)
        - task_type
        - wait_proof_data_url
        """
        for task_data in tasks:
            task_hash = task_data.get("task_hash")
            if not task_hash:
                continue

            # Store task input info for later use
            if task_hash in self.active_tasks:
                task = self.active_tasks[task_hash]
                # Update with S3 URLs and proof data URL
                if "task_proof_data_index" in task_data:
                    try:
                        index_str = task_data["task_proof_data_index"]
                        if isinstance(index_str, str):
                            task.task_info["input_files"] = json.loads(index_str)
                        else:
                            task.task_info["input_files"] = index_str
                    except json.JSONDecodeError:
                        pass
                if "wait_proof_data_url" in task_data:
                    task.task_info["wait_proof_data_url"] = task_data["wait_proof_data_url"]

                # If task is waiting, queue it for processing
                if task.step == TaskState.STEP_BID:
                    # Bid was accepted, proceed to accept
                    await self._accept_task(task)

    async def _submit_bid(self, task: TaskState):
        """Submit a bid for the task."""
        try:
            bid_price = self.config["bid"]
            await self.ws_client.send_bid(task.task_hash, bid_price)
            task.step = TaskState.STEP_BID
            self.stats["tasks_bid"] += 1
            logger.info(f"BID SENT: {task.task_hash[:16]}... price={bid_price}")
        except Exception as e:
            logger.error(f"Failed to bid on {task.task_hash[:16]}...: {e}")
            task.step = TaskState.STEP_FAILED
            self.stats["tasks_failed"] += 1

    async def _accept_task(self, task: TaskState):
        """Accept a task after bid is confirmed."""
        if task.step >= TaskState.STEP_ACCEPTED:
            return

        accept_delay = self.config["timing"]["accept_delay"]
        if accept_delay > 0:
            await asyncio.sleep(accept_delay)

        try:
            # Send accept via WebSocket or HTTP
            data = self.signer.make_accept_data(task.task_hash)
            # TODO: Confirm which endpoint/action accepts a task
            # For now, use nextStep API
            resp = await self.api_client.next_step(data)
            
            if resp.get("code") == 0:
                task.step = TaskState.STEP_ACCEPTED
                self.stats["tasks_accepted"] += 1
                logger.info(f"ACCEPTED: {task.task_hash[:16]}...")
                
                # Queue for proof generation
                await self.task_queue.put(task.task_hash)
            else:
                logger.warning(f"Accept failed for {task.task_hash[:16]}...: {resp}")
        except Exception as e:
            logger.error(f"Failed to accept {task.task_hash[:16]}...: {e}")

    async def _task_processor(self):
        """
        Background task processor.
        Processes one task at a time (GPU is single-threaded).
        """
        while True:
            try:
                task_hash = await self.task_queue.get()
                task = self.active_tasks.get(task_hash)
                
                if not task or task.step == TaskState.STEP_FAILED:
                    continue

                await self._process_task(task)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Task processor error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _process_task(self, task: TaskState):
        """
        Full task processing: generate proof → submit immediately.
        """
        logger.info(f"PROCESSING: {task.task_hash[:16]}...")
        task.step = TaskState.STEP_PROVING
        self.current_proving_task = task.task_hash

        try:
            # Notify server we're starting
            await self.ws_client.send_start_work(task.task_hash)

            # Get input files from task info
            input_files = task.task_info.get("input_files", [])
            if not input_files:
                logger.error(f"No input files for task {task.task_hash[:16]}...")
                task.step = TaskState.STEP_FAILED
                self.stats["tasks_failed"] += 1
                return

            # Generate proof
            logger.info(f"PROVING: {task.task_hash[:16]}... ({len(input_files)} inputs)")
            start_time = time.time()
            
            proof_result = await self.venus_client.generate_proof(
                task.task_hash, input_files
            )
            
            prove_time = time.time() - start_time
            task.proof_done_at = time.time()
            task.proof_hash = proof_result["proof_hash"]
            task.proof_data = proof_result["proof_data"]
            
            logger.info(f"PROOF DONE: {task.task_hash[:16]}... "
                       f"time={prove_time:.1f}s hash={task.proof_hash[:16]}...")

            # Notify server we're done
            await self.ws_client.send_finish_work(task.task_hash)
            self.stats["tasks_proved"] += 1

            # IMMEDIATELY submit proof hash (no heartbeat wait!)
            await self._submit_proof_hash(task)

            # IMMEDIATELY submit proof data (no heartbeat wait!)
            await self._submit_proof_data(task)

        except Exception as e:
            logger.error(f"Task processing failed for {task.task_hash[:16]}...: {e}",
                        exc_info=True)
            task.step = TaskState.STEP_FAILED
            self.stats["tasks_failed"] += 1
        finally:
            self.current_proving_task = None

    async def _submit_proof_hash(self, task: TaskState):
        """Submit proof hash immediately after proof generation."""
        submit_hash_delay = self.config["timing"]["submit_hash_delay"]
        if submit_hash_delay > 0:
            await asyncio.sleep(submit_hash_delay)

        task.step = TaskState.STEP_SUBMIT_HASH
        logger.info(f"SUBMIT HASH: {task.task_hash[:16]}... "
                   f"(delay since proof: {time.time() - task.proof_done_at:.1f}s)")

        try:
            data = self.signer.make_submit_hash_data(task.task_hash, task.proof_hash)
            resp = await self.api_client.submit_proof_hash(data)
            
            if resp.get("code") == 0:
                logger.info(f"HASH SUBMITTED: {task.task_hash[:16]}...")
            else:
                logger.warning(f"Hash submit response: {resp}")
        except Exception as e:
            logger.error(f"Failed to submit hash for {task.task_hash[:16]}...: {e}")

    async def _submit_proof_data(self, task: TaskState):
        """Submit proof data immediately after hash submission."""
        submit_data_delay = self.config["timing"]["submit_data_delay"]
        if submit_data_delay > 0:
            await asyncio.sleep(submit_data_delay)

        task.step = TaskState.STEP_SUBMIT_DATA
        logger.info(f"SUBMIT DATA: {task.task_hash[:16]}... "
                   f"proof_size={len(task.proof_data)} bytes (b64)")

        try:
            data = self.signer.make_submit_data_raw(task.task_hash, task.proof_data)
            resp = await self.api_client.submit_task_data_raw(data)
            
            if resp.get("code") == 0:
                task.proof_data_url = resp.get("data", {}).get("url", "")
                task.submitted_at = time.time()
                task.step = TaskState.STEP_DONE
                self.stats["tasks_submitted"] += 1
                
                total_time = task.submitted_at - task.created_at
                submit_delay = task.submitted_at - task.proof_done_at
                
                logger.info(f"TASK COMPLETE: {task.task_hash[:16]}... "
                           f"total={total_time:.1f}s "
                           f"proof_to_submit={submit_delay:.1f}s "
                           f"url={task.proof_data_url}")
            else:
                logger.warning(f"Data submit response: {resp}")
                task.step = TaskState.STEP_FAILED
                self.stats["tasks_failed"] += 1

        except Exception as e:
            logger.error(f"Failed to submit data for {task.task_hash[:16]}...: {e}")
            task.step = TaskState.STEP_FAILED
            self.stats["tasks_failed"] += 1

    def _log_stats(self):
        """Log periodic stats."""
        active = len([t for t in self.active_tasks.values()
                     if t.step not in (TaskState.STEP_DONE, TaskState.STEP_FAILED)])
        logger.info(f"STATS: received={self.stats['tasks_received']} "
                   f"bid={self.stats['tasks_bid']} "
                   f"accepted={self.stats['tasks_accepted']} "
                   f"proved={self.stats['tasks_proved']} "
                   f"submitted={self.stats['tasks_submitted']} "
                   f"failed={self.stats['tasks_failed']} "
                   f"active={active}")


async def main():
    """Entry point."""
    config_path = "config.yaml"
    if len(sys.argv) > 1 and sys.argv[1] == "--config":
        config_path = sys.argv[2]
    elif len(sys.argv) > 1:
        config_path = sys.argv[1]

    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}")
        print("Usage: python3 prover.py [--config config.yaml]")
        sys.exit(1)

    prover = CustomProver(config_path)
    await prover.start()


if __name__ == "__main__":
    asyncio.run(main())
