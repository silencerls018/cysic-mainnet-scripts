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

        Encoding: file → gzip(level=6, mtime=0) → base64 → "[x,y]" → base64
        """
        # Build proofData
        inner_b64_list = []
        for filepath in proof_files:
            with open(filepath, 'rb') as f:
                raw = f.read()
            logger.info(f"  Reading: {filepath} ({len(raw)} bytes)")

            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6, mtime=0) as gz:
                gz.write(raw)
            compressed = buf.getvalue()
            logger.info(f"  Gzip: {len(compressed)} bytes")
            inner_b64_list.append(base64.b64encode(compressed).decode())

        wrapped = f"[{','.join(inner_b64_list)}]"
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
