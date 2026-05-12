"""
Cysic Prover - HTTP API Client

Handles:
- nextStep: Poll task state until ready for upload
- submitTaskDataRaw: Upload proof files to get S3 URL
"""

import json
import base64
import gzip
import io
import time
import logging
import requests

from signer import CysicSigner

logger = logging.getLogger(__name__)


class APIClient:
    def __init__(self, signer: CysicSigner, config: dict):
        self.signer = signer
        self.base_url = config["server"]["api_endpoint"].rstrip('/')
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Go-http-client/2.0",
            "Accept-Encoding": "gzip",
        }

    def poll_next_step(self, task_hash: str, timeout: int = 120) -> bool:
        """
        Poll nextStep API until nextStep >= 3.
        Returns True when ready for upload.
        """
        payload = self.signer.make_next_step(task_hash)
        url = f"{self.base_url}/api/v1/common/task/prover/nextStep"
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                r = requests.post(url, json=payload, headers=self.headers, timeout=20)
                resp = r.json()

                if resp.get("code") == 0:
                    next_step = resp.get("nextStep", 0)
                    if next_step >= 3:
                        logger.info(f"nextStep={next_step}, ready for upload!")
                        return True
                    logger.debug(f"nextStep={next_step}, waiting...")
                else:
                    logger.warning(f"nextStep error: {resp.get('msg', '')}")
            except Exception as e:
                logger.warning(f"nextStep request failed: {e}")

            time.sleep(2)

        logger.error(f"nextStep poll timeout ({timeout}s)")
        return False

    def upload_proof(self, task_hash: str, proof_files: list) -> str:
        """
        Upload proof files via submitTaskDataRaw.
        Returns S3 URL on success, None on failure.

        Encoding (must use Go gzip for byte-compatibility):
        - Each file → Go gzip (level -1) → base64 (done by gzip_tool)
        - Join with ", " (comma + space) for multiple files
        - Wrap in [] → final base64
        """
        import subprocess

        GZIP_TOOL = "/root/custom_prover/gzip_tool"

        inner_b64_list = []
        for filepath in proof_files:
            logger.info(f"  Reading: {filepath}")

            # Use Go gzip tool for byte-exact compatibility with official prover
            try:
                result = subprocess.run(
                    [GZIP_TOOL, filepath],
                    capture_output=True, timeout=30
                )
                if result.returncode != 0:
                    logger.error(f"  gzip_tool failed: {result.stderr.decode()}")
                    return None
                inner_b64 = result.stdout.decode().strip()
                inner_b64_list.append(inner_b64)
                logger.info(f"  Gzip+Base64: {len(inner_b64)} chars")
            except Exception as e:
                logger.error(f"  gzip_tool error: {e}")
                return None

        # Join with ", " (comma + space) for multiple files, wrap in []
        wrapped = f"[{', '.join(inner_b64_list)}]"
        proof_data = base64.b64encode(wrapped.encode()).decode()
        logger.info(f"  proofData length: {len(proof_data)} chars")

        # Build request
        payload = self.signer.make_submit_data_raw(task_hash, proof_data)

        url = f"{self.base_url}/api/v1/common/task/prover/submitTaskDataRaw"
        logger.info(f"  POST → {url}")

        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=60)
            resp = r.json()

            if r.status_code == 200 and resp.get('code') == 0:
                s3_url = resp.get('data', {}).get('url', '')
                if s3_url:
                    logger.info(f"  Upload success! URL: {s3_url[:80]}...")
                    return s3_url

            logger.error(f"  Upload failed: {json.dumps(resp, ensure_ascii=False)[:200]}")
            return None
        except Exception as e:
            logger.error(f"  Upload exception: {e}")
            return None
