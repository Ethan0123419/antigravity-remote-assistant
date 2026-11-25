"""Zero-trust encrypted tunnel beacon & static iOS Standalone PWA publisher for 反重力助手.

Architecture:
- Hosts the permanent HTTPS iOS Standalone Web App shell (`index.html`, `manifest.json`, `sw.js`,
  `apple-touch-icon.png`) on the repository's `gh-pages` branch (`https://<owner>.github.io/<repo>/`).
- Encrypts the PC's live `{tunnel_url, lan_url, updated_ms}` using PBKDF2-HMAC-SHA256 (100k iterations)
  + HMAC-SHA256 Counter Mode + Encrypt-then-MAC (`AGY-BEACON-V1`), derived from `WEB_ACCESS_TOKEN`.
- Publishes `beacon.json` as a single orphan commit on `gh-pages` from an isolated `.gh_pages_stage/`
  directory so `main` working tree is never touched and commit history never bloats.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
PBKDF2_ITERATIONS = 100_000
BEACON_DOMAIN_TAG = b"AGY-BEACON-V1"

GITHUB_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _derive_keys(access_token: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        access_token.strip().encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=64,
    )
    return dk[:32], dk[32:]


def _xor_keystream(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    blocks = (len(data) + 31) // 32
    for idx in range(blocks):
        ks = hmac.new(enc_key, nonce + idx.to_bytes(4, "big"), hashlib.sha256).digest()
        start = idx * 32
        end = min(start + 32, len(data))
        for j in range(start, end):
            out[j] = data[j] ^ ks[j - start]
    return bytes(out)


def encrypt_beacon_payload(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    """Encrypt connection endpoints so public `beacon.json` leaks zero IP or URL metadata."""
    if not access_token or not access_token.strip():
        raise ValueError("access_token must not be empty")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    enc_key, mac_key = _derive_keys(access_token, salt)

    pt = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = _xor_keystream(enc_key, nonce, pt)
    mac = hmac.new(mac_key, BEACON_DOMAIN_TAG + salt + nonce + ct, hashlib.sha256).digest()

    return {
        "v": 1,
        "updated_ms": int(time.time() * 1000),
        "iter": PBKDF2_ITERATIONS,
        "salt": _b64u_encode(salt),
        "nonce": _b64u_encode(nonce),
        "ct": _b64u_encode(ct),
        "mac": _b64u_encode(mac),
    }


def decrypt_beacon_payload(envelope: dict[str, Any], access_token: str) -> dict[str, Any]:
    """Verify MAC and decrypt `beacon.json` envelope."""
    if int(envelope.get("v") or 0) != 1:
        raise ValueError("Unsupported beacon version")
    salt = _b64u_decode(str(envelope["salt"]))
    nonce = _b64u_decode(str(envelope["nonce"]))
    ct = _b64u_decode(str(envelope["ct"]))
    expected_mac = _b64u_decode(str(envelope["mac"]))

    enc_key, mac_key = _derive_keys(access_token, salt)
    actual_mac = hmac.new(mac_key, BEACON_DOMAIN_TAG + salt + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(actual_mac, expected_mac):
        raise ValueError("Beacon MAC verification failed (invalid token)")

    pt = _xor_keystream(enc_key, nonce, ct)
    parsed = json.loads(pt.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Decrypted beacon payload is not an object")
    return parsed


def detect_github_pages_info(project_dir: Path) -> tuple[str, str, str, str] | None:
    """Return `(remote_url, owner, repo, pages_url)` from `git remote get-url origin`, or None."""
    extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            **extra,
        )
        if res.returncode != 0:
            return None
        remote_url = (res.stdout or "").strip()
        m = GITHUB_REMOTE_RE.search(remote_url)
        if not m:
            return None
        owner = m.group("owner")
        repo = m.group("repo")
        pages_url = f"https://{owner.lower()}.github.io/{repo}/"
        return (remote_url, owner, repo, pages_url)
    except Exception:
        return None


STATIC_PWA_SW_JS = """\
const CACHE_NAME = 'antigravity-ios-shell-v3';
const SHELL_ASSETS = ['./', './index.html', './apple-touch-icon.png', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(SHELL_ASSETS).catch(() => {})));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname.endsWith('/beacon.json') || url.hostname === 'api.github.com') {
    return;
  }
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res && res.ok && url.origin === self.location.origin) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(e.request, clone)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match('./index.html')))
  );
});
"""


def build_static_pwa_bundle(
    project_dir: Path,
    stage_dir: Path,
    *,
    owner: str,
    repo: str,
    access_token: str,
    tunnel_url: str,
    lan_url: str,
) -> dict[str, Any]:
    """Assemble the complete static iOS Standalone PWA shell + encrypted `beacon.json` into `stage_dir`."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    src_html = project_dir / "src" / "qq_agy_bridge" / "static" / "index.html"
    assets_dir = project_dir / "assets"

    html_text = src_html.read_text(encoding="utf-8")
    # Inject repository beacon metadata attribute onto <html> so standalone shell knows its API beacon source
    repo_slug = f"{owner}/{repo}"
    html_text = html_text.replace(
        '<html lang="zh-CN">',
        f'<html lang="zh-CN" data-beacon-repo="{repo_slug}">',
        1,
    )
    (stage_dir / "index.html").write_text(html_text, encoding="utf-8")
    (stage_dir / ".nojekyll").write_text("", encoding="utf-8")
    (stage_dir / "sw.js").write_text(STATIC_PWA_SW_JS, encoding="utf-8")

    # Omit `start_url` so iOS/W3C Home Screen bookmark preserves `#key=...` during initial pairing!
    manifest = {
        "name": "反重力助手",
        "short_name": "反重力助手",
        "description": "专属 iPhone 远程电脑操控与全会话 AI 编程中枢",
        "display": "standalone",
        "background_color": "#0D0D0F",
        "theme_color": "#0D0D0F",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "./apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
            {"src": "./icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "./icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    (stage_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for icon_name in ("apple-touch-icon.png", "icon-192.png", "icon-512.png"):
        src_icon = assets_dir / icon_name
        if not src_icon.is_file() and icon_name == "apple-touch-icon.png":
            src_icon = assets_dir / "icon-192.png"
        if src_icon.is_file():
            shutil.copy2(src_icon, stage_dir / icon_name)

    beacon_env = encrypt_beacon_payload(
        {
            "tunnel_url": tunnel_url.rstrip("/"),
            "lan_url": lan_url.rstrip("/"),
            "ts": int(time.time() * 1000),
        },
        access_token,
    )
    (stage_dir / "beacon.json").write_text(
        json.dumps(beacon_env, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return beacon_env


def publish_ios_pwa_and_beacon(
    project_dir: Path,
    *,
    access_token: str,
    tunnel_url: str,
    lan_url: str,
) -> tuple[bool, str]:
    """Build and force-push single-commit `gh-pages` branch with encrypted `beacon.json`.

    Never touches the `main` working directory or git index.
    Returns `(ok, pages_url_or_error)`.
    """
    gh_info = detect_github_pages_info(project_dir)
    if gh_info is None:
        return (False, "未检测到 GitHub origin 远程仓库")
    remote_url, owner, repo, pages_url = gh_info

    stage_dir = project_dir / ".gh_pages_stage"
    extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}

    try:
        # Clean previous git dir inside stage_dir so every push is 1 tiny orphan commit (~120 KB)
        git_sub = stage_dir / ".git"
        if git_sub.is_dir():
            shutil.rmtree(git_sub, ignore_errors=True)

        build_static_pwa_bundle(
            project_dir,
            stage_dir,
            owner=owner,
            repo=repo,
            access_token=access_token,
            tunnel_url=tunnel_url,
            lan_url=lan_url,
        )

        def _git(*args: str, timeout: int = 25) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=str(stage_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **extra,
            )

        _git("init", "-b", "gh-pages")
        _git("config", "user.name", "Antigravity Beacon")
        _git("config", "user.email", "beacon@antigravity.local")
        _git("add", "-A")
        c_res = _git("commit", "-m", "chore(pwa): sync encrypted tunnel beacon & iOS shell")
        if c_res.returncode != 0:
            return (False, f"生成信标提交失败：{(c_res.stderr or c_res.stdout).strip()[:120]}")

        p_res = _git("push", "-f", remote_url, "HEAD:gh-pages", timeout=30)
        if p_res.returncode != 0:
            return (False, f"推送 gh-pages 失败：{(p_res.stderr or p_res.stdout).strip()[:140]}")

        return (True, pages_url)
    except Exception as exc:
        return (False, f"同步永久入口异常：{exc}")
