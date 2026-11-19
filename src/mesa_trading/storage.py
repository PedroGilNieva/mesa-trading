"""Lectura y escritura del bucket o de un directorio local con las mismas rutas."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


class DataRoot:
    def read_text(self, key: str) -> str | None:
        raise NotImplementedError

    def write_text(self, key: str, text: str) -> None:
        raise NotImplementedError

    def list_prefix(self, prefix: str, max_results: int | None = None) -> tuple[list[str], bool]:
        _ = (prefix, max_results)
        return [], False


class LocalRoot(DataRoot):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def read_text(self, key: str) -> str | None:
        path = self.root / key
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def write_text(self, key: str, text: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def list_prefix(self, prefix: str, max_results: int | None = None) -> tuple[list[str], bool]:
        base = self.root / prefix
        if base.is_file():
            return [prefix], False
        if not base.exists():
            return [], False
        found = sorted(path.relative_to(self.root).as_posix() for path in base.rglob("*") if path.is_file())
        if max_results is not None and len(found) > max_results:
            # El orden lexicográfico de los ciclos es el tiempo: el tope se queda con los últimos.
            return found[-max_results:], True
        return found, False


class GcsRoot(DataRoot):
    # El token del metadata server dura ~1 h. Pedirlo en cada objeto duplicaba
    # las llamadas HTTP de /decisiones/hoy; se reusa unos minutos.
    TOKEN_TTL_S = 240.0

    def __init__(self, bucket: str, token_provider=None, clock=None) -> None:
        self.bucket = bucket
        self.token_provider = token_provider or metadata_token
        self._clock = clock or time.monotonic
        self._token: tuple[float, str] | None = None
        self._token_lock = threading.Lock()

    def _bearer(self) -> str:
        with self._token_lock:
            now = self._clock()
            if self._token is not None and now - self._token[0] < self.TOKEN_TTL_S:
                return self._token[1]
            token = self.token_provider()
            self._token = (now, token)
            return token

    def read_text(self, key: str) -> str | None:
        url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{_quote(key)}?alt=media"
        status, body = _request("GET", url, self._bearer())
        if status == 404:
            return None
        if status != 200:
            return None
        return body.decode("utf-8")

    def write_text(self, key: str, text: str) -> None:
        url = f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o?uploadType=media&name={_quote(key)}"
        status, _body = _request("POST", url, self._bearer(), text.encode("utf-8"), "application/json")
        if status not in (200, 201):
            raise RuntimeError(f"no se pudo escribir {key}")

    def list_prefix(self, prefix: str, max_results: int | None = None) -> tuple[list[str], bool]:
        from mesa_trading.gcs import _list

        return _list(self.bucket, prefix, self._bearer(), max_results=max_results)


def data_root_from_env() -> DataRoot:
    bucket = os.environ.get("MESA_BUCKET", "").strip()
    if bucket:
        return GcsRoot(bucket)
    path = os.environ.get("MESA_DATA_DIR", "/tmp/mesa-trading")
    return LocalRoot(Path(path))


def metadata_token() -> str:
    url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload["access_token"])


class GcpSecretKey:
    """Lee el secreto en el momento de la llamada. Si no hay versión, devuelve None."""

    def __init__(self, project: str, secret_id: str, token_provider=None) -> None:
        self.project = project
        self.secret_id = secret_id
        self.token_provider = token_provider or metadata_token

    def load(self) -> str | None:
        try:
            token = self.token_provider()
            url = (
                "https://secretmanager.googleapis.com/v1/projects/"
                f"{self.project}/secrets/{self.secret_id}/versions/latest:access"
            )
            status, body = _request("GET", url, token)
            if status != 200:
                return None
            payload = json.loads(body.decode("utf-8"))
            import base64

            data = payload.get("payload", {}).get("data")
            if not data:
                return None
            return base64.b64decode(data).decode("utf-8").strip() or None
        except Exception:
            return None


def _quote(key: str) -> str:
    return key.replace("/", "%2F")


def _request(method: str, url: str, token: str, body: bytes | None = None, content_type: str = "application/json"):  # noqa: E302
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()

