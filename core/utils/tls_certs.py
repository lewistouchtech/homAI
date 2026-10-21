# -*- coding: utf-8 -*-
"""自签 TLS 证书：让浏览器把管理台当作「安全来源」

为什么需要（2026-10-14 主人报障 + 已批准方案）：
    浏览器只在「安全来源」才提供 navigator.mediaDevices —— https，或 localhost/127.0.0.1。
    用户在自己的手机/电脑上用「家里 IP 或域名 + http」打开管理台时讲不了麦克风，
    声纹录入 / 摄像头拍照全部不可用（详见 docs/plans/2026-10-14_声纹录入与HTTPS必要性方案.md）。
    所以给服务器加一份自签证书 + 一个 https 端口，设备侧那条 http/ws 链路完全不动。

两份证书的关系（不要混）：
    - 这里生成的是**服务器自己的身份证书**，同时当 CA 用（BasicConstraints CA:TRUE），
      这样用户在自己设备上把它装成「受信任的根证书」就能消掉浏览器红警告；
      就算不装，点一次「继续前往」之后 https 也算安全来源，麦克风照样能用。
    - SAN 必须同时带 IP 和主机名，否则换地址访问又会警告。

对外只暴露：
    ensure_self_signed()  首次生成 / 已存在则原样返回
    describe()            证书信息（到期时间、SAN、指纹）给管理台展示
    ssl_context_for()     给 aiohttp / websockets 用的 SSLContext
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

# 证书有效期：825 天（Chromium 对「用户安装的根证书」不再限制 398 天，但留足余量）
DEFAULT_DAYS = 825

# 浏览器「安全来源」两个端口（可用 config server.https_port / server.wss_port 覆盖）
#   8443 给页面与 /api（aiohttp）；9443 给网页端 WebSocket（websockets 库是另一个服务器）
DEFAULT_HTTPS_PORT = 8443
DEFAULT_WSS_PORT = 9443

DEFAULT_CERT_PATH = Path("data") / "tls" / "server.crt"
DEFAULT_KEY_PATH = Path("data") / "tls" / "server.key"


def _project_root() -> Path:
    """项目根（core/utils/tls_certs.py → 上三级）"""
    return Path(__file__).resolve().parents[2]


def default_paths() -> tuple[Path, Path]:
    root = _project_root()
    return root / DEFAULT_CERT_PATH, root / DEFAULT_KEY_PATH


def collect_san_names(extra_hosts: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """收集本机应该进 SAN 的主机名与 IP。

    ⚠️ 只收集「本机网卡实测值」，不读配置里的 local_ip（配置可能过期/是别的机器），
    与原「服务器信息」页的口径一致。
    """
    hostnames: list[str] = []
    ips: list[str] = ["127.0.0.1"]

    def _add_host(value: Optional[str]) -> None:
        value = (value or "").strip()
        if value and value not in hostnames:
            hostnames.append(value)

    _add_host("localhost")
    try:
        raw_host = socket.gethostname()
    except Exception:
        raw_host = ""
    if raw_host:
        short = raw_host.split(".")[0]
        _add_host(raw_host)
        _add_host(short)
        # macOS 的 mDNS 名，手机/平板用它访问也能过证书校验
        _add_host(f"{short}.local")
    for h in extra_hosts:
        _add_host(h)

    # 本机所有 IPv4（含各网卡），手机/电脑访问用的就是这些
    def _add_ip(ip: Optional[str]) -> None:
        ip = (ip or "").strip()
        if not ip or ip in ips:
            return
        # 滤掉噪声地址：网络号/广播号、链路本地、通配
        if ip.endswith(".0") or ip.endswith(".255"):
            return
        if ip.startswith("169.254.") or ip == "0.0.0.0":
            return
        ips.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add_ip(info[4][0])
    except Exception:
        pass
    try:
        # 兜底：拿「默认出口网卡」的地址（hostname 解析不到时）
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            primary = sock.getsockname()[0]
        finally:
            sock.close()
        _add_ip(primary)
    except Exception:
        pass

    return hostnames, ips


def ensure_self_signed(
    cert_path: Optional[Path] = None,
    key_path: Optional[Path] = None,
    extra_hosts: Iterable[str] = (),
    days: int = DEFAULT_DAYS,
    force: bool = False,
) -> dict:
    """证书不存在就生成一份；已存在则原样保留（不覆盖用户已信任的那份）。

    返回 {"status": "exists"|"created"|"failed", "cert": str, "key": str,
          "hostnames": [...], "ips": [...], "error": str|None}
    """
    cert_path = Path(cert_path) if cert_path else default_paths()[0]
    key_path = Path(key_path) if key_path else default_paths()[1]
    hostnames, ips = collect_san_names(extra_hosts)
    result = {
        "status": "failed",
        "cert": str(cert_path),
        "key": str(key_path),
        "hostnames": hostnames,
        "ips": ips,
        "error": None,
    }

    if cert_path.exists() and key_path.exists() and not force:
        result["status"] = "exists"
        return result

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "HomeBrain"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HomeBrain"),
        ])
        san_entries = [x509.DNSName(h) for h in hostnames]
        for ip in ips:
            try:
                san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except ValueError:
                continue

        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)          # 自签：自己签发自己
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))   # 留 1 天余量，防设备时钟略慢
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            # CA:TRUE —— 用户设备把它装成「受信任的根证书」时 iOS/Android 才认
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=True, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(key, hashes.SHA256())
        )

        cert_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(cert_path.parent, 0o700)
        except OSError:
            pass

        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        for p in (key_path, cert_path):
            try:
                os.chmod(p, 0o600)      # 私钥/证书只给本用户读
            except OSError:
                pass

        result["status"] = "created"
    except Exception as e:      # 生成失败不能影响主服务，如实上报
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def describe(cert_path: Optional[Path] = None) -> Optional[dict]:
    """读证书信息给管理台展示；读不了返回 None（不抛）"""
    cert_path = Path(cert_path) if cert_path else default_paths()[0]
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
    except Exception:
        return None
    try:
        data = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(data)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {
            "path": str(cert_path),
            "subject": cert.subject.rfc4514_string(),
            "serial": format(cert.serial_number, "x"),
            "not_before": cert.not_valid_before_utc.isoformat(),
            "not_after": cert.not_valid_after_utc.isoformat(),
            "days_left": (cert.not_valid_after_utc - datetime.now(timezone.utc)).days,
            "sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "dns_names": list(san.get_values_for_type(x509.DNSName)),
            "ips": [str(i) for i in san.get_values_for_type(x509.IPAddress)],
        }
    except Exception:
        return None


def ssl_context_for(cert_path: Optional[Path] = None, key_path: Optional[Path] = None) -> Optional[ssl.SSLContext]:
    """给 aiohttp / websockets 用的 SSLContext；文件缺失或损坏返回 None（调用方据此跳过 https 端口）"""
    cert_path = Path(cert_path) if cert_path else default_paths()[0]
    key_path = Path(key_path) if key_path else default_paths()[1]
    if not (cert_path.exists() and key_path.exists()):
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
    except Exception:
        return None
