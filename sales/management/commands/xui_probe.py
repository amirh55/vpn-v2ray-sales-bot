from __future__ import annotations

import json
import time
import uuid

from django.core.management.base import BaseCommand, CommandError

from sales.models import XUIPanel
from sales.services.xui import XUIClient, XUIError


class Command(BaseCommand):
    help = 'Probe the configured 3x-ui panel API routes and optionally create a test client.'

    def add_arguments(self, parser):
        parser.add_argument('--panel-id', type=int, default=None, help='XUIPanel id. Defaults to first active panel.')
        parser.add_argument('--inbound-id', type=int, default=None, help='Inbound id for create test.')
        parser.add_argument('--create-test', action='store_true', help='Actually create a short test client on 3x-ui.')
        parser.add_argument('--email', default='', help='Custom email for create test.')

    def handle(self, *args, **opts):
        panel_qs = XUIPanel.objects.all()
        if opts['panel_id']:
            panel_qs = panel_qs.filter(pk=opts['panel_id'])
        else:
            panel_qs = panel_qs.filter(is_active=True)
        panel = panel_qs.first()
        if not panel:
            raise CommandError('هیچ پنل فعالی پیدا نشد.')

        client = XUIClient(panel)
        self.stdout.write(self.style.SUCCESS(f'Panel: #{panel.pk} {panel.name}'))
        self.stdout.write(f'base_url={client.base_url}')
        self.stdout.write(f'api_base_path={client.api_base_path}')
        self.stdout.write(f'verify_ssl={panel.verify_ssl}')
        self.stdout.write(f'auth_header={"Authorization: Bearer ***" if panel.api_token else "NO API TOKEN"}')

        self.stdout.write('\n--- OpenAPI paths ---')
        try:
            spec = client.get_openapi()
            paths = spec.get('paths', {}) if isinstance(spec, dict) else {}
            wanted = [p for p in sorted(paths) if 'client' in p.lower() or 'inbound' in p.lower()]
            for p in wanted:
                methods = ','.join(paths[p].keys()).upper() if isinstance(paths.get(p), dict) else ''
                self.stdout.write(f'{methods:8} {p}')
            if not wanted:
                self.stdout.write(self.style.WARNING('هیچ مسیر client/inbound داخل openapi.json پیدا نشد.'))
        except XUIError as exc:
            self.stdout.write(self.style.ERROR(f'OpenAPI failed: {exc}'))

        self.stdout.write('\n--- Basic API checks ---')
        for method, path in [
            ('GET', '/clients/list'),
            ('GET', '/inbounds/list'),
            ('GET', '/inbounds'),
        ]:
            url = client._api_url(path)
            try:
                result = client.request(method, url)
                summary = json.dumps(result, ensure_ascii=False)[:700]
                self.stdout.write(self.style.SUCCESS(f'{method} {url} => OK {summary}'))
            except XUIError as exc:
                self.stdout.write(self.style.ERROR(f'{method} {url} => {exc}'))

        if not opts['create_test']:
            self.stdout.write('\nبرای تست ساخت واقعی: python manage.py xui_probe --create-test --inbound-id 16')
            return

        inbound_id = opts['inbound_id']
        if not inbound_id:
            raise CommandError('--inbound-id لازم است.')

        email = opts['email'].strip() or f'test{int(time.time())}{uuid.uuid4().hex[:6]}'
        payload = {
            'email': email,
            'subId': email,
            'enable': True,
            'totalGB': 1024 * 1024 * 10,
            'expiryTime': int((time.time() + 3600) * 1000),
            'limitIp': 1,
            'tgId': 0,
            'reset': 0,
            'flow': '',
            'comment': 'vpnshop api probe - can delete',
        }
        self.stdout.write(f'\n--- Create test client: email={email}, inbound_id={inbound_id} ---')
        try:
            result = client.add_client(inbound_id, payload)
            self.stdout.write(self.style.SUCCESS(json.dumps(result, ensure_ascii=False, indent=2)[:4000]))
        except XUIError as exc:
            raise CommandError(str(exc))
