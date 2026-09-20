# RecreationBench Sandbox access

The Linux and Web Sandbox paths require an FC Agent Sandbox account and the following
settings.

## `E2B_API_KEY`

Create an API key in the Function Compute console under Cloud Sandbox and API Key
Management:

https://fcnext.console.aliyun.com/cn-beijing/sandbox-api-keys

## `E2B_API_URL`

Use the endpoint for your region:

```text
https://api.<region>.e2b.fc.aliyuncs.com
```

For example:

```text
https://api.cn-hongkong.e2b.fc.aliyuncs.com
```

## `E2B_DOMAIN`

Use the matching regional domain:

```text
<region>.e2b.fc.aliyuncs.com
```

For example:

```text
cn-hongkong.e2b.fc.aliyuncs.com
```

## Sandbox template

A template is generated from a Docker image by `build_template.py`; its
`template_id` is not chosen manually. You can use a published benchmark image or
build `providers/linux/Dockerfile` or `providers/web/Dockerfile` and push it to a
registry that Sandbox can access.

The workflow is:

1. Build the Linux or Web image.
2. Push it to a publicly reachable registry.
3. Run `build_template.py` from `providers/linux/template` or
   `providers/web/template`.
4. Save the returned ID as `RB_LINUX_TEMPLATE` or `RB_WEB_TEMPLATE`.

See the [Linux guide](linux.md) and [Web guide](web.md) for complete commands.
