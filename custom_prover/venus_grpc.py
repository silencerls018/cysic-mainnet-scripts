"""
Cysic Prover - Venus Prover Server gRPC Client

Calls venus_prover_server on localhost:7000 to generate ZK proofs.

Proto:
    service VenusProver { rpc Prove(ProveRequest) returns (ProveResponse); }
    message ProveRequest { uint64 block_number = 1; string input_presigned_url = 2; }
    message ProveResponse { bytes proof_bytes = 1; }
"""

import grpc
import logging

logger = logging.getLogger(__name__)


def _varint_encode(value):
    result = []
    while value > 0x7f:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value & 0x7f)
    return bytes(result)


def _encode_uint64_field(field_number, value):
    tag = (field_number << 3) | 0
    return _varint_encode(tag) + _varint_encode(value)


def _encode_string_field(field_number, value):
    tag = (field_number << 3) | 2
    encoded = value.encode('utf-8')
    return _varint_encode(tag) + _varint_encode(len(encoded)) + encoded


def _decode_varint(data, offset):
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7f) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset


def _decode_response(data):
    """Decode ProveResponse: field 1 = bytes proof_bytes"""
    offset = 0
    proof_bytes = b''
    while offset < len(data):
        tag_wire, offset = _decode_varint(data, offset)
        field_number = tag_wire >> 3
        wire_type = tag_wire & 0x07
        if wire_type == 2:
            length, offset = _decode_varint(data, offset)
            if field_number == 1:
                proof_bytes = data[offset:offset + length]
            offset += length
        elif wire_type == 0:
            _, offset = _decode_varint(data, offset)
        else:
            break
    return proof_bytes


class VenusGRPCClient:
    def __init__(self, endpoint="127.0.0.1:7000"):
        self.endpoint = endpoint
        self.channel = None

    def connect(self):
        self.channel = grpc.insecure_channel(
            self.endpoint,
            options=[
                ('grpc.max_receive_message_length', 100 * 1024 * 1024),
                ('grpc.max_send_message_length', 100 * 1024 * 1024),
            ]
        )
        logger.info(f"gRPC channel: {self.endpoint}")

    def prove(self, block_number: int, input_url: str, timeout=600) -> bytes:
        """Call Prove RPC. Returns raw proof bytes."""
        if not self.channel:
            self.connect()

        request_bytes = (
            _encode_uint64_field(1, block_number) +
            _encode_string_field(2, input_url)
        )

        logger.info(f"gRPC Prove: block={block_number}")

        try:
            response_bytes = self.channel.unary_unary(
                '/venus.prover.v1.VenusProver/Prove',
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )(request_bytes, timeout=timeout)

            proof_bytes = _decode_response(response_bytes)
            if proof_bytes:
                logger.info(f"gRPC Prove OK: {len(proof_bytes)} bytes")
                return proof_bytes
            else:
                logger.error("gRPC Prove: empty response")
                return None
        except grpc.RpcError as e:
            logger.error(f"gRPC error: {e.code()} - {e.details()}")
            return None
        except Exception as e:
            logger.error(f"gRPC exception: {e}")
            return None

    def close(self):
        if self.channel:
            self.channel.close()
