from p2p_python.tools import *
from p2p_python.peer import *
from p2p_python.sockpool import *
from typing import TYPE_CHECKING, Tuple, Optional
from srudp import SecureReliableSocket
from ecdsa.keys import VerifyingKey
from io import BytesIO
import socket as s
import logging
import random

if TYPE_CHECKING:
    from p2p_python.peer2peer import Peer2Peer

log = logging.getLogger(__name__)


class MediatorCmd(CmdThreadBase):
    """
    work as mediator of connection

    * input: issuer_info + issuer_addr + dest_pubkey
    * output: dest_info + dest_addr
    """
    cmd = InnerCmd.REQUEST_MEDIATOR

    @staticmethod
    def encode(issuer_info: PeerInfo, issuer_addr: FormalAddr, dest_pubkey: VerifyingKey) -> bytes:  # type: ignore
        io = BytesIO()
        issuer_info.to_bytes(io)
        issuer_addr.to_bytes(io)
        dest_pubkey_bytes: bytes = dest_pubkey.to_string()
        io.write(len(dest_pubkey_bytes).to_bytes(4, "big"))
        io.write(dest_pubkey_bytes)
        return io.getvalue()

    @staticmethod
    def decode(io: BytesIO) -> Tuple[PeerInfo, FormalAddr]:
        info = PeerInfo.from_bytes(io)
        addr = FormalAddr.from_bytes(io)
        assert len(io.getbuffer()) == io.tell(), (len(io.getbuffer()), io.tell())
        return info, addr

    @staticmethod
    def thread(res_fnc: _ResponseFuc, body: bytes, sock: 'Sock', p2p: 'Peer2Peer') -> None:
        try:
            # detect request peer
            req_peer = p2p.get_peer_by_sock(sock)
            assert req_peer is not None
            req_pubkey = req_peer.get_validated_key()
            assert req_pubkey is not None

            # decode
            io = BytesIO(body)
            issuer_info = PeerInfo.from_bytes(io)
            issuer_addr = FormalAddr.from_bytes(io)
            length = int.from_bytes(io.read(4), "big")
            dest_pubkey = VerifyingKey.from_string(io.read(length), curve=CURVE)
            assert len(io.getbuffer()) == io.tell(), (len(io.getbuffer()), io.tell())

            # find destination
            for dest_peer in p2p.peers:
                public_key = dest_peer.get_validated_key()
                if public_key and public_key == dest_pubkey:
                    break
            else:
                raise AssertionError(f"not found destination {dest_pubkey.to_string().hex()}")

            # ask connection
            body = AskSrudpCmd.encode(issuer_info, issuer_addr)
            response, _res_pock = p2p.throw_command(dest_peer, InnerCmd.REQUEST_ASK_SRUDP, body)

            # return response directly
            res_fnc(_SUCCESS, response)

            # success
            return

        except ConnectionError as e:
            log.debug(f"ConnectionError mediator_thread(): {e}")
        except AssertionError as e:
            log.debug(f"AssertionError mediator_thread(): {e}")
        except Exception:
            log.warning("mediator_thread()", exc_info=True)

        # failed
        res_fnc(_FAILED, b"mediator_thread() failed")


class AskSrudpCmd(CmdThreadBase):
    """
    ask new srudp connection

    * input: issuer_info + issuer_addr
    * output: dest_info + dest_addr
    """
    cmd = InnerCmd.REQUEST_ASK_SRUDP

    @staticmethod
    def encode(issuer_info: PeerInfo, issuer_addr: FormalAddr) -> bytes:  # type: ignore
        io = BytesIO()
        issuer_info.to_bytes(io)
        issuer_addr.to_bytes(io)
        return io.getvalue()

    @staticmethod
    def decode(io: BytesIO) -> Tuple[PeerInfo, FormalAddr]:
        info = PeerInfo.from_bytes(io)
        addr = FormalAddr.from_bytes(io)
        assert len(io.getbuffer()) == io.tell(), (len(io.getbuffer()), io.tell())
        return info, addr

    @staticmethod
    def thread(res_fnc: _ResponseFuc, body: bytes, sock: 'Sock', p2p: 'Peer2Peer') -> None:
        try:
            # detect mediate peer
            mediate_peer = p2p.get_peer_by_sock(sock)
            assert mediate_peer is not None

            # decode
            io = BytesIO(body)
            issuer_info = PeerInfo.from_bytes(io)
            issuer_addr = FormalAddr.from_bytes(io)

            # find my address (destination)
            for address in p2p.my_info.addresses:
                if address.host.version == issuer_addr.host.version:
                    if issuer_addr.host.is_loopback:
                        dest_addr = FormalAddr(address.host, random.randint(1024, 65535))
                    else:
                        dest_addr = FormalAddr(address.host, issuer_addr.port)
                    break
            else:
                raise AssertionError("not found my connect address")

            # srudp connect
            new_sock = SecureReliableSocket(s.AF_INET if issuer_addr.host.version == 4 else s.AF_INET6)
            if issuer_addr.host.is_loopback:
                log.debug(f"issuer's address is loopback {issuer_addr}")
                fut = executor.submit(new_sock.connect, issuer_addr, dest_addr.port)
            else:
                fut = executor.submit(new_sock.connect, issuer_addr)

            # return response
            io = BytesIO()
            p2p.my_info.to_bytes(io)
            dest_addr.to_bytes(io)
            res_fnc(_SUCCESS, io.getvalue())

            # wait for srudp connect success
            fut.result(20.0)

            # wait for establish
            # note: raise ConnectionError if failed
            new_sock.settimeout(0.0)
            issuer_sock = Sock(
                new_sock, p2p._callback_recv, SockType.INBOUND, issuer_info.public_key, p2p.pool.secret_key)
            p2p.pool.add_sock(issuer_sock)

            # update sock's flag
            issuer_sock.validate_the_other(True).wait(20.0)
            issuer_sock.measure_delay_time(True).wait(20.0)

            # note: srudp is encrypted in low-layer
            # issuer_sock.establish_encryption(True).wait(20.0)

            # after validation success, add sock to peer or create new peer
            if issuer_sock.flags & SockControl.VALIDATED:
                issuer_peer = p2p.get_peer_by_pubkey(issuer_info.public_key)
                if issuer_peer is None:
                    p2p.peers.append(Peer(issuer_info))
                else:
                    issuer_peer.socks.append(issuer_sock)
            else:
                log.debug(f"connected by srudp but validation failed {issuer_sock}")
                issuer_sock.close()

            # success
            return

        except AssertionError as e:
            log.debug(f"AssertionError ask_srudp_thread(): {e}")
        except ConnectionError as e:
            log.debug(f"ConnectionError ask_srudp_thread(): {e}")
        except Exception:
            log.warning("ask_srudp_thread()", exc_info=True)

        # failed
        res_fnc(_FAILED, b"ask_srudp_thread() failed")


__all__ = [
    "MediatorCmd",
    "AskSrudpCmd",
]