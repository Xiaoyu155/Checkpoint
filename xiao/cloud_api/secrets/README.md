# Gateway runtime secrets

This directory is mounted read-only at `/run/secrets` by
`docker-compose.gateway.yml`. Keep actual merchant keys and certificates here;
all files except this README are ignored by Git and excluded from the Docker
build context.

Expected filenames in the generated environment example:

- `apiclient_key.pem`
- `wechatpay_public_key.pem` (optional)
- `wechatpay_platform_cert.pem` (optional)
