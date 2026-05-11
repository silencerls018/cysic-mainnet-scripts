#!/usr/bin/env python3
"""
Cysic Custom Prover - Main Orchestrator
=========================================

Fully replaces the official prover binary.
Event-driven: proof done → immediate submit (0s delay)

Flow:
1. WebSocket: receive task → bid (action=6)
2. WebSocket: task update confirms bid won
3. Chain TX: MsgAccept
4. Generate proof (via venus_prover_server / cache)
5. Chain TX: MsgSubmitHash
6. HTTP API: nextStep poll (wait for step=3)
7. HTTP API: submitTaskDataRaw (upload proof)
8. Chain TX: MsgSubmitData

Usage: python3 prover.py [config.yaml]
"""

import asyncio
import json
import hashlib
import base64
import os
import sys
import time
import glob
import logging

import yaml

from signer import CysicSigner
from chain_client import ChainClient
from api_client import APIClient
from ws_client import WSClient

# ==================== Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y/%m/%d %H:%M:%S',
)
logger = logging.getLogger("prover")


class Task:
    """Track task state"""
    NEW = 0
    BID_SENT = 1
    ACCEPTED = 2
    PROVING = 3
    HASH_SUBMITTED = 4
    DATA_UPLOADED = 5
    DONE = 100
    FAILED = -1

    def __init__(self, task_hash, task_type, task_info):
        self.hash = task_hash
        self.type = task_type
        self.info = task_info
        self.state = self.NEW
        self.input_files = []  # [{BlockHeight, S3Url}]
        self.proof_files = []  # local proof file paths
        self.created_at = time.time()


