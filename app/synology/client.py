"""Client asynchrone de l'API Web DSM (SYNO.API.Auth + SYNO.FileStation.*).

Principe de sécurité : chaque utilisateur du site se connecte avec **ses propres
identifiants DSM** et obtient un `sid` personnel. Tous les appels File Station
réalisés pour son compte utilisent ce `sid`, ce qui délègue intégralement le
contrôle d'accès à DSM (permissions de partage **et** ACL avancées).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from .models import DSMSession, RemoteFile, ShareInfo

logger = logging.getLogger(__name__)

#: Codes communs à toutes les API DSM (guide « DSM Login Web API », ch. 2).
DSM_COMMON_ERRORS: dict[int, str] = {
    100: "Erreur inconnue.",
    101: "Paramètre d'API, de méthode ou de version manquant.",
    102: "API demandée introuvable sur le NAS.",
    103: "Méthode demandée introuvable.",
    104: "Version d'API non supportée par le NAS.",
    105: "La session n'a pas la permission requise.",
    106: "Session expirée, veuillez vous reconnecter.",
    107: "Session interrompue par une connexion en double.",
    114: "Paramètres manquants pour cette API.",
    119: "Session invalide (SID ou SynoToken).",
    150: "L'adresse IP de la requête ne correspond pas à celle de la connexion.",
    160: "Privilège d'application insuffisant : autorisez « File Station » pour ce compte "
    "(Panneau de configuration → Utilisateur → Applications).",
}

#: Codes propres à SYNO.API.Auth (guide « DSM Login Web API », ch. 4).
DSM_AUTH_ERRORS: dict[int, str] = {
    400: "Identifiant ou mot de passe incorrect.",
    401: "Compte désactivé.",
    402: "Permission refusée.",
    403: "Code de vérification en deux étapes requis.",
    404: "Code de vérification en deux étapes incorrect.",
    405: "Accès au portail applicatif refusé pour ce compte.",
    406: "L'authentification à deux facteurs est obligatoire pour ce compte.",
    407: "Adresse IP bloquée par DSM (trop de tentatives échouées).",
    408: "Le mot de passe a expiré et ne peut plus être changé.",
    409: "Le mot de passe du compte a expiré.",
    410: "Le mot de passe doit être changé avant de continuer.",
    411: "Compte verrouillé après trop de tentatives.",
}

#: Codes propres aux opérations de fichiers File Station (guide « File Station », ch. 2).
#: Attention : les mêmes numéros ont un sens différent sous SYNO.API.Auth.
FILESTATION_ERRORS: dict[int, str] = {
    400: "Paramètre d'opération de fichier invalide.",
    401: "Erreur inconnue lors de l'opération de fichier.",
    402: "Le système est trop occupé.",
    403: "Cet utilisateur n'a pas le droit d'effectuer cette opération.",
    404: "Ce groupe n'a pas le droit d'effectuer cette opération.",
    405: "Cet utilisateur et ce groupe n'ont pas le droit d'effectuer cette opération.",
    406: "Informations d'utilisateur ou de groupe indisponibles.",
    407: "Opération non permise.",
    408: "Fichier ou dossier introuvable.",
    409: "Système de fichiers non supporté.",
    410: "Échec de connexion au système de fichiers distant.",
    411: "Système de fichiers en lecture seule.",
    417: "Erreur d'entrée/sortie.",
    418: "Nom ou chemin illégal.",
    421: "Ressource occupée.",
    599: "Tâche d'opération de fichier inexistante.",
    1002: "Impossible d'accéder au dossier (permissions insuffisantes).",
    1100: "Opération impossible.",
}

#: Codes File Station traduisant un refus d'accès (par opposition à 408 = absent).
#: DSM ne distingue pas toujours « interdit » de « introuvable » : les deux cas
#: aboutissent de toute façon à un refus, la stratégie étant *fail-closed*.
FILESTATION_DENIED = frozenset({403, 404, 405, 407})

#: Rétrocompatibilité : table agrégée utilisée par défaut.
DSM_ERRORS: dict[int, str] = {**DSM_COMMON_ERRORS, **DSM_AUTH_ERRORS}


class DSMError(RuntimeError):
    """Erreur renvoyée par l'API DSM.

    Le même numéro pouvant désigner deux choses différentes selon l'API appelée
    (402 = « permission refusée » sous Auth mais « système occupé » sous File
    Station), le message est résolu à partir de l'API d'origine.
    """

    def __init__(
        self,
        code: int,
        message: str | None = None,
        api: str = "",
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.code = code
        self.api = api
        self.errors = errors or []
        self.message = message or self._describe(code, api)
        super().__init__(f"[{code}] {self.message}")

    @staticmethod
    def _describe(code: int, api: str) -> str:
        if code in DSM_COMMON_ERRORS:
            return DSM_COMMON_ERRORS[code]
        table = FILESTATION_ERRORS if api.startswith("SYNO.FileStation") else DSM_AUTH_ERRORS
        return table.get(code) or f"Erreur DSM {code}"

    @property
    def is_filestation(self) -> bool:
        return self.api.startswith("SYNO.FileStation")

    @property
    def needs_otp(self) -> bool:
        return not self.is_filestation and self.code in (403, 404, 406)

    @property
    def is_auth_failure(self) -> bool:
        return not self.is_filestation and self.code in (400, 401, 402, 405, 407, 408, 409, 410, 411)

    @property
    def is_session_expired(self) -> bool:
        """Codes imposant une reconnexion (150 : IP de sortie du conteneur modifiée)."""
        return self.code in (105, 106, 107, 119, 150)

    @property
    def is_app_privilege(self) -> bool:
        """Le compte n'a pas accès à l'application File Station."""
        return self.code == 160

    @property
    def is_permission_denied(self) -> bool:
        return self.is_filestation and self.code in FILESTATION_DENIED


class DSMClient:
    """Client HTTP minimal mais complet pour l'API Web DSM."""

    def __init__(
        self,
        base_url: str,
        *,
        verify_ssl: bool = False,
        timeout: float = 20.0,
        session_name: str = "FileStation",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_name = session_name
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "syno-ia"},
        )
        self._api_info: dict[str, dict[str, Any]] = {}
        self._info_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    # ------------------------------------------------------------ bas niveau
    async def _discover(self) -> dict[str, dict[str, Any]]:
        """Interroge SYNO.API.Info pour connaître le chemin et la version de chaque API."""
        if self._api_info:
            return self._api_info
        async with self._info_lock:
            if self._api_info:
                return self._api_info
            try:
                response = await self._client.get(
                    "/webapi/query.cgi",
                    params={
                        "api": "SYNO.API.Info",
                        "version": "1",
                        "method": "query",
                        "query": "SYNO.API.Auth,SYNO.FileStation.",
                    },
                )
                payload = response.json()
                if payload.get("success"):
                    self._api_info = payload.get("data", {}) or {}
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("Découverte des API DSM impossible (%s), valeurs par défaut", exc)
            if not self._api_info:
                self._api_info = {}
        return self._api_info

    async def _endpoint(self, api: str, fallback_version: int) -> tuple[str, int]:
        """Retourne (chemin CGI, version max) pour une API donnée."""
        info = (await self._discover()).get(api) or {}
        path = info.get("path") or "entry.cgi"
        max_version = int(info.get("maxVersion") or fallback_version)
        return f"/webapi/{path}", min(max_version, fallback_version)

    @staticmethod
    def _encode(params: dict[str, Any]) -> dict[str, str]:
        """Sérialise les paramètres ; les listes/dicts sont encodés en JSON comme l'exige DSM."""
        encoded: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                encoded[key] = "true" if value else "false"
            elif isinstance(value, (list, tuple, dict)):
                encoded[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                encoded[key] = str(value)
        return encoded

    async def request(
        self,
        api: str,
        method: str,
        *,
        version: int,
        sid: str | None = None,
        use_post: bool = False,
        **params: Any,
    ) -> Any:
        """Effectue un appel API et retourne le champ `data` en cas de succès."""
        path, resolved_version = await self._endpoint(api, version)
        payload = self._encode(
            {"api": api, "version": resolved_version, "method": method, **params}
        )
        if sid:
            payload["_sid"] = sid

        try:
            if use_post:
                response = await self._client.post(path, data=payload)
            else:
                response = await self._client.get(path, params=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise DSMError(100, f"NAS injoignable : {exc}", api=api) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise DSMError(100, "Réponse DSM illisible (URL DSM incorrecte ?)", api=api) from exc

        if not body.get("success"):
            error = body.get("error") or {}
            raise DSMError(
                int(error.get("code", 100)),
                api=api,
                errors=[e for e in (error.get("errors") or []) if isinstance(e, dict)],
            )
        return body.get("data", {})

    # --------------------------------------------------------- authentification
    async def login(
        self, account: str, password: str, otp_code: str | None = None
    ) -> DSMSession:
        """Authentifie un utilisateur DSM et retourne sa session."""
        data = await self.request(
            "SYNO.API.Auth",
            "login",
            version=6,
            use_post=True,
            account=account,
            passwd=password,
            session=self.session_name,
            format="sid",
            otp_code=otp_code or None,
        )
        sid = data.get("sid")
        if not sid:
            raise DSMError(400, api="SYNO.API.Auth")
        # DSM 6 renvoie « did », DSM 7 peut renvoyer « device_id » pour la même notion.
        device_id = data.get("did") or data.get("device_id") or ""
        session = DSMSession(sid=sid, account=account, device_id=device_id)
        session.is_admin = await self.is_administrator(sid)
        return session

    async def logout(self, sid: str) -> None:
        try:
            await self.request(
                "SYNO.API.Auth", "logout", version=6, sid=sid, session=self.session_name
            )
        except DSMError as exc:  # une déconnexion ne doit jamais faire échouer l'appelant
            logger.debug("Déconnexion DSM ignorée : %s", exc)

    async def is_administrator(self, sid: str) -> bool:
        """`SYNO.FileStation.Info` expose `is_manager` (appartenance au groupe administrateurs)."""
        try:
            data = await self.request("SYNO.FileStation.Info", "get", version=2, sid=sid)
        except DSMError as exc:
            logger.debug("Statut administrateur indéterminé : %s", exc)
            return False
        return bool(data.get("is_manager", False))

    async def validate(self, sid: str) -> bool:
        """Vérifie qu'un `sid` est toujours valide côté DSM."""
        try:
            await self.request("SYNO.FileStation.Info", "get", version=2, sid=sid)
            return True
        except DSMError:
            return False

    # ------------------------------------------------------------- File Station
    async def list_shares(self, sid: str) -> list[ShareInfo]:
        """Partages visibles par le détenteur du `sid` (premier niveau de filtrage ACL)."""
        data = await self.request(
            "SYNO.FileStation.List",
            "list_share",
            version=2,
            sid=sid,
            offset=0,
            limit=1000,
            additional=["real_path", "perm", "mount_point_type"],
        )
        shares: list[ShareInfo] = []
        for entry in data.get("shares", []) or []:
            additional = entry.get("additional") or {}
            perm = additional.get("perm") if isinstance(additional.get("perm"), dict) else {}
            acl = perm.get("acl") if isinstance(perm.get("acl"), dict) else {}
            adv = perm.get("adv_right") if isinstance(perm.get("adv_right"), dict) else {}
            # `adv_right` n'est renseigné que pour les comptes non administrateurs.
            readable = bool(acl.get("read", True)) and not adv.get("disable_list", False)
            shares.append(
                ShareInfo(
                    path=entry.get("path", ""),
                    name=entry.get("name", ""),
                    real_path=additional.get("real_path", "") or "",
                    readable=readable,
                    writable=bool(acl.get("write", False)),
                )
            )
        return [share for share in shares if share.path]

    async def list_folder(
        self, sid: str, folder_path: str, *, offset: int = 0, limit: int = 500
    ) -> list[RemoteFile]:
        data = await self.request(
            "SYNO.FileStation.List",
            "list",
            version=2,
            sid=sid,
            folder_path=folder_path,
            offset=offset,
            limit=limit,
            additional=["size", "time", "type", "perm"],
        )
        return [_to_remote_file(entry) for entry in data.get("files", []) or []]

    async def walk(
        self, sid: str, folder_path: str, *, max_depth: int = 12
    ) -> AsyncIterator[RemoteFile]:
        """Parcours récursif d'un dossier via File Station (mode sans montage)."""
        stack: list[tuple[str, int]] = [(folder_path, 0)]
        while stack:
            current, depth = stack.pop()
            offset = 0
            while True:
                try:
                    entries = await self.list_folder(sid, current, offset=offset, limit=500)
                except DSMError as exc:
                    logger.warning("Dossier ignoré %s : %s", current, exc)
                    break
                for entry in entries:
                    if entry.is_dir:
                        if depth < max_depth:
                            stack.append((entry.path, depth + 1))
                    else:
                        yield entry
                if len(entries) < 500:
                    break
                offset += len(entries)

    async def stat_paths(self, sid: str, paths: Iterable[str]) -> dict[str, bool]:
        """Teste l'accès en lecture à plusieurs chemins DSM avec le `sid` fourni.

        Retourne `{chemin: accessible}`. La stratégie est *fail-closed* : tout
        chemin absent de la réponse, marqué en erreur ou non résolu est refusé.

        DSM ne documente pas le comportement de `getinfo` en cas d'échec partiel
        et le schéma `data`/`error` est exclusif : un seul chemin fautif (fichier
        supprimé depuis l'indexation, par exemple) fait vraisemblablement échouer
        tout le lot. On retombe donc sur des appels unitaires afin qu'un chemin
        obsolète ne masque pas les résultats légitimes de l'utilisateur.
        """
        wanted = [p for p in dict.fromkeys(paths) if p]
        if not wanted:
            return {}

        try:
            return self._read_getinfo(
                await self._getinfo(sid, wanted),
                wanted,
            )
        except DSMError as exc:
            if exc.is_session_expired or exc.is_app_privilege:
                raise
            if len(wanted) == 1:
                logger.debug("Accès refusé à %s : %s", wanted[0], exc)
                return {wanted[0]: False}
            logger.info(
                "getinfo groupé en échec pour %d chemin(s) (%s), reprise chemin par chemin",
                len(wanted),
                exc,
            )

        results = await asyncio.gather(
            *(self.stat_paths(sid, [path]) for path in wanted), return_exceptions=True
        )
        merged = dict.fromkeys(wanted, False)
        for path, outcome in zip(wanted, results, strict=True):
            if isinstance(outcome, DSMError) and (
                outcome.is_session_expired or outcome.is_app_privilege
            ):
                raise outcome
            if isinstance(outcome, dict):
                merged[path] = outcome.get(path, False)
        return merged

    async def _getinfo(self, sid: str, paths: list[str]) -> Any:
        return await self.request(
            "SYNO.FileStation.List",
            "getinfo",
            version=2,
            sid=sid,
            use_post=True,
            path=paths,
            additional=["real_path", "perm", "size", "time"],
        )

    @staticmethod
    def _read_getinfo(data: Any, wanted: list[str]) -> dict[str, bool]:
        result = dict.fromkeys(wanted, False)
        by_path = {path.lower(): path for path in wanted}
        for entry in (data or {}).get("files", []) or []:
            key = by_path.get(str(entry.get("path", "")).lower())
            if key is None:
                continue
            # DSM signale un échec par chemin via un champ « code » sur l'entrée.
            if entry.get("code"):
                continue
            # Une entrée sans nom correspond à un chemin non résolu.
            if not entry.get("name"):
                continue
            perm = (entry.get("additional") or {}).get("perm")
            if isinstance(perm, dict):
                acl = perm.get("acl")
                if isinstance(acl, dict) and acl.get("read") is False:
                    continue
                adv = perm.get("adv_right")
                if isinstance(adv, dict) and adv.get("disable_list"):
                    continue
            result[key] = True
        return result

    async def download(self, sid: str, path: str) -> bytes:
        """Télécharge un fichier via File Station (mode sans montage)."""
        endpoint, version = await self._endpoint("SYNO.FileStation.Download", 2)
        params = self._encode(
            {
                "api": "SYNO.FileStation.Download",
                "version": version,
                "method": "download",
                "path": [path],
                "mode": "download",
                "_sid": sid,
            }
        )
        response = await self._client.get(endpoint, params=params)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            body = response.json()
            if not body.get("success"):
                error = body.get("error") or {}
                raise DSMError(int(error.get("code", 100)), api="SYNO.FileStation.Download")
        return response.content


def _to_remote_file(entry: dict[str, Any]) -> RemoteFile:
    additional = entry.get("additional") or {}
    time_info = additional.get("time") or {}
    return RemoteFile(
        path=entry.get("path", ""),
        name=entry.get("name", ""),
        is_dir=bool(entry.get("isdir")),
        size=int(additional.get("size") or 0),
        mtime=int(time_info.get("mtime") or 0),
        extra=additional,
    )
