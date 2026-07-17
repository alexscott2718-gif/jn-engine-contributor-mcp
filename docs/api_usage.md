# REST Usage

`GET /health` is public. The other five GET routes require the same gateway bearer
used by MCP. Put the bearer only in the `Authorization` header, never in a URL.

~~~sh
curl -H "Authorization: Bearer $TOKEN" \
  'https://mcp.example.org/v1/content/search?q=C3DPlayer&scope=re&limit=5'

curl -H "Authorization: Bearer $TOKEN" \
  --get --data-urlencode "id=$CONTENT_ID" \
  'https://mcp.example.org/v1/content/fetch'

curl -H "Authorization: Bearer $TOKEN" \
  'https://mcp.example.org/v1/tasks/list?status=open&limit=20'

curl -H "Authorization: Bearer $TOKEN" \
  'https://mcp.example.org/v1/projects/context?max_chars=12000'

curl -H "Authorization: Bearer $TOKEN" \
  'https://mcp.example.org/v1/re/symbols?address=00437c40'
~~~

Search IDs are bound to one snapshot commit. After promotion, fetch returns HTTP 409
with `code=snapshot_changed`; run search again. Validation errors use HTTP 400 rather
than FastAPI's default 422 body. Every error has `code`, `detail`, and `request_id`.
