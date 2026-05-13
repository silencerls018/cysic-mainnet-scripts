"""
Cysic Prover - HTTP API Client

- get_next_step: Single call to check task state
- poll_next_step: Poll until nextStep >= target
- upload_proof: Upload proof files (uses Go gzip tool)
"""

import json
import base64
import time
import logging
import subprocess
import requests

from signer import CysicSigner

logger = logging.getLogger(__name__)

GZIP_TOOL = "/root/custom_prover/gzip_tool"


class APIClient:
    def __init__(self, signer: CysicSigner, config: dict):
        self.signer = signer
        self.base_url = config["server"]["api_endpoint"].rstrip('/')
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Go-http-client/2.0",
            "Accept-Encoding": "gzip",
        }

    def get_next_step(self, task_hash: str) -> dict:
        """Single nextStep call. Returns full response dict."""
        payload = self.signer.make_next_step(task_hash)
        url = f"{self.base_url}/api/v1/common/task/prover/nextStep"
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=20)
            return r.json()
        except Exception as e:
            logger.warning(f"nextStep request failed: {e}")
            return {"code": -1, "nextStep": -1}

    def poll_next_step(self, task_hash: str, timeout: int = 120) -> bool:
        """Poll nextStep until >= 3."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.get_next_step(task_hash)
            if resp.get("code") == 0:
                step = resp.get("nextStep", 0)
                if step >= 3:
                    logger.info(f"nextStep={step}, ready!")
                    return True
                logger.info(f"nextStep={step}, waiting...")
            else:
                logger.warning(f"nextStep error: {resp.get('msg', '')}")
            time.sleep(2)
        logger.error(f"nextStep poll timeout ({timeout}s)")
        return False

    def upload_proof(self, task_hash: str, proof_files: list) -> str:
        """
        Upload proof files via submitTaskDataRaw.
        Uses Go gzip tool for byte-exact compatibility.
        Multiple files joined with ", " (comma + space).
        """
        inner_b64_list = []
        for filepath in proof_files:
            logger.info(f"  Reading: {filepath}")
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

        # Join with ", " (comma + space), wrap in [], final base64
        wrapped = f"[{', '.join(inner_b64_list)}]"
        proof_data = base64.b64encode(wrapped.encode()).decode()
        logger.info(f"  proofData length: {len(proof_data)} chars")

        payload = self.signer.make_submit_data_raw(task_hash, proof_data)

        url = f"{self.base_url}/api/v1/common/task/prover/submitTaskDataRaw"
        logger.info(f"  POST -> {url}")

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
