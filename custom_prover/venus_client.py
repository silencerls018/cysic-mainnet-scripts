"""
Venus Prover Server gRPC Client
Communicates with venus_prover_server on localhost:7000
"""

import grpc
import os
import logging
import hashlib
import base64
from pathlib import Path

logger = logging.getLogger(__name__)


class VenusProverClient:
    """
    Client for venus_prover_server gRPC service.
    
    The venus_prover_server handles ZK proof generation using GPU acceleration.
    It listens on a gRPC port (default 7000) and accepts prove requests.
    
    Since we don't have the .proto file, this implementation uses the
    proof file output approach: venus_prover_server writes proof to disk,
    and we read it from there.
    
    Based on log analysis, the prover binary:
    1. Downloads task input from S3
    2. Calls venus_prover_server via gRPC to generate proof
    3. Reads proof output from VENUS_OUT_DIR
    """

    def __init__(self, grpc_endpoint: str = "127.0.0.1:7000",
                 venus_dir: str = None, cache_dir: str = "./cache"):
        self.grpc_endpoint = grpc_endpoint
        self.venus_dir = venus_dir or os.path.expanduser("~/venus_v0_1_6")
        self.venus_out_dir = os.path.join(self.venus_dir, "tmp")
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.venus_out_dir, exist_ok=True)

    async def generate_proof(self, task_hash: str, input_files: list) -> dict:
        """
        Generate a ZK proof for the given task.
        
        This method:
        1. Downloads task input files from S3 URLs
        2. Calls venus_prover_server to generate proof
        3. Returns proof hash and proof data
        
        Args:
            task_hash: The task hash identifier
            input_files: List of dicts with 'BlockHeight' and 'S3Url'
            
        Returns:
            dict with 'proof_hash' and 'proof_data' (base64)
        """
        import aiohttp

        # Step 1: Download input files
        input_paths = []
        async with aiohttp.ClientSession() as session:
            for item in input_files:
                block_height = item["BlockHeight"]
                s3_url = item["S3Url"]
                local_path = os.path.join(self.cache_dir, f"{block_height}.bin")

                if not os.path.exists(local_path):
                    logger.info(f"Downloading input for block {block_height}: {s3_url}")
                    async with session.get(s3_url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            with open(local_path, 'wb') as f:
                                f.write(data)
                            logger.info(f"Downloaded {len(data)} bytes for block {block_height}")
                        else:
                            raise Exception(f"Failed to download input: HTTP {resp.status} from {s3_url}")
                else:
                    logger.info(f"Using cached input for block {block_height}")

                input_paths.append(local_path)

        # Step 2: Call venus_prover_server via gRPC
        # NOTE: Since we don't have the exact .proto definition,
        # we use a subprocess approach as fallback.
        # The official prover calls gRPC with the input file path.
        proof_result = await self._call_venus_prover(task_hash, input_paths)

        return proof_result

    async def _call_venus_prover(self, task_hash: str, input_paths: list) -> dict:
        """
        Call venus_prover_server to generate proof.
        
        Strategy:
        - Try gRPC call first (if we can discover the proto interface)
        - Fallback: Use the venus_prover_demo binary if available
        
        The gRPC service likely has an interface like:
            service VenusProver {
                rpc Prove(ProveRequest) returns (ProveResponse);
            }
            
        Based on log analysis, the input is a file path and the output
        is proof data written to VENUS_OUT_DIR.
        """
        try:
            return await self._call_via_grpc(task_hash, input_paths)
        except Exception as e:
            logger.warning(f"gRPC call failed: {e}, this needs protocol investigation")
            raise

    async def _call_via_grpc(self, task_hash: str, input_paths: list) -> dict:
        """
        Direct gRPC call to venus_prover_server.
        
        TODO: This requires the actual .proto definition.
        For now, we'll use a generic unary call approach.
        
        From venus_prover_server strings analysis, the service likely accepts:
        - Input file path(s)
        - Task identifier
        And returns:
        - Proof bytes
        - Proof hash
        """
        # PLACEHOLDER: This needs the actual proto definition
        # You need to run: strings ~/cysic-prover/venus_prover_server | grep -i "proto\|service\|rpc"
        # to discover the service definition
        
        # For now, we'll read proof output from the standard output directory
        # The official prover triggers the proof and waits for output files
        
        raise NotImplementedError(
            "gRPC proto definition needed. "
            "Run: strings ~/cysic-prover/venus_prover_server | grep -iE 'service|rpc|proto' | head -30 "
            "to discover the interface."
        )

    def read_proof_output(self, task_hash: str) -> dict:
        """
        Read proof output files generated by venus_prover_server.
        
        After proof generation, files are typically written to VENUS_OUT_DIR.
        Returns proof_hash and base64-encoded proof_data.
        """
        # Look for proof output files
        out_dir = Path(self.venus_out_dir)
        
        # Common output patterns based on log analysis
        possible_patterns = [
            out_dir / f"{task_hash}*",
            out_dir / "proof_*",
            out_dir / "vadcop_final*",
        ]
        
        # Find proof files
        proof_files = []
        for pattern in possible_patterns:
            proof_files.extend(out_dir.glob(pattern.name))

        if not proof_files:
            return None

        # Read and combine proof data
        proof_data_parts = []
        for pf in sorted(proof_files):
            with open(pf, 'rb') as f:
                proof_data_parts.append(f.read())

        combined_proof = b''.join(proof_data_parts)
        proof_hash = hashlib.sha256(combined_proof).hexdigest()
        proof_data_b64 = base64.b64encode(combined_proof).decode()

        return {
            "proof_hash": proof_hash,
            "proof_data": proof_data_b64,
        }
