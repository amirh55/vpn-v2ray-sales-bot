module.exports = {
  apps: [
    {
      name: 'vpn-panel',
      script: './scripts/run_panel.sh',
      cwd: __dirname,
      interpreter: 'bash',
      autorestart: true,
      max_restarts: 10,
      env: {
        VPNSHOP_RUNTIME_DIR: '/var/lib/vpnshop',
        PORT: '8000',
        BIND_HOST: '0.0.0.0'
      }
    },
    {
      name: 'vpn-bot',
      script: './scripts/run_bot.sh',
      cwd: __dirname,
      interpreter: 'bash',
      autorestart: true,
      max_restarts: 10,
      env: {
        VPNSHOP_RUNTIME_DIR: '/var/lib/vpnshop'
      }
    }
  ]
};
