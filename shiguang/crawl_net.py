"""Public-network HTTP connections with DNS checked at the actual connection boundary."""
import ipaddress
import socket

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError


def public_addresses(host, port):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses:
        raise ValueError('网址无法解析')
    for address in addresses:
        ip = ipaddress.ip_address(address.split('%')[0])
        # Match the gateway's existing fake-IP DNS policy. These are mapped by
        # the local VPN to public destinations, never ordinary LAN addresses.
        fake = ip.version == 4 and ip in ipaddress.ip_network('198.18.0.0/15')
        if not ip.is_global and not fake:
            raise ValueError('不能采集内网或本机地址')
    return addresses


class PublicConnection:
    def _new_conn(self):
        hostname = self._dns_host
        addresses = public_addresses(hostname, self.port)
        # Only the socket destination changes. Restore the original hostname
        # before HTTPS verifies certificates/SNI and HTTP generates its Host.
        try:
            for index, address in enumerate(addresses):
                self._dns_host = address
                try:
                    return super()._new_conn()
                except (ConnectTimeoutError, NewConnectionError):
                    if index + 1 == len(addresses):
                        raise
        finally:
            self._dns_host = hostname


class PublicHTTPConnection(PublicConnection, HTTPConnection):
    pass


class PublicHTTPSConnection(PublicConnection, HTTPSConnection):
    pass


class PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {'http': PublicHTTPPool, 'https': PublicHTTPSPool}
