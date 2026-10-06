import base64
import hashlib
import hmac
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCryptoError(ValueError):
    """企业微信报文验签或加解密失败。"""


def calculate_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    """按照企业微信规则计算消息签名。"""
    parts = sorted((token, timestamp, nonce, encrypted))
    return hashlib.sha1("".join(parts).encode()).hexdigest()  # noqa: S324


def _pkcs7_pad(payload: bytes) -> bytes:
    block_size = 32
    padding = block_size - len(payload) % block_size
    return payload + bytes([padding]) * padding


def _pkcs7_unpad(payload: bytes) -> bytes:
    if not payload:
        raise WeComCryptoError("解密结果为空")
    padding = payload[-1]
    if padding < 1 or padding > 32 or payload[-padding:] != bytes([padding]) * padding:
        raise WeComCryptoError("消息填充无效")
    return payload[:-padding]


@dataclass(frozen=True)
class WeComCrypto:
    token: str
    encoding_aes_key: str
    receive_id: str

    def __post_init__(self) -> None:
        try:
            aes_key = base64.b64decode(f"{self.encoding_aes_key}=", validate=True)
        except (ValueError, TypeError) as exc:
            raise WeComCryptoError("EncodingAESKey格式无效") from exc
        if len(aes_key) != 32:
            raise WeComCryptoError("EncodingAESKey解码后必须为32字节")
        object.__setattr__(self, "_aes_key", aes_key)

    def verify_signature(
        self,
        signature: str,
        timestamp: str,
        nonce: str,
        encrypted: str,
    ) -> None:
        expected = calculate_signature(self.token, timestamp, nonce, encrypted)
        if not hmac.compare_digest(expected, signature):
            raise WeComCryptoError("消息签名校验失败")

    def decrypt(self, encrypted: str) -> str:
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            decryptor = Cipher(
                algorithms.AES(self._aes_key), modes.CBC(self._aes_key[:16])
            ).decryptor()
            plaintext = _pkcs7_unpad(decryptor.update(ciphertext) + decryptor.finalize())
            if len(plaintext) < 20:
                raise WeComCryptoError("解密消息长度无效")
            message_length = struct.unpack(">I", plaintext[16:20])[0]
            message_end = 20 + message_length
            if message_end > len(plaintext):
                raise WeComCryptoError("解密消息长度不匹配")
            message = plaintext[20:message_end]
            receive_id = plaintext[message_end:].decode()
            decoded_message = message.decode()
        except WeComCryptoError:
            raise
        except (ValueError, TypeError, struct.error, UnicodeDecodeError) as exc:
            raise WeComCryptoError("消息解密失败") from exc
        if receive_id != self.receive_id:
            raise WeComCryptoError("消息接收方不匹配")
        return decoded_message

    def encrypt(self, message: str) -> str:
        message_bytes = message.encode()
        payload = (
            os.urandom(16)
            + struct.pack(">I", len(message_bytes))
            + message_bytes
            + self.receive_id.encode()
        )
        encryptor = Cipher(algorithms.AES(self._aes_key), modes.CBC(self._aes_key[:16])).encryptor()
        ciphertext = encryptor.update(_pkcs7_pad(payload)) + encryptor.finalize()
        return base64.b64encode(ciphertext).decode("ascii")
