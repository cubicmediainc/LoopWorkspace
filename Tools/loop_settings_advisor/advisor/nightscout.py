"""Client Nightscout en lecture seule (API v1).

Authentification : Nightscout 15 n'accepte dans l'en-tête
`Authorization: Bearer` qu'un JWT. Un jeton d'accès brut (« readable-… »)
doit être passé en paramètre `?token=`. Le client lit donc le jeton dans
la variable d'environnement NIGHTSCOUT_TOKEN et l'ajoute à chaque requête.
Le jeton n'est jamais journalisé.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import requests

DEFAULT_URL = "https://nightscout.domainecullens.ca"


class NightscoutAuthError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class NightscoutClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = 60.0, session: requests.Session | None = None):
        self.base_url = (base_url or os.environ.get("NIGHTSCOUT_URL") or DEFAULT_URL).rstrip("/")
        self._token = token if token is not None else os.environ.get("NIGHTSCOUT_TOKEN")
        self.timeout = timeout
        self.session = session or requests.Session()

    # ------------------------------------------------------------------ bas niveau
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        if self._token:
            params["token"] = self._token
        url = f"{self.base_url}{path}"
        for attempt in range(4):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.ConnectionError:
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code == 401:
                # Ne pas réessayer : Nightscout pénalise les échecs répétés.
                raise NightscoutAuthError(
                    "401 Unauthorized : définissez NIGHTSCOUT_TOKEN (jeton d'accès « readable »).")
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    def verify_auth(self) -> dict:
        data = self.get("/api/v1/verifyauth")
        return data.get("message", data)

    def status(self) -> dict:
        return self.get("/api/v1/status.json")

    # ------------------------------------------------------------------ lectures paginées
    def _windowed(self, path: str, field: str, start: datetime, end: datetime,
                  as_millis: bool, chunk: timedelta = timedelta(days=3),
                  count: int = 10000, extra: dict | None = None) -> Iterator[dict]:
        """Lit [start, end) par fenêtres ; redécoupe une fenêtre si elle est pleine."""
        stack = []
        cur = start
        while cur < end:
            nxt = min(cur + chunk, end)
            stack.append((cur, nxt))
            cur = nxt
        stack.reverse()
        while stack:
            a, b = stack.pop()
            lo = int(a.timestamp() * 1000) if as_millis else _iso(a)
            hi = int(b.timestamp() * 1000) if as_millis else _iso(b)
            params = {f"find[{field}][$gte]": lo, f"find[{field}][$lt]": hi, "count": count}
            params.update(extra or {})
            docs = self.get(path, params)
            if len(docs) >= count and (b - a) > timedelta(minutes=30):
                mid = a + (b - a) / 2
                stack.append((mid, b))
                stack.append((a, mid))
                continue
            yield from docs

    def entries(self, start: datetime, end: datetime) -> list[dict]:
        return list(self._windowed("/api/v1/entries/sgv.json", "date", start, end, as_millis=True))

    def treatments(self, start: datetime, end: datetime) -> list[dict]:
        return list(self._windowed("/api/v1/treatments.json", "created_at", start, end, as_millis=False))

    def devicestatus(self, start: datetime, end: datetime) -> list[dict]:
        return list(self._windowed("/api/v1/devicestatus.json", "created_at", start, end,
                                   as_millis=False, chunk=timedelta(days=1)))

    def profiles(self) -> list[dict]:
        return self.get("/api/v1/profile.json", {"count": 1000})

    # ------------------------------------------------------------------ inventaire
    def exists_before(self, path: str, field: str, when: datetime, as_millis: bool) -> bool:
        v = int(when.timestamp() * 1000) if as_millis else _iso(when)
        return len(self.get(path, {f"find[{field}][$lt]": v, "count": 1})) > 0

    def oldest_date(self, path: str, field: str, as_millis: bool,
                    floor: datetime = datetime(2015, 1, 1, tzinfo=timezone.utc)) -> datetime | None:
        """Recherche dichotomique de la plus ancienne donnée (précision ~1 h).

        L'API v1 trie par date décroissante ; on évite ainsi de tout télécharger.
        """
        hi = datetime.now(timezone.utc) + timedelta(minutes=1)
        if not self.exists_before(path, field, hi, as_millis):
            return None
        lo = floor
        if self.exists_before(path, field, lo, as_millis):
            return lo
        while hi - lo > timedelta(hours=1):
            mid = lo + (hi - lo) / 2
            if self.exists_before(path, field, mid, as_millis):
                hi = mid
            else:
                lo = mid
        return hi
