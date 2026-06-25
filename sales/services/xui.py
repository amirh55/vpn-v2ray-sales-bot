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
                response.raise_for_status()
                text = (response.text or '').strip()
                if not text:
                    return {'success': True}
                try:
                    return response.json()
                except Exception:
                    return {'success': True, 'raw': text}
        except Exception as exc:  # noqa: BLE001
            raise XUIError(f'خطا در ارتباط با 3x-ui: {exc}') from exc

    @staticmethod
    def _ok(result: dict[str, Any]) -> bool:
        return result.get('success') is not False

    @staticmethod
    def _extract_uuid(obj: Any) -> str:
        if isinstance(obj, dict):
            for key in ('id', 'uuid', 'clientId', 'client_id'):
                val = obj.get(key)
                if val:
                    return str(val)
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
            return XUIClient._extract_uuid(obj[0])
        return ''

    def get_openapi(self) -> dict[str, Any]:
        return self.request('GET', self._api_url('/openapi.json'))

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

    def _client_v3_payloads(self, inbound_id: int, client_payload: dict[str, Any]) -> list[dict[str, Any]]:
        email = str(client_payload['email']).strip()
        total_gb = int(client_payload.get('totalGB') or 0)
        expiry_time = int(client_payload.get('expiryTime') or 0)
        limit_ip = int(client_payload.get('limitIp') or 0)
        reset = int(client_payload.get('reset') or 0)
        tg_id = client_payload.get('tgId') or ''
        sub_id = str(client_payload.get('subId') or email).strip() or email
        enable = bool(client_payload.get('enable', True))
        flow = str(client_payload.get('flow') or '')

        base = {
            'email': email,
            'subId': sub_id,
            'enable': enable,
            'totalGB': total_gb,
            'expiryTime': expiry_time,
            'limitIp': limit_ip,
            'tgId': str(tg_id),
            'reset': reset,
            'flow': flow,
            'comment': str(client_payload.get('comment') or ''),
            # Keep this for forks/builds that accept caller supplied UUID.
            'id': str(client_payload.get('id') or ''),
        }
        # Newer 3.x builds use a client-level API. Different builds/forks used
        # slightly different names for the selected inbound list, so try all.
        return [
            {**base, 'inboundIds': [int(inbound_id)]},
            {**base, 'inbounds': [int(inbound_id)]},
            {**base, 'attachedInbounds': [int(inbound_id)]},
            {**base, 'inbound_ids': [int(inbound_id)]},
            dict(base),
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

    def _add_client_v3(self, inbound_id: int, client_payload: dict[str, Any]) -> dict[str, Any]:
        email = str(client_payload['email']).strip()
        last_error = None
        create_paths = ('/clients', '/clients/', '/client', '/client/add', '/clients/add')
        for payload in self._client_v3_payloads(inbound_id, client_payload):
            for path in create_paths:
                # JSON request used by current 3.x API and in-panel Swagger.
                try:
                    result = self.request('POST', self._api_url(path), json=payload, headers={'Content-Type': 'application/json'})
                    if self._ok(result):
                        # Some builds create the client but do not attach it when the
                        # selected inbound key name differs; attach defensively.
                        try:
                            self._attach_client_to_inbound(email, inbound_id)
                        except XUIError:
                            pass
                        try:
                            got = self.get_client(email)
                            uuid_value = self._extract_uuid(got)
                        except XUIError:
                            uuid_value = self._extract_uuid(result)
                        if uuid_value:
                            result['client_uuid'] = uuid_value
                        result['client_email'] = email
                        result['api_mode'] = 'clients-v3'
                        return result
                    last_error = XUIError(str(result))
                except XUIError as exc:
                    last_error = exc
                # Some proxies/forks only parse form-urlencoded bodies.
                try:
                    result = self.request(
                        'POST',
                        self._api_url(path),
                        data={k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in payload.items()},
                        headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    )
                    if self._ok(result):
                        try:
                            self._attach_client_to_inbound(email, inbound_id)
                        except XUIError:
                            pass
                        result['client_email'] = email
                        result['api_mode'] = 'clients-v3-form'
                        return result
                    last_error = XUIError(str(result))
                except XUIError as exc:
                    last_error = exc
        raise XUIError(str(last_error))

    def _add_client_legacy(self, inbound_id: int, client_payload: dict[str, Any]) -> dict[str, Any]:
        settings_obj = {'clients': [client_payload]}
        settings_json = json.dumps(settings_obj, ensure_ascii=False, separators=(',', ':'))
        legacy_body = {'id': int(inbound_id), 'settings': settings_json}
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

    def add_client(self, inbound_id: int, client_payload: dict[str, Any]) -> dict[str, Any]:
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

        errors: list[str] = []
        # Prefer 3.x global client API because current 3x-ui versions moved user
        # management to /panel/api/clients and attach clients to inbounds.
        try:
            return self._add_client_v3(inbound_id, client_payload)
        except XUIError as exc:
            errors.append(f'clients-v3: {exc}')

        try:
            return self._add_client_legacy(inbound_id, client_payload)
        except XUIError as exc:
            errors.append(f'inbound-legacy: {exc}')

        raise XUIError(
            'ساخت کلاینت در 3x-ui ناموفق بود. '
            f'email={email}, inbound_id={inbound_id}. '
            f'خطاها: ' + ' | '.join(errors[-6:])
        )

    def update_client(self, email: str, client_payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(client_payload)
        body['email'] = email
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