class CustomProver:
    def __init__(self, config_path="config.yaml"):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.signer = CysicSigner(self.config["private_key"])
        self.chain = ChainClient(self.signer, self.config)
        self.api = APIClient(self.signer, self.config)
        self.ws = WSClient(
            self.signer, self.config,
            on_new_task=self._on_new_task,
            on_task_update=self._on_task_update,
        )

        self.tasks = {}  # task_hash → Task
        self.task_queue = asyncio.Queue()
        self.proof_output_dir = self.config.get("proof_output_dir", "/root/venus_v0_1_6/tmp/prover_7000")
        self.cache_dir = self.config.get("cache_dir", "/root/cysic-prover/cache")
        self.task_map_dir = self.config.get("task_map_dir", "/root/cysic-prover/task_map")

        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.task_map_dir, exist_ok=True)

        # Stats
        self.stats = {"received": 0, "bid": 0, "accepted": 0, "submitted": 0, "failed": 0}

    async def start(self):
        logger.info("=" * 60)
        logger.info("  Cysic Custom Prover")
        logger.info(f"  Worker: {self.signer.eth_address}")
        logger.info(f"  Cysic:  {self.signer.cysic_address}")
        logger.info(f"  Bid:    {self.config['bid']}")
        logger.info("=" * 60)

        # Connect WebSocket
        await self.ws.connect()

        # Start task processor
        asyncio.create_task(self._task_processor())

        # Keep running
        try:
            while True:
                await asyncio.sleep(60)
                logger.info(f"STATS: {self.stats}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
        finally:
            await self.ws.disconnect()

    # ==================== WebSocket Callbacks ====================

    async def _on_new_task(self, task_info):
        """New task from server (respType=5, gzip+base64 decoded)"""
        task_hash = task_info.get("task_hash") or task_info.get("taskHash")
        task_type = task_info.get("task_type", "venus")

        if not task_hash or task_hash in self.tasks:
            return

        self.stats["received"] += 1
        logger.info(f"NEW TASK: {task_hash[:16]}... type={task_type} "
                   f"difficulty={task_info.get('task_difficulty')} "
                   f"max_bid={task_info.get('task_max_bid')}")

        task = Task(task_hash, task_type, task_info)
        self.tasks[task_hash] = task

        # Immediately bid
        await self.ws.send_bid(task_hash)
        task.state = Task.BID_SENT
        self.stats["bid"] += 1

    async def _on_task_update(self, tasks_data):
        """Task update from server (respType=3, base64 decoded)"""
        for td in tasks_data:
            task_hash = td.get("task_hash")
            if not task_hash or task_hash not in self.tasks:
                continue

            task = self.tasks[task_hash]

            # Parse input files
            if "task_proof_data_index" in td:
                try:
                    idx = td["task_proof_data_index"]
                    task.input_files = json.loads(idx) if isinstance(idx, str) else idx
                except:
                    pass

            # Write task_map for each block
            for item in task.input_files:
                bh = item.get("BlockHeight")
                if bh:
                    map_file = os.path.join(self.task_map_dir, f"block_{bh}")
                    with open(map_file, 'w') as f:
                        f.write(task_hash)

            # If bid was sent and we have input files, proceed to accept
            if task.state == Task.BID_SENT and task.input_files:
                await self.task_queue.put(task_hash)

    # ==================== Task Processing ====================

    async def _task_processor(self):
        """Process tasks one at a time"""
        while True:
            try:
                task_hash = await self.task_queue.get()
                task = self.tasks.get(task_hash)
                if not task or task.state == Task.FAILED:
                    continue
                await self._process_task(task)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Task processor error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _process_task(self, task: Task):
        """Full task lifecycle"""
        start_time = time.time()

        try:
            # Step 1: Accept task (chain TX)
            logger.info(f"[{task.hash[:16]}] Step 1: Accept task...")
            task.state = Task.ACCEPTED
            ok, result = await asyncio.to_thread(self.chain.accept_task, task.hash)
            if not ok:
                logger.error(f"[{task.hash[:16]}] Accept failed: {result}")
                task.state = Task.FAILED
                self.stats["failed"] += 1
                return
            self.stats["accepted"] += 1

            # Step 2: Wait for proof (from cache or venus_prover_server)
            logger.info(f"[{task.hash[:16]}] Step 2: Get proof files...")
            await self.ws.send_start_work()
            task.state = Task.PROVING

            proof_files = await self._get_proof_files(task)
            if not proof_files:
                logger.error(f"[{task.hash[:16]}] No proof files found")
                task.state = Task.FAILED
                self.stats["failed"] += 1
                return
            task.proof_files = proof_files

            await self.ws.send_finish_work()
            logger.info(f"[{task.hash[:16]}] Proof ready: {len(proof_files)} files")

            # Step 3: Submit hash (chain TX)
            logger.info(f"[{task.hash[:16]}] Step 3: Submit hash...")
            proof_hash = self._compute_proof_hash(proof_files)
            task.state = Task.HASH_SUBMITTED
            ok, result = await asyncio.to_thread(self.chain.submit_hash, task.hash, proof_hash)
            if not ok:
                logger.error(f"[{task.hash[:16]}] SubmitHash failed: {result}")
                task.state = Task.FAILED
                self.stats["failed"] += 1
                return

            # Step 4: Poll nextStep (HTTP API)
            logger.info(f"[{task.hash[:16]}] Step 4: Poll nextStep...")
            ok = await asyncio.to_thread(self.api.poll_next_step, task.hash, 120)
            if not ok:
                logger.error(f"[{task.hash[:16]}] nextStep timeout")
                task.state = Task.FAILED
                self.stats["failed"] += 1
                return

            # Step 5: Upload proof (HTTP API)
            logger.info(f"[{task.hash[:16]}] Step 5: Upload proof...")
            task.state = Task.DATA_UPLOADED
            s3_url = await asyncio.to_thread(self.api.upload_proof, task.hash, proof_files)
            if not s3_url:
                logger.error(f"[{task.hash[:16]}] Upload failed")
                task.state = Task.FAILED
                self.stats["failed"] += 1
                return

            # Step 6: Submit data (chain TX)
            logger.info(f"[{task.hash[:16]}] Step 6: Submit data...")
            ok, result = await asyncio.to_thread(self.chain.submit_data, task.hash, s3_url)
            if not ok:
                logger.error(f"[{task.hash[:16]}] SubmitData failed: {result}")
                task.state = Task.FAILED
                self.stats["failed"] += 1
                return

            # Done!
            task.state = Task.DONE
            self.stats["submitted"] += 1
            total = time.time() - start_time
            logger.info(f"✅✅✅ [{task.hash[:16]}] COMPLETE! total={total:.1f}s")

        except Exception as e:
            logger.error(f"[{task.hash[:16]}] Error: {e}", exc_info=True)
            task.state = Task.FAILED
            self.stats["failed"] += 1

    # ==================== Proof File Management ====================

    CARGO_ZISK = "/root/venus_v0_1_6/target/release/cargo-zisk-real"
    ELF = "/root/venus_v0_1_6/guest/zisk-eth-client/bin/guests/stateless-validator-reth/target/riscv64ima-zisk-zkvm-elf/release/zec-reth"
    PROVING_KEY = "/root/venus_v0_1_6/build/provingKey"

    async def _get_proof_files(self, task: Task) -> list:
        """
        Get proof files for all blocks in the task.
        1. Check cache first
        2. If no cache, download input from S3 + run cargo-zisk-real
        """
        import subprocess
        import aiohttp

        proof_files = []

        for idx, item in enumerate(task.input_files):
            bh = item.get("BlockHeight")
            s3_url = item.get("S3Url")
            if not bh:
                continue

            # Check cache first
            cache_pattern = os.path.join(self.cache_dir, f"proof_{bh}_*/vadcop_final_proof.bin")
            cached = glob.glob(cache_pattern)
            if cached:
                proof_files.append(cached[0])
                logger.info(f"  Block {bh}: cache hit!")
                continue

            # Check if already computed in proof_output_dir
            existing = glob.glob(os.path.join(self.proof_output_dir, f"{bh}_*/vadcop_final_proof.bin"))
            if existing:
                proof_files.append(existing[0])
                logger.info(f"  Block {bh}: already computed!")
                continue

            # Need to compute: download input + run cargo-zisk-real
            logger.info(f"  Block {bh}: downloading input...")

            # Download input
            input_dir = os.path.join(self.cache_dir, f"prove_{bh}_{idx}")
            os.makedirs(input_dir, exist_ok=True)
            input_file = os.path.join(input_dir, "input.bin")

            if not os.path.exists(input_file):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(s3_url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                # Check if gzipped
                                if data[:2] == b'\x1f\x8b':
                                    import gzip as gz
                                    data = gz.decompress(data)
                                with open(input_file, 'wb') as f:
                                    f.write(data)
                                logger.info(f"  Block {bh}: downloaded {len(data)} bytes")
                            else:
                                logger.error(f"  Block {bh}: download failed HTTP {resp.status}")
                                return []
                except Exception as e:
                    logger.error(f"  Block {bh}: download error: {e}")
                    return []

            # Run cargo-zisk-real
            logger.info(f"  Block {bh}: running cargo-zisk-real...")
            proof_dir = os.path.join(self.cache_dir, f"proof_{bh}_{idx}")
            os.makedirs(proof_dir, exist_ok=True)

            start = time.time()
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    [self.CARGO_ZISK, "prove",
                     "-k", self.PROVING_KEY,
                     "-e", self.ELF,
                     "-i", input_file,
                     "-o", proof_dir,
                     "-a", "-y", "-u"],
                    timeout=600,
                    capture_output=True,
                    env={**os.environ, "ASM_UNLOCK": "true",
                         "VENUS_DIR": "/root/venus_v0_1_6",
                         "VENUS_OUT_DIR": "/root/venus_v0_1_6/tmp",
                         "RUST_LOG": "info"}
                )
                duration = time.time() - start

                proof_file = os.path.join(proof_dir, "vadcop_final_proof.bin")
                if result.returncode == 0 and os.path.exists(proof_file):
                    proof_files.append(proof_file)
                    logger.info(f"  Block {bh}: proof generated in {duration:.1f}s")
                else:
                    logger.error(f"  Block {bh}: cargo-zisk failed (code={result.returncode})")
                    if result.stderr:
                        logger.error(f"  stderr: {result.stderr.decode()[:500]}")
                    return []
            except subprocess.TimeoutExpired:
                logger.error(f"  Block {bh}: cargo-zisk timeout (600s)")
                return []
            except Exception as e:
                logger.error(f"  Block {bh}: cargo-zisk error: {e}")
                return []

        return proof_files

    def _compute_proof_hash(self, proof_files: list) -> str:
        """SHA256 of all proof files concatenated, base64 encoded"""
        h = hashlib.sha256()
        for pf in proof_files:
            with open(pf, 'rb') as f:
                h.update(f.read())
        return base64.b64encode(h.digest()).decode()


# ==================== Entry Point ====================

async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        print("Usage: python3 prover.py [config.yaml]")
        sys.exit(1)

    prover = CustomProver(config_path)
    await prover.start()


if __name__ == "__main__":
    asyncio.run(main())
