from __future__ import annotations

import json
from typing import Any

import httpx

from sales.models import XUIPanel


class XUIError(RuntimeError):
    pass


class XUIClient:
    """Small 3x-ui API client.

    3x-ui exposes Swagger/OpenAPI on the panel itself. Versions can differ, so this
    client tries the common endpoints used by recent and older 3x-ui/x-ui builds.
    If your panel uses different routes, keep the model/templates and adjust these
    methods only.
    """

    def __init__(self, panel: XUIPanel):
        self.panel = panel
        self.base_url = panel.base_url.rstrip('/')
        self.api_base_path = '/' + panel.api_base_path.strip('/')
        self.timeout = panel.timeout_seconds or 20
        self.headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
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

    def request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout, verify=self.panel.verify_ssl) as client:
                response = client.request(method, url, headers=self.headers, **kwargs)
                response.raise_for_status()
                if response.text:
                    return response.json()
                return {'success': True}
        except Exception as exc:  # noqa: BLE001
            raise XUIError(f'خطا در ارتباط با 3x-ui: {exc}') from exc

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

        # Official/legacy 3x-ui addClient format is:
        # {"id": inbound_id, "settings": "{\"clients\":[...]}"}
        # Newer/forked builds sometimes also accept an object or top-level clients.
        settings_obj = {'clients': [client_payload]}
        settings_json = json.dumps(settings_obj, ensure_ascii=False, separators=(',', ':'))
        attempts = [
            ('POST', self._api_url('/inbounds/addClient'), {'json': {'id': int(inbound_id), 'settings': settings_json}}),
            ('POST', self._api_url('/inbound/addClient'), {'json': {'id': int(inbound_id), 'settings': settings_json}}),
            ('POST', self._api_url('/inbounds/addClient'), {'json': {'id': int(inbound_id), 'settings': settings_obj}}),
            ('POST', self._api_url('/inbounds/addClient'), {'json': {'id': int(inbound_id), 'clients': [client_payload]}}),
            ('POST', self._api_url('/clients'), {'json': {'inboundId': int(inbound_id), **client_payload}}),
            ('POST', self._api_url('/clients/add'), {'json': {'inboundId': int(inbound_id), **client_payload}}),
        ]
        last_error = None
        for method, url, kwargs in attempts:
            try:
                result = self.request(method, url, **kwargs)
                if result.get('success') is False:
                    last_error = XUIError(str(result))
                    continue
                return result
            except XUIError as exc:
                last_error = exc
        raise XUIError(f'ساخت کلاینت در 3x-ui ناموفق بود. آخرین خطا: {last_error}. email={email}, inbound_id={inbound_id}')

    def update_client(self, email: str, client_payload: dict[str, Any]) -> dict[str, Any]:
        attempts = [
            ('POST', self._api_url(f'/clients/update/{email}'), {'json': client_payload}),
            ('PUT', self._api_url(f'/clients/{email}'), {'json': client_payload}),
            ('POST', self._api_url('/clients/update'), {'json': {'email': email, **client_payload}}),
        ]
        last_error = None
        for method, url, kwargs in attempts:
            try:
                result = self.request(method, url, **kwargs)
                if result.get('success') is False:
                    last_error = XUIError(str(result))
                    continue
                return result
            except XUIError as exc:
                last_error = exc
        raise XUIError(f'تمدید/ویرایش کلاینت در 3x-ui ناموفق بود: {last_error}')
