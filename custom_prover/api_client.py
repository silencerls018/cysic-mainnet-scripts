"""
Cysic Prover HTTP API Client
Handles all HTTP requests to api.prover.xyz
"""

import aiohttp
import logging
import json
from typing import Optional

logger = logging.getLogger(__name__)

API_BASE = "https://api.prover.xyz"


class CysicAPIClient:
    """HTTP API client for Cysic prover server."""

    def __init__(self, base_url: str = API_BASE):
        self.base_url = base_url.rstrip('/')
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """Initialize HTTP session."""
        self.session = aiohttp.ClientSession(
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Go-http-client/2.0",
                "Accept-Encoding": "gzip",
            }
        )

    async def close(self):
        """Close HTTP session."""
        if self.session:
            await self.session.close()

    async def next_step(self, data: dict) -> dict:
        """
        POST /api/v1/common/task/prover/nextStep
        
        Update task status / get next step instruction.
        
        Args:
            data: Signed request data containing taskHash, workerAddress, step, etc.
            
        Returns:
            Server response dict
        """
        url = f"{self.base_url}/api/v1/common/task/prover/nextStep"
        return await self._post(url, data)

    async def submit_task_data_raw(self, data: dict) -> dict:
        """
        POST /api/v1/common/task/prover/submitTaskDataRaw
        
        Upload proof data to server.
        
        Args:
            data: Dict with taskHash, prover, proofData (base64), sign
            
        Returns:
            Server response with upload URL
            Example: {"code":0,"msg":"","data":{"url":"https://public.prover.xyz/zkTask/proofResult/..."}}
        """
        url = f"{self.base_url}/api/v1/common/task/prover/submitTaskDataRaw"
        return await self._post(url, data)

    async def submit_proof_hash(self, data: dict) -> dict:
        """
        Submit proof hash to server.
        
        This may use nextStep endpoint or a dedicated endpoint.
        Based on log: "submit taskHash, task: xxx, tx: xxx"
        The original prover does this as a chain transaction.
        
        For our custom prover, we'll use the HTTP API approach.
        """
        # This might be the same as nextStep with a specific step value
        # or it might be a chain transaction via gRPC
        # TODO: Confirm with packet capture
        url = f"{self.base_url}/api/v1/common/task/prover/nextStep"
        return await self._post(url, data)

    async def _post(self, url: str, data: dict) -> dict:
        """Make a POST request and return parsed response."""
        if not self.session:
            await self.start()

        body = json.dumps(data, separators=(',', ':'))
        logger.debug(f"POST {url} body_len={len(body)}")

        try:
            async with self.session.post(url, data=body) as resp:
                resp_text = await resp.text()
                logger.debug(f"Response [{resp.status}]: {resp_text[:200]}")

                if resp.status != 200:
                    logger.error(f"HTTP {resp.status} from {url}: {resp_text[:500]}")
                    return {"code": -1, "msg": f"HTTP {resp.status}", "data": None}

                result = json.loads(resp_text)
                if result.get("code") != 0:
                    logger.warning(f"API error from {url}: {result}")
                return result

        except Exception as e:
            logger.error(f"Request failed to {url}: {e}")
            return {"code": -1, "msg": str(e), "data": None}
