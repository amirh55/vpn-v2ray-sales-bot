from __future__ import annotations

import json
from typing import Any

import httpx

from sales.models import XUIPanel


class XUIError(RuntimeError):
    pass


class XUIClient:
    """Small 3x-ui API client.

    3x-ui API has changed between the old 2.x inbound-scoped API and the
    newer 3.x client-scoped API. 3.x creates a global client first and then
    attaches it to one or more inbounds, while 2.x commonly uses
    /inbounds/addClient with {id, settings}. This client supports both.
    """

    def __init__(self, panel: XUIPanel):
        self.panel = panel
        self.base_url = panel.base_url.rstrip('/')
        self.api_base_path = '/' + panel.api_base_path.strip('/')
        self.timeout = panel.timeout_seconds or 20
        self.headers = {
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        }
        if panel.api_token:
            self.headers['Authorization'] = f'Bearer {panel.api_token}'

    def _url(self, path: str) -> str:
        if path.startswith('http'):
            return path
        if not path.startswith('/'):
            path = '/' + path
        return f'{self.base_url}{path}'

    def _api_url(self, path: str) -> str:
        if not path.startswith('/'):
            path = '/' + path
        return f'{self.base_url}{self.api_base_path}{path}'

    def _schema_url(self, path: str) -> str:
        """Turn a path out of the OpenAPI document into a full URL.

        Most panels list paths from the panel root ("/panel/api/clients/add"),
        but some list them relative to the API base ("/clients/add"), which
        needs that base put back.
        """
        if path.startswith('http'):
            return path
        if not path.startswith('/'):
            path = '/' + path
        if path.startswith(self.api_base_path):
            return self._url(path)
        return self._api_url(path)

    def _panel_url(self, path: str) -> str:
        """Build non-OpenAPI panel route for older 3x-ui/x-ui builds."""
        if not path.startswith('/'):
            path = '/' + path
        panel_path = self.api_base_path
        if panel_path.endswith('/api'):
            panel_path = panel_path[:-4] or '/panel'
        return f'{self.base_url}{panel_path}{path}'

    def request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        headers = dict(self.headers)
        headers.update(kwargs.pop('headers', {}) or {})
        try:
            with httpx.Client(timeout=self.timeout, verify=self.panel.verify_ssl) as client:
                response = client.request(method, url, headers=headers, **kwargs)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = (response.text or '').strip().replace('\n', ' ')
                    if len(body) > 500:
                        body = body[:500] + '…'
                    raise XUIError(
                        f'خطا در ارتباط با 3x-ui: HTTP {response.status_code} برای {method.upper()} {url}'
                        + (f' | body={body}' if body else '')
                    ) from exc
                text = (response.text or '').strip()
                if not text:
                    return {'success': True}
                try:
                    return response.json()
                except Exception:
                    return {'success': True, 'raw': text}
        except XUIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise XUIError(f'خطا در ارتباط با 3x-ui: {exc}') from exc

    @staticmethod
    def _ok(result: dict[str, Any]) -> bool:
        return result.get('success') is not False

    # The secret that goes in a share link. 3x-ui 3.x names it `uuid` for
    # vless/vmess, `password` for trojan/shadowsocks and `auth` for hysteria.
    # `id` is checked last and only when it looks like a secret, because on this
    # generation of the panel `id` is the client's numeric database row — using
    # it produced config links with a row number where the UUID belonged.
    SECRET_KEYS = ('uuid', 'password', 'auth', 'clientId', 'client_id', 'id')

    @staticmethod
    def _looks_like_secret(value: Any) -> bool:
        text = str(value or '').strip()
        if len(text) < 8:
            return False
        # A bare integer is a row id, never a share-link secret.
        return not text.lstrip('-').isdigit()

    @staticmethod
    def _extract_uuid(obj: Any) -> str:
        if isinstance(obj, dict):
            for key in XUIClient.SECRET_KEYS:
                val = obj.get(key)
                if val and XUIClient._looks_like_secret(val):
                    return str(val).strip()
            for key in ('obj', 'data', 'client', 'result'):
                val = XUIClient._extract_uuid(obj.get(key))
                if val:
                    return val
            clients = obj.get('clients')
            if isinstance(clients, list) and clients:
                val = XUIClient._extract_uuid(clients[0])
                if val:
                    return val
        if isinstance(obj, list) and obj:
            for item in obj:
                val = XUIClient._extract_uuid(item)
                if val:
                    return val
        return ''

    @staticmethod
    def _as_links(result: Any) -> list[str]:
        """Pull share links out of whatever wrapper the panel used."""
        if isinstance(result, dict):
            for key in ('obj', 'data', 'result', 'links'):
                if key in result:
                    return XUIClient._as_links(result[key])
            return []
        if isinstance(result, str):
            result = [result]
        if not isinstance(result, list):
            return []

        links = []
        for item in result:
            if isinstance(item, dict):
                # Some builds return [{remark, link}] instead of bare strings.
                item = item.get('link') or item.get('url') or item.get('uri') or ''
            text = str(item or '').strip()
            if '://' in text and text not in links:
                links.append(text)
        return links

    def get_client_links(self, email: str) -> list[str]:
        """Every share link the panel itself would copy for this client.

        Asking the panel is the only way to get a link that is correct for the
        inbound's actual protocol, TLS and Reality settings. Building one from a
        template means duplicating logic the panel already has, and getting it
        wrong the moment an inbound is reconfigured.
        """
        last_error = None
        for path in (f'/clients/links/{email}', f'/client/links/{email}'):
            try:
                links = self._as_links(self.request('GET', self._api_url(path)))
            except XUIError as exc:
                last_error = exc
                continue
            if links:
                return links
        if last_error is not None:
            raise XUIError(str(last_error))
        return []

    def get_client_sub_id(self, email: str) -> str:
        """The Subscription ID the panel actually gave this client.

        Provisioning asks for subId to equal the client's name, but the panel is
        free to keep its own. The subscription URL is keyed on whatever the
        panel decided, so it is read back instead of assumed — guessing wrong
        hands the customer a subscription link that resolves to nothing.
        """
        try:
            result = self.get_client(email)
        except XUIError:
            return ''

        def find(node, depth=0):
            if depth > 4:
                return ''
            if isinstance(node, dict):
                for key in ('subId', 'sub_id', 'subID'):
                    value = str(node.get(key) or '').strip()
                    if value:
                        return value
                for value in node.values():
                    found = find(value, depth + 1)
                    if found:
                        return found
            if isinstance(node, list):
                for item in node:
                    found = find(item, depth + 1)
                    if found:
                        return found
            return ''

        return find(result)

    def get_sub_links(self, sub_id: str) -> list[str]:
        """Every link behind one subscription id."""
        if not sub_id:
            return []
        try:
            return self._as_links(self.request('GET', self._api_url(f'/clients/subLinks/{sub_id}')))
        except XUIError:
            return []

    def get_openapi(self) -> dict[str, Any]:
        """Fetch the panel's own API description.

        Newer 3x-ui builds publish one, and it is the only way to know what this
        exact build expects without guessing. Several locations are tried
        because forks moved the file around.
        """
        last_error = None
        for url in (
            self._api_url('/openapi.json'),
            self._panel_url('/openapi.json'),
            self._url('/openapi.json'),
            self._api_url('/swagger/doc.json'),
        ):
            try:
                result = self.request('GET', url)
            except XUIError as exc:
                last_error = exc
                continue
            if isinstance(result, dict) and result.get('paths'):
                result['_source_url'] = url
                return result
            last_error = XUIError(f'{url} پاسخ داد اما ساختار OpenAPI نداشت.')
        raise XUIError(f'فایل openapi.json در این پنل پیدا نشد: {last_error}')

    # --- Schema-driven client creation ------------------------------------
    #
    # Without a schema this client has to try each known endpoint and payload
    # shape until one works, which costs a round trip per failed guess and
    # breaks whenever a new 3x-ui version appears. With the schema in hand the
    # right endpoint is known up front and the payload is built from the panel's
    # own field list. The guessing path stays as the fallback for older builds
    # that publish no schema.

    ADD_CLIENT_HINTS = ('client', 'user')

    @staticmethod
    def _schema_paths(schema: dict[str, Any]) -> dict[str, Any]:
        paths = schema.get('paths')
        return paths if isinstance(paths, dict) else {}

    @classmethod
    def find_add_client_path(cls, schema: dict[str, Any]) -> str:
        """Pick the endpoint in the schema that creates a client.

        Ranked rather than filtered, because a panel may expose several
        candidates (`/clients/add`, `/clients/bulkCreate`, `/inbounds/addClient`)
        and the plain single-client one is always the right first choice.
        """
        best = ''
        best_score = -1
        for raw_path, methods in cls._schema_paths(schema).items():
            if not isinstance(methods, dict) or 'post' not in methods:
                continue
            lowered = str(raw_path).lower()
            if '{' in lowered:
                continue
            if not any(hint in lowered for hint in cls.ADD_CLIENT_HINTS):
                continue

            score = -1
            if lowered.endswith('/clients/add'):
                score = 100
            elif lowered.endswith('/addclient'):
                score = 90
            elif lowered.endswith('/clients'):
                score = 70
            elif lowered.endswith('/client/add'):
                score = 80
            elif 'bulkcreate' in lowered or 'bulk' in lowered:
                score = 40
            if score > best_score:
                best_score, best = score, str(raw_path)
        return best

    def _schema_body_properties(self, schema: dict[str, Any], path: str) -> dict[str, Any]:
        """Resolve the POST body schema of one path into a property map."""
        operation = self._schema_paths(schema).get(path, {}).get('post', {})
        body = operation.get('requestBody') or {}
        content = body.get('content') or {}
        media = content.get('application/json') or next(iter(content.values()), {})
        resolved = self._resolve_ref(schema, media.get('schema') or {})
        props = resolved.get('properties')
        return props if isinstance(props, dict) else {}

    @classmethod
    def _resolve_ref(cls, schema: dict[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
        """Follow a local $ref. Depth-capped so a self-referencing schema cannot loop."""
        if not isinstance(node, dict) or depth > 8:
            return node if isinstance(node, dict) else {}
        ref = node.get('$ref')
        if not isinstance(ref, str) or not ref.startswith('#/'):
            return node
        target: Any = schema
        for part in ref[2:].split('/'):
            if not isinstance(target, dict):
                return {}
            target = target.get(part.replace('~1', '/').replace('~0', '~'))
            if target is None:
                return {}
        return cls._resolve_ref(schema, target, depth + 1)

    def _payload_from_schema(
        self,
        schema: dict[str, Any],
        path: str,
        inbound_ids,
        client_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Shape the request body to match what this panel's schema declares.

        Returns None when the schema describes something this code does not
        recognise, so the caller falls back to the known shapes rather than
        posting a body built on a wrong guess.
        """
        props = self._schema_body_properties(schema, path)
        if not props:
            return None

        keys = {str(k): k for k in props}
        lowered = {k.lower(): original for k, original in keys.items()}

        client_key = lowered.get('client')
        inbound_key = (
            lowered.get('inboundids')
            or lowered.get('inbound_ids')
            or lowered.get('inbounds')
            or lowered.get('inboundid')
            or lowered.get('inbound_id')
            or lowered.get('id')
        )

        if client_key:
            # 3.x shape: {"client": {...}, "inboundIds": [n]}
            client = self._client_v3_payloads(inbound_ids, client_payload)[0]['client']
            body: dict[str, Any] = {keys[client_key]: client}
            if inbound_key:
                target = keys[inbound_key]
                declared = self._resolve_ref(schema, props.get(target) or {})
                ids = self._as_inbound_list(inbound_ids)
                # A schema that wants a single id can only be given one, so the
                # rest are attached separately after the client exists.
                body[target] = ids if str(declared.get('type')) == 'array' else (ids[0] if ids else 0)
            return body

        if 'settings' in lowered:
            # 2.x shape: {"id": inbound, "settings": "<json string>"}
            settings_json = json.dumps(
                {'clients': [client_payload]}, ensure_ascii=False, separators=(',', ':')
            )
            body = {keys[lowered['settings']]: settings_json}
            if inbound_key:
                ids = self._as_inbound_list(inbound_ids)
                body[keys[inbound_key]] = ids[0] if ids else 0
            return body

        return None

    def load_schema(self, *, refresh: bool = False) -> dict[str, Any]:
        """The cached schema, fetched from the panel the first time it is needed.

        Cached on the panel row rather than in memory so every process — the
        bot, the web workers, a management command — shares one copy and the
        panel is not re-queried on each sale. A panel that turned out to have no
        schema is remembered as such by the fetch timestamp, so it is not probed
        again on every order.
        """
        if refresh:
            return self.refresh_schema()
        cached = self.panel.openapi_schema
        if isinstance(cached, dict) and cached:
            return cached
        if self.panel.openapi_fetched_at is None:
            try:
                return self.refresh_schema()
            except XUIError:
                return {}
        return {}

    def refresh_schema(self) -> dict[str, Any]:
        """Re-read the schema from the panel and remember what it says.

        The failure note is stored too, so the panel page can explain why a
        panel is still running on the fallback path.
        """
        from django.utils import timezone

        try:
            schema = self.get_openapi()
        except XUIError as exc:
            self.panel.openapi_note = str(exc)[:300]
            self.panel.openapi_fetched_at = timezone.now()
            self.panel.save(update_fields=['openapi_note', 'openapi_fetched_at', 'updated_at'])
            raise

        path = self.find_add_client_path(schema)
        self.panel.openapi_schema = schema
        self.panel.openapi_add_client_path = path
        self.panel.openapi_fetched_at = timezone.now()
        self.panel.openapi_note = (
            f'خوانده شد. مسیر ساخت کلاینت: {path}' if path
            else 'خوانده شد، اما مسیر ساخت کلاینت در آن پیدا نشد.'
        )
        self.panel.save(update_fields=[
            'openapi_schema', 'openapi_add_client_path', 'openapi_fetched_at', 'openapi_note', 'updated_at',
        ])
        return schema

    def _add_client_from_schema(self, inbound_ids, client_payload: dict[str, Any]) -> dict[str, Any]:
        """Create the client using the endpoint and body the panel documents."""
        schema = self.load_schema()
        if not schema:
            raise XUIError('ساختار API این پنل خوانده نشده است.')
        path = self.panel.openapi_add_client_path or self.find_add_client_path(schema)
        if not path:
            raise XUIError('مسیر ساخت کلاینت در ساختار API این پنل پیدا نشد.')

        body = self._payload_from_schema(schema, path, inbound_ids, client_payload)
        if body is None:
            raise XUIError(f'ساختار بدنه‌ی {path} شناخته‌شده نیست.')

        email = str(client_payload['email']).strip()
        result = self.request(
            'POST', self._schema_url(path), json=body, headers={'Content-Type': 'application/json'}
        )
        if not self._ok(result):
            raise XUIError(f'POST {path}: {result}')

        try:
            uuid_value = self._extract_uuid(self.get_client(email))
        except XUIError:
            uuid_value = self._extract_uuid(result)
        if uuid_value:
            result['client_uuid'] = uuid_value
        result['client_email'] = email
        result['api_mode'] = f'openapi:{path}'
        return result

    def list_inbounds(self) -> dict[str, Any]:
        last_error = None
        for path in ('/inbounds/list', '/inbounds', '/inbounds/'):
            try:
                return self.request('GET', self._api_url(path))
            except XUIError as exc:
                last_error = exc
        raise XUIError(str(last_error))

    def get_client(self, email: str) -> dict[str, Any]:
        last_error = None
        for path in (f'/clients/get/{email}', f'/client/get/{email}', f'/clients/{email}'):
            try:
                result = self.request('GET', self._api_url(path))
                if self._ok(result):
                    return result
                last_error = XUIError(str(result))
            except XUIError as exc:
                last_error = exc
        raise XUIError(str(last_error))

    @staticmethod
    def _as_list(value: Any) -> list[dict[str, Any]]:
        """Unwrap the several shapes 3x-ui uses for list payloads."""
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for key in ('obj', 'data', 'result', 'inbounds'):
                inner = value.get(key)
                if inner is not None and inner is not value:
                    found = XUIClient._as_list(inner)
                    if found:
                        return found
        return []

    @staticmethod
    def _inbound_clients(inbound: dict[str, Any]) -> list[dict[str, Any]]:
        """Read the client list out of an inbound's settings.

        3x-ui stores settings as a JSON *string* on the inbound row, though
        some builds return it already decoded.
        """
        settings = inbound.get('settings')
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except ValueError:
                return []
        if not isinstance(settings, dict):
            return []
        clients = settings.get('clients')
        return [c for c in clients if isinstance(c, dict)] if isinstance(clients, list) else []

    @staticmethod
    def _client_identifier(client: dict[str, Any]) -> str:
        """The value that also appears in the user's share link.

        vless/vmess use `id`; trojan and shadowsocks use `password`.
        """
        for key in ('id', 'password'):
            value = str(client.get(key) or '').strip()
            if value:
                return value
        return ''

    def find_client_by_identifier(self, identifier: str) -> dict[str, Any] | None:
        """Locate a client across every inbound by its link identifier.

        Returns the client row, its inbound and live traffic counters, or None
        when this panel does not hold that config.
        """
        wanted = (identifier or '').strip().lower()
        if not wanted:
            return None

        inbounds = self._as_list(self.list_inbounds())
        for inbound in inbounds:
            for client in self._inbound_clients(inbound):
                if self._client_identifier(client).lower() != wanted:
                    continue

                email = str(client.get('email') or '').strip()
                stats = {}
                for row in inbound.get('clientStats') or []:
                    if isinstance(row, dict) and str(row.get('email') or '').strip() == email:
                        stats = row
                        break

                # Limits live on the client row, usage on the stats row, but
                # some builds duplicate both. Prefer whichever is populated.
                total = int(stats.get('total') or client.get('totalGB') or 0)
                expiry = int(stats.get('expiryTime') or client.get('expiryTime') or 0)
                return {
                    'inbound_id': int(inbound.get('id') or 0),
                    'inbound_remark': str(inbound.get('remark') or ''),
                    'email': email,
                    'identifier': self._client_identifier(client),
                    'up': int(stats.get('up') or 0),
                    'down': int(stats.get('down') or 0),
                    'total': total,
                    'expiry_time': expiry,
                    'enable': bool(stats.get('enable', client.get('enable', True))),
                }
        return None

    @staticmethod
    def _as_inbound_list(inbound_ids) -> list[int]:
        """Accept one inbound or several, always work with a list."""
        if isinstance(inbound_ids, (list, tuple, set)):
            values = list(inbound_ids)
        else:
            values = [inbound_ids]
        found = []
        for value in values:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in found:
                found.append(number)
        return found

    def _client_v3_payloads(self, inbound_ids, client_payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Return payloads for current 3x-ui 3.x Clients API.

        In 3x-ui v3.3.x the router is POST /panel/api/clients/add and the
        controller binds JSON into service.ClientCreatePayload, whose shape is
        {"client": model.Client, "inboundIds": []int}. Sending `email` at
        top-level makes the panel bind an empty Client and then it returns
        `client email is required`.
        """
        email = str(client_payload['email']).strip()
        total_gb = int(client_payload.get('totalGB') or 0)
        expiry_time = int(client_payload.get('expiryTime') or 0)
        limit_ip = int(client_payload.get('limitIp') or 0)
        reset = int(client_payload.get('reset') or 0)
        raw_tg_id = client_payload.get('tgId', 0)
        try:
            tg_id = int(raw_tg_id or 0)
        except (TypeError, ValueError):
            tg_id = 0
        sub_id = str(client_payload.get('subId') or email).strip() or email
        enable = bool(client_payload.get('enable', True))
        flow = str(client_payload.get('flow') or '')

        client = {
            'email': email,
            'subId': sub_id,
            'enable': enable,
            'totalGB': total_gb,
            'expiryTime': expiry_time,
            'limitIp': limit_ip,
            # 3x-ui Go model expects tgId as int64. Sending it as a string
            # makes clients/add fail with: cannot unmarshal string into ... int64.
            'tgId': tg_id,
            'reset': reset,
            'flow': flow,
            'comment': str(client_payload.get('comment') or ''),
            'groupName': str(client_payload.get('groupName') or ''),
        }

        # Current official v3 shape. The extra variants are defensive for forks
        # that keep Go field casing or accept a supplied UUID.
        supplied_id = str(client_payload.get('id') or '').strip()
        client_with_id = dict(client)
        if supplied_id:
            client_with_id['id'] = supplied_id

        ids = self._as_inbound_list(inbound_ids)
        return [
            {'client': client, 'inboundIds': ids},
            {'client': client_with_id, 'inboundIds': ids},
            {'Client': client, 'InboundIds': ids},
            {'client': client, 'inbounds': ids},
        ]

    def _attach_client_to_inbound(self, email: str, inbound_id: int) -> dict[str, Any]:
        payloads = [
            {'inboundIds': [int(inbound_id)]},
            {'inbounds': [int(inbound_id)]},
            {'ids': [int(inbound_id)]},
            {'inbound_id': int(inbound_id)},
            {'email': email, 'inboundIds': [int(inbound_id)]},
            {'emails': [email], 'inboundIds': [int(inbound_id)]},
        ]
        paths = [
            f'/clients/{email}/attach',
            f'/client/{email}/attach',
            f'/clients/attach/{email}',
            f'/client/attach/{email}',
            '/clients/bulkAttach',
            '/client/bulkAttach',
        ]
        last_error = None
        for path in paths:
            for payload in payloads:
                try:
                    result = self.request('POST', self._api_url(path), json=payload, headers={'Content-Type': 'application/json'})
                    if self._ok(result):
                        return result
                    last_error = XUIError(str(result))
                except XUIError as exc:
                    last_error = exc
        raise XUIError(f'اتصال کلاینت به inbound ناموفق بود: {last_error}')

    def _add_client_v3(self, inbound_ids, client_payload: dict[str, Any]) -> dict[str, Any]:
        email = str(client_payload['email']).strip()

        # The API Docs exported from the target panel expose POST /panel/api/clients/add.
        # There is no documented POST /panel/api/clients route, so do not call it here.
        # The previous fallback to /clients made the final error misleading and could
        # hide the real /clients/add response.
        primary_payloads = self._client_v3_payloads(inbound_ids, client_payload)
        attempts: list[str] = []

        for payload in primary_payloads:
            url = self._api_url('/clients/add')
            try:
                result = self.request('POST', url, json=payload, headers={'Content-Type': 'application/json'})
                if self._ok(result):
                    try:
                        got = self.get_client(email)
                        uuid_value = self._extract_uuid(got)
                    except XUIError:
                        uuid_value = self._extract_uuid(result)
                    if uuid_value:
                        result['client_uuid'] = uuid_value
                    result['client_email'] = email
                    result['api_mode'] = 'clients-v3-add'
                    return result
                attempts.append(f'POST {url} payload_keys={list(payload.keys())}: {result}')
            except XUIError as exc:
                attempts.append(f'POST {url} payload_keys={list(payload.keys())}: {exc}')

        # Some newer/forked panels also expose /clients/bulkCreate. Try it as a
        # documented backup with array and object wrappers.
        single = primary_payloads[0]
        bulk_payloads = [
            [single],
            {'items': [single]},
            {'clients': [single]},
            {'data': [single]},
        ]
        bulk_url = self._api_url('/clients/bulkCreate')
        for payload in bulk_payloads:
            try:
                result = self.request('POST', bulk_url, json=payload, headers={'Content-Type': 'application/json'})
                if self._ok(result):
                    try:
                        got = self.get_client(email)
                        uuid_value = self._extract_uuid(got)
                    except XUIError:
                        uuid_value = self._extract_uuid(result)
                    if uuid_value:
                        result['client_uuid'] = uuid_value
                    result['client_email'] = email
                    result['api_mode'] = 'clients-v3-bulkCreate'
                    return result
                attempts.append(f'POST {bulk_url} type={type(payload).__name__}: {result}')
            except XUIError as exc:
                attempts.append(f'POST {bulk_url} type={type(payload).__name__}: {exc}')

        raise XUIError(' | '.join(attempts[-10:]))

    def _add_client_legacy(self, inbound_ids, client_payload: dict[str, Any]) -> dict[str, Any]:
        settings_obj = {'clients': [client_payload]}
        settings_json = json.dumps(settings_obj, ensure_ascii=False, separators=(',', ':'))
        ids = self._as_inbound_list(inbound_ids)
        # The 2.x API is inbound-scoped and can only take one at a time; the
        # caller attaches the remaining inbounds afterwards.
        legacy_body = {'id': ids[0] if ids else 0, 'settings': settings_json}
        attempts = [
            ('POST', self._api_url('/inbounds/addClient'), {'json': legacy_body, 'headers': {'Content-Type': 'application/json'}}),
            ('POST', self._api_url('/inbound/addClient'), {'json': legacy_body, 'headers': {'Content-Type': 'application/json'}}),
            ('POST', self._api_url('/inbounds/addClient'), {'data': legacy_body, 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}),
            ('POST', self._api_url('/inbound/addClient'), {'data': legacy_body, 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}),
            ('POST', self._panel_url('/inbounds/addClient'), {'data': legacy_body, 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}),
            ('POST', self._panel_url('/inbound/addClient'), {'data': legacy_body, 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}),
        ]
        last_error = None
        for method, url, kwargs in attempts:
            try:
                result = self.request(method, url, **kwargs)
                if self._ok(result):
                    result['client_email'] = client_payload.get('email')
                    result['api_mode'] = 'inbound-legacy'
                    return result
                last_error = XUIError(str(result))
            except XUIError as exc:
                last_error = exc
        raise XUIError(str(last_error))

    def add_client(self, inbound_ids, client_payload: dict[str, Any]) -> dict[str, Any]:
        email = str(client_payload.get('email') or '').strip()
        if not email:
            raise XUIError('client email برای ساخت کلاینت در 3x-ui خالی است.')
        client_payload = dict(client_payload)
        client_payload['email'] = email
        client_payload.setdefault('subId', email)
        client_payload.setdefault('enable', True)
        client_payload.setdefault('alterId', 0)
        client_payload.setdefault('flow', '')
        client_payload.setdefault('reset', 0)
        client_payload.setdefault('up', 0)
        client_payload.setdefault('down', 0)
        for int_key in ('totalGB', 'expiryTime', 'limitIp', 'reset', 'up', 'down', 'tgId'):
            try:
                client_payload[int_key] = int(client_payload.get(int_key) or 0)
            except (TypeError, ValueError):
                client_payload[int_key] = 0

        ids = self._as_inbound_list(inbound_ids)
        if not ids:
            raise XUIError('هیچ شناسه Inbound معتبری برای این سرویس تعریف نشده است.')

        errors: list[str] = []
        result = None
        # The panel's own schema is the only source that is right by
        # construction, so it goes first. Everything after it is a guess kept
        # for panels that publish no schema.
        for label, attempt in (
            ('openapi', self._add_client_from_schema),
            ('clients-v3', self._add_client_v3),
            ('inbound-legacy', self._add_client_legacy),
        ):
            try:
                result = attempt(ids, client_payload)
                break
            except XUIError as exc:
                errors.append(f'{label}: {exc}')

        if result is None:
            raise XUIError(
                'ساخت کلاینت در 3x-ui ناموفق بود. '
                f'email={email}, inbound_ids={ids}. '
                'خطاها: ' + ' | '.join(errors[-6:])
            )

        # Whether every inbound was covered depends on which path answered: the
        # v3 API takes the whole list, the older ones take one. Rather than
        # guess, ask the panel which inbounds the client ended up on and attach
        # whatever is missing.
        result['inbound_ids'] = ids
        missing = self._inbounds_missing_for(email, ids)
        for inbound in missing:
            try:
                self._attach_client_to_inbound(email, inbound)
            except XUIError as exc:
                errors.append(f'attach {inbound}: {exc}')
        if missing:
            result['attached_extra'] = [i for i in missing]
        if errors:
            result['warnings'] = errors[-4:]
        return result

    def _inbounds_missing_for(self, email: str, wanted: list[int]) -> list[int]:
        """Which of the wanted inbounds this client is not on yet.

        Returns nothing when the panel cannot be read: attaching blindly would
        be worse than leaving a client that is already correct alone.
        """
        if len(wanted) <= 1:
            return []
        try:
            current = set(self.client_inbound_ids(email))
        except XUIError:
            return []
        if not current:
            return []
        return [i for i in wanted if i not in current]

    def client_inbound_ids(self, email: str) -> list[int]:
        """The inbounds a client is currently attached to."""
        found: list[int] = []

        def collect(value):
            if isinstance(value, list):
                for item in value:
                    try:
                        number = int(item)
                    except (TypeError, ValueError):
                        continue
                    if number > 0 and number not in found:
                        found.append(number)

        result = self.get_client(email)
        for holder in (result, result.get('obj') if isinstance(result, dict) else None):
            if isinstance(holder, dict):
                for key in ('inboundIds', 'inbound_ids', 'inbounds'):
                    collect(holder.get(key))
        return found

    def list_inbounds_brief(self) -> list[dict[str, Any]]:
        """Every inbound as {id, remark, protocol, port}, for picking in the panel."""
        rows = []
        for inbound in self._as_list(self.list_inbounds()):
            try:
                inbound_id = int(inbound.get('id') or 0)
            except (TypeError, ValueError):
                continue
            if inbound_id <= 0:
                continue
            rows.append({
                'id': inbound_id,
                'remark': str(inbound.get('remark') or '').strip(),
                'protocol': str(inbound.get('protocol') or '').strip(),
                'port': int(inbound.get('port') or 0),
                'enable': bool(inbound.get('enable', True)),
            })
        rows.sort(key=lambda row: row['id'])
        return rows

    def refresh_inbounds(self) -> list[dict[str, Any]]:
        """Read the inbound list and remember it on the panel row."""
        from django.utils import timezone

        rows = self.list_inbounds_brief()
        self.panel.inbounds_cache = rows
        self.panel.inbounds_fetched_at = timezone.now()
        self.panel.save(update_fields=['inbounds_cache', 'inbounds_fetched_at', 'updated_at'])
        return rows

    def get_settings(self) -> dict[str, Any]:
        """The panel's own settings blob, which includes the subscription server."""
        last_error = None
        for path in ('/setting/all', '/settings/all', '/setting'):
            try:
                result = self.request('POST', self._api_url(path))
            except XUIError as exc:
                last_error = exc
                try:
                    result = self.request('GET', self._api_url(path))
                except XUIError as exc2:
                    last_error = exc2
                    continue
            if isinstance(result, dict) and self._ok(result):
                inner = result.get('obj')
                return inner if isinstance(inner, dict) else result
        raise XUIError(f'خواندن تنظیمات پنل ناموفق بود: {last_error}')

    # The subscription section of 3x-ui has had several spellings across
    # versions and forks, so each value is looked up under all of them.
    SUB_KEYS = {
        'enable': ('subEnable', 'subscriptionEnable', 'sub_enable'),
        'port': ('subPort', 'subscriptionPort', 'sub_port'),
        'path': ('subPath', 'subscriptionPath', 'sub_path'),
        'domain': ('subDomain', 'subscriptionDomain', 'sub_domain'),
        'uri': ('subURI', 'subUri', 'subscriptionURI', 'sub_uri'),
        'cert': ('subCertFile', 'subscriptionCertFile', 'sub_cert_file'),
    }

    @classmethod
    def _pick(cls, blob: dict[str, Any], names: tuple[str, ...]):
        for name in names:
            if name in blob and blob[name] not in (None, ''):
                return blob[name]
        # Some builds nest everything one level down.
        for value in blob.values():
            if isinstance(value, dict):
                found = cls._pick(value, names)
                if found not in (None, ''):
                    return found
        return None

    def detect_subscription(self) -> dict[str, Any]:
        """Work out where this panel serves subscriptions from.

        The panel publishes it, so reading beats asking the operator to retype
        a port and a path they already configured once.
        """
        from django.utils import timezone

        try:
            blob = self.get_settings()
        except XUIError as exc:
            self.panel.sub_note = str(exc)[:300]
            self.panel.save(update_fields=['sub_note', 'updated_at'])
            raise

        uri = str(self._pick(blob, self.SUB_KEYS['uri']) or '').strip()
        detected: dict[str, Any] = {}

        if uri:
            # subURI is the full base the panel advertises; trust it whole.
            rest = uri.split('://', 1)
            detected['scheme'] = rest[0] if len(rest) == 2 and rest[0] in ('http', 'https') else 'https'
            body = rest[-1].rstrip('/')
            host, _, path = body.partition('/')
            domain, _, port = host.partition(':')
            detected['domain'] = domain
            if port.isdigit():
                detected['port'] = int(port)
            detected['path'] = '/' + path.strip('/') + '/' if path else '/sub/'
        else:
            port = self._pick(blob, self.SUB_KEYS['port'])
            path = self._pick(blob, self.SUB_KEYS['path'])
            domain = self._pick(blob, self.SUB_KEYS['domain'])
            cert = self._pick(blob, self.SUB_KEYS['cert'])
            if port not in (None, ''):
                try:
                    detected['port'] = int(port)
                except (TypeError, ValueError):
                    pass
            if path:
                detected['path'] = '/' + str(path).strip('/') + '/'
            if domain:
                detected['domain'] = str(domain).strip().strip('/')
            # A certificate configured for the subscription server is the only
            # honest signal that it answers on https.
            detected['scheme'] = 'https' if cert else 'http'

        # Scheme alone is a guess, not an answer: without a port, path or domain
        # nothing useful was actually read, and saying otherwise would send the
        # operator looking for a problem somewhere else.
        found_anything = any(detected.get(key) for key in ('port', 'path', 'domain'))
        if not found_anything:
            detected = {}
        else:
            enabled = self._pick(blob, self.SUB_KEYS['enable'])
            detected['enabled'] = bool(enabled) if enabled is not None else True

        self.panel.sub_detected = detected
        self.panel.sub_note = (
            ('خوانده شد: ' + ' '.join(f'{k}={v}' for k, v in detected.items()))[:300]
            if detected else
            'تنظیمات Subscription در پاسخ پنل پیدا نشد. روش را «دستی» بگذارید و مقادیر را وارد کنید.'
        )
        self.panel.save(update_fields=['sub_detected', 'sub_note', 'updated_at'])
        return detected

    def reset_client_traffic(self, email: str) -> bool:
        """Zero a client's used up/down counters.

        Needed on renewal: replacing the quota alone leaves the old usage in
        place, so a customer who renewed a used-up plan would still be over
        their limit and stay disconnected.
        """
        for path in (f'/clients/resetTraffic/{email}', f'/client/resetTraffic/{email}'):
            try:
                if self._ok(self.request('POST', self._api_url(path))):
                    return True
            except XUIError:
                continue
        return False

    def set_client_enabled(self, email: str, client_payload: dict[str, Any], enabled: bool) -> dict[str, Any]:
        """Switch a client on or off without changing anything else about it.

        The payload has to be complete, because `update_client` replaces the
        client rather than patching it: sending `{'enable': False}` alone would
        wipe the quota, the expiry and the IP limit. Callers rebuild the payload
        from the order, so the database stays the authority on what was sold.
        """
        body = dict(client_payload)
        body['enable'] = bool(enabled)
        return self.update_client(email, body)

    def delete_client(self, email: str, client_uuid: str = '') -> bool:
        """Remove a client from every inbound it sits on.

        Panels disagree about this route more than about any other: some take
        the client's uuid under the inbound, older builds take the email, and
        the path sits under either the API base or the panel root. Every known
        shape is tried and the first success wins, which is the same approach
        `add_client` already takes.

        Returns True when at least one inbound accepted the delete. A client
        that the panel no longer holds counts as deleted.
        """
        email = (email or '').strip()
        if not email:
            raise XUIError('برای حذف کلاینت، شناسه آن لازم است.')

        inbound_ids: list[int] = []
        try:
            inbound_ids = self.client_inbound_ids(email)
        except XUIError:
            pass
        if not inbound_ids:
            found = self.find_client_by_identifier(email)
            if found is None:
                # Nothing to delete. Saying so plainly beats raising, because
                # the caller's goal — this config is gone — already holds.
                return True
            inbound_ids = [found['inbound_id']]
            client_uuid = client_uuid or found.get('identifier') or ''

        attempts: list[str] = []
        keys = [key for key in (client_uuid, email) if key]
        for inbound_id in inbound_ids:
            for key in keys:
                for method, url in (
                    ('POST', self._panel_url(f'/inbounds/{inbound_id}/delClient/{key}')),
                    ('POST', self._panel_url(f'/inbounds/delClient/{inbound_id}/{key}')),
                    ('POST', self._api_url(f'/inbounds/{inbound_id}/delClient/{key}')),
                    ('POST', self._api_url(f'/clients/del/{key}')),
                    ('POST', self._api_url(f'/clients/delete/{key}')),
                    ('DELETE', self._api_url(f'/clients/{key}')),
                ):
                    try:
                        self.request(method, url)
                    except XUIError as exc:
                        attempts.append(f'{method} {url}: {exc}')

        # The panel's answer is not the test — whether the client is still there
        # is. This covers builds that reply in a shape `_ok` does not recognise,
        # and it is the only check that means anything to the caller.
        if self.find_client_by_identifier(email) is None:
            return True
        raise XUIError(
            'حذف کلاینت از پنل ناموفق بود. مسیر حذف این نسخه پنل شناسایی نشد: '
            + ('; '.join(attempts[-3:]) or 'هیچ مسیری پاسخ نداد')
        )

    def usage_by_email(self) -> dict[str, dict[str, int]]:
        """Live traffic counters for every client on this panel, in one request.

        One call covers the whole panel, which is what makes the sweep that
        looks for finished subscriptions cheap enough to run on a timer.
        """
        stats: dict[str, dict[str, int]] = {}
        for inbound in self._as_list(self.list_inbounds()):
            rows = inbound.get('clientStats') or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                email = str(row.get('email') or '').strip()
                if not email:
                    continue
                # A client on several inbounds has a row per inbound; its usage
                # and quota are the totals across them.
                bucket = stats.setdefault(email.lower(), {'up': 0, 'down': 0, 'total': 0})
                bucket['up'] += int(row.get('up') or 0)
                bucket['down'] += int(row.get('down') or 0)
                bucket['total'] += int(row.get('total') or 0)
        return stats

    def update_client(self, email: str, client_payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(client_payload)
        body['email'] = email
        for int_key in ('totalGB', 'expiryTime', 'limitIp', 'reset', 'up', 'down', 'tgId'):
            try:
                body[int_key] = int(body.get(int_key) or 0)
            except (TypeError, ValueError):
                body[int_key] = 0
        attempts = [
            ('POST', self._api_url(f'/clients/update/{email}'), {'json': body, 'headers': {'Content-Type': 'application/json'}}),
            ('PUT', self._api_url(f'/clients/{email}'), {'json': body, 'headers': {'Content-Type': 'application/json'}}),
            ('POST', self._api_url('/clients/update'), {'json': body, 'headers': {'Content-Type': 'application/json'}}),
            ('POST', self._api_url(f'/clients/update/{email}'), {'data': body, 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}),
        ]
        last_error = None
        for method, url, kwargs in attempts:
            try:
                result = self.request(method, url, **kwargs)
                if self._ok(result):
                    return result
                last_error = XUIError(str(result))
            except XUIError as exc:
                last_error = exc
        raise XUIError(f'تمدید/ویرایش کلاینت در 3x-ui ناموفق بود: {last_error}')
