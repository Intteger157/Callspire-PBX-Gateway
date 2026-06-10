"""Mount browser softphone (static UI + /api session proxy) on the PBX Gateway FastAPI app."""

from gateway_web_softphone.install import install_web_softphone
from gateway_web_softphone.mount import WebSoftphoneConfig, mount_web_softphone

__all__ = ["install_web_softphone", "mount_web_softphone", "WebSoftphoneConfig"]
